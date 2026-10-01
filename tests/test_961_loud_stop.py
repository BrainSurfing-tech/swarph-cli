"""Card #961. A capped harness self-expires its inbox tail and says so.

Grok kills a background task at 10h. The hook wraps that tail in
``timeout -k 5s 590m`` (one unit; ``9h50m`` is rejected) and echoes one
EXPIRED line. Claude, cursor, and codex have no known cap and stay the
bare pipeline. On main, ``--harness grok`` is an unsupported-harness
refusal and this file fails.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from swarph_cli.commands import wake_hook_output as who

_EXPIRED = "[MESH WAKE EXPIRED] re-arm the inbox tail now"


def _context(monkeypatch, capsys, harness: str, tmp_path, cell: str = "probe-cell"):
    def _dir(name):
        return tmp_path / "swarph_state" / name / "mesh-sidecar"

    monkeypatch.setattr(who, "_sidecar_dir", _dir)
    monkeypatch.setattr(who, "_resolve_cell", lambda explicit=None: (cell, "test"))
    side = _dir(cell)
    side.mkdir(parents=True, exist_ok=True)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    rc = who.run_wake_hook_output(["--harness", harness])
    assert rc == 0
    raw = json.loads(capsys.readouterr().out)
    if harness == "cursor":
        ctx = raw["additional_context"]
    else:
        ctx = raw["hookSpecificOutput"]["additionalContext"]
    return ctx, inbox


def _timeout_line(ctx: str) -> str:
    lines = [line.strip() for line in ctx.splitlines() if "timeout -k 5s" in line]
    assert len(lines) == 1
    return lines[0]


def test_grok_emits_a_single_unit_timeout_and_the_expired_echo(monkeypatch, capsys, tmp_path):
    ctx, _inbox = _context(monkeypatch, capsys, "grok", tmp_path)
    line = _timeout_line(ctx)
    assert line.startswith("timeout -k 5s 590m sh -c '")
    assert line.endswith(f"'; echo '{_EXPIRED}'")
    assert "9h50m" not in ctx
    assert "9h" not in line


def test_claude_cursor_and_codex_stay_the_bare_pipeline(monkeypatch, capsys, tmp_path):
    for harness in ("claude", "cursor", "codex"):
        ctx, _inbox = _context(monkeypatch, capsys, harness, tmp_path)
        assert "timeout" not in ctx
        assert "MESH WAKE EXPIRED" not in ctx
        assert "tail -n 0 -F" in ctx
        assert "dm_notify_filter" in ctx


def _coreutils_timeout() -> bool:
    """A coreutils timeout that honors ``-k`` and a duration.

    GNU and uutils both do. A macOS stand-in returns at once, so the
    elapsed check would fail for the wrong reason.
    """
    if shutil.which("timeout") is None:
        return False
    proc = subprocess.run(
        ["timeout", "--version"],
        capture_output=True,
        text=True,
    )
    text = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
    return proc.returncode == 0 and "coreutils" in text


def test_a_two_second_cap_prints_exactly_one_expired_line(monkeypatch, capsys, tmp_path):
    """Run the emitted command with the duration swapped to 2s.

    The scratch inbox is empty, so the filter prints nothing. After the
    timeout, the shell echoes the re-arm line once.
    """
    ctx, inbox = _context(monkeypatch, capsys, "grok", tmp_path)
    script = _timeout_line(ctx).replace("590m", "2s", 1)
    assert "590m" not in script
    assert inbox.is_file()
    if not _coreutils_timeout():
        pytest.skip("the 2s run needs a coreutils timeout")
    started = time.monotonic()
    proc = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - started
    expired = [line for line in proc.stdout.splitlines() if "MESH WAKE EXPIRED" in line]
    assert expired == [_EXPIRED]
    assert elapsed >= 1.5
