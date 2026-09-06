#!/usr/bin/env python3
"""MiniMax-M3 official endpoint wrapper for iterative image runs.

This module uses the official MiniMax anthropic-compatible Messages endpoint with
`MINIMAX_API_KEY` and supports the same image-to-code call pattern as the other
raw inline agents.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from ..qwen3_8_max_preview_raw.backend import (
        extract_script,
        load_image,
        render_script,
        rendered_views,
    )
except ImportError:
    from qwen3_8_max_preview_raw.backend import (
        extract_script,
        load_image,
        render_script,
        rendered_views,
    )


DEFAULT_ALIAS = "minimax-m3"
DEFAULT_BASE_URL = "https://api.minimax.io/anthropic/v1"
DEFAULT_MODEL_NAME = "MiniMax-M3"
VISION_MODEL = "minimax-m3"


class _Interface:
    """Minimal object matching tasksolver-style agents used by iterative_runner."""

    def __init__(self) -> None:
        self.thinking_depth = None


class _Agent:
    def __init__(self) -> None:
        self.visual_interface = _Interface()


def _resolve_api_key() -> str | None:
    return os.environ.get("MINIMAX_API_KEY")


def _resolve_base_url() -> str:
    return os.environ.get("MINIMAX_OPENAI_BASE_URL", DEFAULT_BASE_URL)


def _resolve_model_name(model_name: str | None) -> str:
    return (
        os.environ.get("MINIMAX_MODEL_NAME")
        or (model_name if model_name else None)
        or DEFAULT_MODEL_NAME
    )


def _encode_image(pil_image) -> tuple[str, str]:
    source = str(pil_image.info.get("image_path", ""))
    if source:
        source_path = Path(source)
        media_type = "image/" + (source_path.suffix.lower().lstrip(".") or "png")
        data = source_path.read_bytes()
    else:
        from io import BytesIO

        buf = BytesIO()
        pil_image.save(buf, format="PNG")
        data = buf.getvalue()
        media_type = "image/png"

    return base64.b64encode(data).decode("ascii"), media_type


def _anthropic_content(prompt: str, pil_images) -> list[dict[str, Any]]:
    images = [{"type": "text", "text": prompt}]
    for image in pil_images:
        data, media_type = _encode_image(image)
        images.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        )
    return images


def _response_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, dict):
                piece = block.get("text", "")
                if piece:
                    pieces.append(str(piece))
        if pieces:
            return "".join(pieces).strip()

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message")
        if isinstance(message, dict):
            text = message.get("content") or ""
            if text:
                return str(text).strip()

    for key in ("text", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_agent(vision_model: str, credentials):  # noqa: ARG001 - parity with API
    if vision_model != VISION_MODEL:
        raise ValueError(f"unsupported backend {vision_model}")
    return _Agent()


def call_model(
    interface: _Interface, text: str, pil_images, max_tokens: int, model: str
):
    api_key = _resolve_api_key()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY is not set; minimax-m3 requires it.")

    base_url = _resolve_base_url()
    model_name = _resolve_model_name(model)
    request_body = {
        "model": model_name,
        "messages": [{"role": "user", "content": _anthropic_content(text, pil_images)}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/messages",
        data=json.dumps(request_body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180.0) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"detail": body or exc.reason}
        if "error" in parsed:
            message = parsed.get("error", parsed)
            raise RuntimeError(f"minimax-m3 API error: {message}") from exc
        raise RuntimeError(f"minimax-m3 API error: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"minimax-m3 API request failed: {exc}") from exc

    reply = _response_text(payload)
    raw = {
        "backend": "minimax-m3",
        "model": model_name,
        "thinking_depth": getattr(interface, "thinking_depth", None),
        "errors": [],
        "text": reply,
        "n_images": len(pil_images),
        "response_metadata": payload,
    }
    if not reply:
        raise RuntimeError("minimax-m3 returned no output")
    return reply, raw


def is_backend_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    indicators = [
        "rate limit",
        "usage limit",
        "quota",
        "429",
        "530",
        "too many requests",
        "context length",
        "model overloaded",
        "token limit",
    ]
    return any(indicator in msg for indicator in indicators)


__all__ = [
    "build_agent",
    "call_model",
    "extract_script",
    "is_backend_limit_error",
    "load_image",
    "render_script",
    "rendered_views",
]
