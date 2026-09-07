#!/usr/bin/env python3
"""Minimal Python replacement for the `mmx` CLI `text chat` path.

The environment here can miss a Node.js runtime, which causes the packaged
`mmx` executable to fail immediately. This shim keeps the same transport
surface that the MmxAgent expects:

  * reads prompt messages from `--messages-file`
  * optionally passes tool definitions from repeated `--tool` arguments
  * POSTs to `<base_url>/anthropic/v1/messages` on the configured base URL
  * prints one JSON response object to stdout
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("subcommand1", nargs="?", default="")
    parser.add_argument("subcommand2", nargs="?", default="")
    parser.add_argument("--model", required=True)
    parser.add_argument("--messages-file", dest="messages_file", required=True)
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=4096)
    parser.add_argument("--base-url", dest="base_url", required=True)
    parser.add_argument("--api-key", dest="api_key", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--tool", dest="tools", action="append", default=[])
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--output", default="json")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parsed = parser.parse_args(argv)
    if not parsed.subcommand1 or parsed.subcommand1 != "text":
        raise SystemExit("unsupported mmx command: expected `text` as first arg")
    if not parsed.subcommand2 or parsed.subcommand2 != "chat":
        raise SystemExit("unsupported mmx command: expected `chat` as second arg")
    return parsed


class AnthropicStreamAccumulator:
    """Rebuild a full Anthropic Messages response from SSE events."""

    def __init__(self):
        self.message: dict = {}
        self.blocks: dict[int, dict] = {}
        self.partial_json: dict[int, str] = {}
        self.error: dict | None = None

    def feed(self, raw: str) -> None:
        if not raw or raw == "[DONE]":
            return
        event = json.loads(raw)
        event_type = event.get("type")
        if event_type == "error":
            self.error = event
            return
        if event_type == "message_start":
            self.message = dict(event.get("message") or {})
            content = self.message.pop("content", []) or []
            self.blocks = {
                index: dict(block)
                for index, block in enumerate(content)
                if isinstance(block, dict)
            }
            return
        if event_type == "content_block_start":
            index = int(event.get("index", len(self.blocks)))
            self.blocks[index] = dict(event.get("content_block") or {})
            return
        if event_type == "content_block_delta":
            index = int(event.get("index", 0))
            block = self.blocks.setdefault(index, {})
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                self.partial_json[index] = (
                    self.partial_json.get(index, "")
                    + str(delta.get("partial_json") or "")
                )
            elif delta_type == "text_delta":
                block["text"] = (str(block.get("text") or "") +
                                 str(delta.get("text") or ""))
            elif delta_type == "thinking_delta":
                block["thinking"] = (str(block.get("thinking") or "") +
                                     str(delta.get("thinking") or ""))
            else:
                for key, value in delta.items():
                    if key == "type":
                        continue
                    if isinstance(value, str) and isinstance(block.get(key), str):
                        block[key] += value
                    else:
                        block[key] = value
            return
        if event_type == "content_block_stop":
            self._finish_block(int(event.get("index", 0)))
            return
        if event_type == "message_delta":
            self.message.update(event.get("delta") or {})
            usage = event.get("usage") or {}
            if usage:
                self.message["usage"] = {
                    **(self.message.get("usage") or {}),
                    **usage,
                }

    def _finish_block(self, index: int) -> None:
        if index not in self.partial_json:
            return
        partial = self.partial_json.pop(index)
        try:
            self.blocks.setdefault(index, {})["input"] = (
                json.loads(partial) if partial else {}
            )
        except ValueError as exc:
            self.error = {
                "type": "error",
                "error": {
                    "type": "invalid_tool_input",
                    "message": f"could not decode streamed tool input: {exc}",
                },
            }

    def response(self) -> dict:
        for index in list(self.partial_json):
            self._finish_block(index)
        if self.error:
            return self.error
        if not self.message:
            return {
                "type": "error",
                "error": {
                    "type": "proxy_error",
                    "message": "upstream stream ended without message_start",
                },
            }
        response = dict(self.message)
        response["content"] = [self.blocks[index]
                               for index in sorted(self.blocks)]
        return response


def _read_sse_response(body) -> dict:
    accumulator = AnthropicStreamAccumulator()
    for raw_line in body:
        line = raw_line.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        accumulator.feed(data)
    return accumulator.response()


def _read_body(response) -> dict:
    content_type = (response.headers.get("content-type", "") or "").lower()
    if "text/event-stream" in content_type:
        return _read_sse_response(response)
    payload = response.read()
    try:
        value = json.loads(payload)
    except ValueError:
        return {"type": "error", "error": {"type": "json_decode_error",
                                          "message": payload.decode(errors="replace")[:4000]}}
    return value if isinstance(value, dict) else {"type": "error",
                                                 "error": {"type": "proxy_error",
                                                           "message": value}}


def _request_payload(args: argparse.Namespace, messages: list,
                     tools: list[dict]) -> dict:
    payload = {
        "model": args.model,
        "messages": messages,
        "max_tokens": int(args.max_tokens),
        "stream": bool(args.stream),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = {"type": "auto"}
    return payload


def main() -> None:
    args = _parse_args(__import__("sys").argv[1:])
    messages_path = Path(args.messages_file)
    try:
        messages = json.loads(messages_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to read --messages-file: {exc}") from exc

    tools = []
    for tool_path in args.tools:
        loaded = json.loads(Path(tool_path).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            tools.append(loaded)

    payload = _request_payload(args, messages, tools)

    request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    endpoint = args.base_url.rstrip("/") + "/anthropic/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "Anthropic-Version": "2023-06-01",
    }
    if args.api_key:
        headers["x-api-key"] = args.api_key

    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(args.timeout)) as response:
            response_payload = _read_body(response)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(error_body)
            # ensure error surface stays structured for downstream parsing
            if isinstance(parsed, dict):
                print(json.dumps(parsed), end="")
                return
        except ValueError:
            pass
        print(json.dumps({
            "error": {
                "type": "http_error",
                "message": error_body or str(exc.reason),
            },
        }), end="")
        raise SystemExit(1) from exc
    except OSError as exc:
        print(json.dumps({
            "error": {
                "type": "network_error",
                "message": str(exc),
            },
        }), end="")
        raise SystemExit(1) from exc

    print(json.dumps(response_payload), end="")


if __name__ == "__main__":
    main()
