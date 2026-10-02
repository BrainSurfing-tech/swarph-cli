"""codex exec transport for the bench `codex` backend. Subscription-only, isolated,
billing-guarded. Sibling of :mod:`swarph_cli.bench.claude_cli`, and it differs from it
in three places that matter.

Measured 2026-10-02, codex-cli 0.157.0, macOS arm64, a one-line prompt:
    default config                              15,472 input tokens
    --ignore-user-config --ignore-rules         14,410 input tokens
The isolation flags remove the operator's MCP servers and plugins, and that is all
they remove. The floor is the CLI's own agent instructions: there is no flag here that
turns `codex exec` into a bare completion, so this arm does not reach the ~421 tokens
`claude -p --tools ""` does. Compare arms on quality and on OUTPUT tokens; the input
count carries this fixed load.

THE BILLING GUARD IS CHECKED BEFORE THE CALL, NOT READ FROM THE RESULT. The claude
stream names its key source (`apiKeySource`). The codex stream names nothing about
billing, so the guard reads `auth_mode` from `$CODEX_HOME/auth.json` and refuses to
call unless it is `chatgpt`. A file that cannot be read is `billing:unknown`: not
knowing is a refusal, never a pass.

NO SYSTEM-PROMPT FLAG. `codex exec` has none, so the pack's `system` is sent on stdin
ahead of the prompt, separated by a blank line.

The child runs in an empty temporary directory under a read-only sandbox, so there is
no repository for the agent to read and nothing it can write.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from swarph_cli.bench.claude_cli import CliResult

_STRIP_EXACT = ("OPENAI_API_KEY", "CODEX_API_KEY")
_RATE = ("usage limit", "rate limit", "429", "too many requests")
_SUBSCRIPTION_MODE = "chatgpt"


def build_argv(model_id: str) -> list:
    """Fixed argv. The trailing `-` reads the prompt from stdin, never from argv."""
    return ["codex", "exec", "--model", model_id, "--json", "--ephemeral",
            "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "-"]


def clean_env(env: dict) -> dict:
    """Drop billing keys and every SWARPH_CHANNEL* name, including ones not listed yet."""
    return {
        k: v for k, v in env.items()
        if k not in _STRIP_EXACT and not k.startswith("SWARPH_CHANNEL")
    }


def codex_home(env: dict) -> Path:
    return Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))


def auth_mode(home: Path) -> Optional[str]:
    """`auth_mode` from auth.json, or None when it cannot be read. None is not a pass."""
    try:
        data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("auth_mode") if isinstance(data, dict) else None
    return mode if isinstance(mode, str) and mode else None


def stdin_text(prompt: str, system: str) -> str:
    return f"{system}\n\n{prompt}" if system else prompt


def _is_rate(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in _RATE)


def parse_stream(lines, stderr: str = "", key: Optional[str] = _SUBSCRIPTION_MODE) -> CliResult:
    """JSONL from `codex exec --json` -> CliResult.

    The answer is the LAST agent_message. Usage is the LAST turn.completed: that event
    carries the thread's running total, so adding two of them would double-count.
    A usage limit is `rate_limit` whether it arrives as an event or on stderr — the
    runner retries that string and never scores it as an answer.
    """
    text, usage, failure = None, None, None
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        if kind == "item.completed":
            item = e.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif kind == "turn.completed":
            usage = e.get("usage") or {}
        elif kind in ("error", "turn.failed"):
            detail = e.get("message") or e.get("error") or ""
            failure = (kind, detail if isinstance(detail, str) else json.dumps(detail))
    u = usage or {}
    tin, tout = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    if failure is not None:
        err = "rate_limit" if _is_rate(failure[1]) or _is_rate(stderr) else f"cli_error:{failure[0]}"
        return CliResult("", tin, tout, key, None, error=err)
    if text is None or usage is None:
        err = "rate_limit" if _is_rate(stderr) else "no_result"
        return CliResult("", tin, tout, key, None, error=err)
    return CliResult(text, tin, tout, key, None)


def call(model_id: str, prompt: str, system: str, *, runner=subprocess.run,
         timeout: int = 300, env: Optional[dict] = None) -> CliResult:
    source = dict(os.environ) if env is None else dict(env)
    mode = auth_mode(codex_home(source))
    if mode != _SUBSCRIPTION_MODE:
        # Refused before any call: nothing was spent, and nothing is scored.
        return CliResult("", 0, 0, mode, None, error=f"billing:{mode or 'unknown'}")
    with tempfile.TemporaryDirectory(prefix="swarph-bench-codex-") as cwd:
        p = runner(build_argv(model_id), input=stdin_text(prompt, system), capture_output=True,
                   text=True, env=clean_env(source), timeout=timeout, cwd=cwd)
    return parse_stream((p.stdout or "").splitlines(), p.stderr or "", key=mode)
