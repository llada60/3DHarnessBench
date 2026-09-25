"""Process-lifetime guard for the parallel MCP benchmark runners.

The per-task runners already clean up in ``finally`` blocks, but no Python
``finally`` can run after SIGKILL.  ``RunJanitor`` starts a tiny process outside
the runner's session and tags every subsequently spawned descendant with a
unique environment value.  Closing the control pipe -- whether deliberately,
because Python exits, or because it is killed -- makes the guard terminate all
processes carrying that tag.

Process-group leaders are terminated as groups as well.  This matters for CLI
agents which start their own MCP subprocesses and may not propagate the tag in
the environment they construct for those subprocesses.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


ENV_KEY = "THREEDCOT_MCP_RUN_ID"
TERM_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 2.0


def _start_time(pid: int) -> str | None:
    """Return Linux /proc starttime, which disambiguates PID reuse."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]  # proc(5) field 22; this list starts at field 3
    except (OSError, IndexError):
        return None


def _same_process(pid: int, started: str | None) -> bool:
    return started is not None and _start_time(pid) == started


def _tagged_pids(run_id: str, *, exclude: set[int]) -> set[int]:
    marker = f"{ENV_KEY}={run_id}".encode()
    found: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude:
            continue
        try:
            environment = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in environment:
            found.add(pid)
    return found


def _alive_non_zombie(pid: int) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return bool(fields) and fields[0] != "Z"
    except (OSError, IndexError):
        return False


def _signal_processes(pids: set[int], sig: signal.Signals, log) -> None:
    """Signal tagged session leaders as groups, then every tagged PID."""
    groups: set[int] = set()
    for pid in pids:
        try:
            if os.getpgid(pid) == pid:
                groups.add(pid)
        except ProcessLookupError:
            pass
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
            print(f"signal {sig.name} process-group {pgid}", file=log, flush=True)
        except ProcessLookupError:
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _cleanup(run_id: str, owner_pid: int, *, kill_owner: bool, log) -> None:
    excluded = {os.getpid()}
    if not kill_owner:
        excluded.add(owner_pid)

    tagged = _tagged_pids(run_id, exclude=excluded)
    if kill_owner and _alive_non_zombie(owner_pid):
        tagged.add(owner_pid)
    print(f"cleanup start: {len(tagged)} process(es)", file=log, flush=True)
    _signal_processes(tagged, signal.SIGTERM, log)

    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # Re-scan to catch a child forked while TERM was being delivered.
        remaining = _tagged_pids(run_id, exclude=excluded)
        if kill_owner and _alive_non_zombie(owner_pid):
            remaining.add(owner_pid)
        if not remaining:
            print("cleanup complete after SIGTERM", file=log, flush=True)
            return
        time.sleep(0.1)

    remaining = _tagged_pids(run_id, exclude=excluded)
    if kill_owner and _alive_non_zombie(owner_pid):
        remaining.add(owner_pid)
    _signal_processes(remaining, signal.SIGKILL, log)
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _alive_non_zombie(pid)}
        remaining.update(_tagged_pids(run_id, exclude=excluded))
        if not remaining:
            break
        time.sleep(0.1)
    print(f"cleanup complete after SIGKILL; remaining={sorted(remaining)}",
          file=log, flush=True)


def _watch(args: argparse.Namespace) -> int:
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", buffering=1) as log:
        print(f"janitor={os.getpid()} owner={args.owner} launcher={args.launcher}",
              file=log)
        reason = "control pipe closed"
        kill_owner = False
        while True:
            readable, _, _ = select.select([args.control_fd], [], [], 0.5)
            if readable:
                try:
                    data = os.read(args.control_fd, 1)
                except OSError:
                    data = b""
                if not data:
                    break
            if not _same_process(args.owner, args.owner_started):
                reason = "owner exited"
                break
            if (args.launcher > 1 and
                    not _same_process(args.launcher, args.launcher_started)):
                # Killing ``pixi run`` must not leave its Python runner alive.
                reason = "launcher exited"
                kill_owner = True
                break
        print(f"trigger: {reason}", file=log, flush=True)
        _cleanup(args.run_id, args.owner, kill_owner=kill_owner, log=log)
    return 0


class RunJanitor:
    """Tag and reap every process created during one MCP batch invocation."""

    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)
        self.run_id = uuid.uuid4().hex
        self._previous = os.environ.get(ENV_KEY)
        self._control: int | None = None
        self._guard: subprocess.Popen | None = None
        self._closed = False

    def start(self) -> "RunJanitor":
        if self._guard is not None:
            return self
        owner = os.getpid()
        launcher = os.getppid()
        owner_started = _start_time(owner)
        launcher_started = _start_time(launcher)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        env = os.environ.copy()
        env.pop(ENV_KEY, None)  # the janitor must never reap itself
        command = [
            sys.executable, str(Path(__file__).resolve()), "--watch",
            "--run-id", self.run_id,
            "--owner", str(owner),
            "--owner-started", owner_started or "",
            "--launcher", str(launcher),
            "--launcher-started", launcher_started or "",
            "--control-fd", str(read_fd),
            "--log", str(self.log_path),
        ]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._guard = subprocess.Popen(
                command, pass_fds=(read_fd,), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, env=env,
            )
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(read_fd)
        self._control = write_fd
        # Set the marker only after starting the untagged guard. Every worker,
        # CLI, Blender, Xvfb and MCP process started later inherits it.
        os.environ[ENV_KEY] = self.run_id
        return self

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._control is not None:
            os.close(self._control)
            self._control = None
        if self._guard is not None:
            try:
                self._guard.wait(
                    timeout=TERM_GRACE_SECONDS + KILL_GRACE_SECONDS + 5)
            except subprocess.TimeoutExpired:
                self._guard.kill()
                self._guard.wait()
            self._guard = None
        if self._previous is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self._previous

    def __enter__(self) -> "RunJanitor":
        return self.start()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.cleanup()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--owner", type=int)
    parser.add_argument("--owner-started")
    parser.add_argument("--launcher", type=int)
    parser.add_argument("--launcher-started")
    parser.add_argument("--control-fd", type=int)
    parser.add_argument("--log")
    return parser


if __name__ == "__main__":
    parsed = _parser().parse_args()
    if not parsed.watch:
        raise SystemExit("process_janitor.py is an internal --watch helper")
    raise SystemExit(_watch(parsed))
