"""Report the App Server JSON-RPC category for an intentionally invalid thread.

This starts a separate App Server child and terminates only that child when the
probe completes. It sends no turns, tools, credentials, or mesh traffic.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from queue import Empty, Queue


def main() -> int:
    proc = subprocess.Popen(
        ["codex.cmd" if os.name == "nt" else "codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    events: Queue[str | None] = Queue()

    def read_events() -> None:
        assert proc.stdout
        for line in proc.stdout:
            events.put(line)
        events.put(None)

    threading.Thread(target=read_events, daemon=True).start()

    def request(request_id: int, method: str, params: dict) -> dict:
        assert proc.stdin
        proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                line = events.get(timeout=max(0, deadline - time.monotonic()))
            except Empty as exc:
                raise TimeoutError(f"timed out waiting for {method}") from exc
            if line is None:
                raise RuntimeError("App Server closed its protocol stream")
            event = json.loads(line)
            if event.get("id") == request_id:
                return event
        raise TimeoutError(f"timed out waiting for {method}")

    try:
        initialized = request(1, "initialize", {"clientInfo": {"name": "swarph-probe", "version": "1"}})
        if "error" in initialized:
            raise RuntimeError(f"initialize failed: {initialized['error']}")
        assert proc.stdin
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        # Valid UUID syntax ensures App Server attempts lookup rather than only
        # rejecting the request during parameter validation.
        missing_thread = request(2, "thread/resume", {"threadId": "00000000-0000-4000-8000-000000000000"})
        malformed_resume = request(3, "thread/resume", {})

        def error_summary(result: dict) -> dict:
            error = result.get("error", {})
            return {
                "has_error": bool(error),
                "code": error.get("code"),
                "message": error.get("message"),
                "data_type": type(error.get("data")).__name__,
            }

        print(json.dumps({
            "nonexistent_thread": error_summary(missing_thread),
            "malformed_resume": error_summary(malformed_resume),
        }, indent=2))
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
