"""Card #725 / #125 — CLI stdout must survive a non-UTF-8 sink.

Razorpeter 2026-09-05: `swarph channel read` on PowerShell 5.1 raises
UnicodeEncodeError when any post is non-ASCII (ACP=1252). `--json` survives
via ensure_ascii. Distinct from #724 (.ps1) and subprocess encoding=.

Razorpeter 32116: `chcp` is a NO-OP for Python (ACP, not OEMCP). Do NOT use
chcp as a test lever — it false-passes both ways. The portable harness is
`io.TextIOWrapper(..., encoding="cp1252")` + `redirect_stdout`: no Windows,
no registry, no false-pass on UTF-8-Beta boxes. `errors="replace"` is
load-bearing for redirected sinks that never get reconfigure().
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import textwrap

from swarph_cli.commands.channel import _format_channel_messages
from swarph_cli.console_safe import configure_stdio, print_safe

_HOSTILE = "cafe - naive resume \u4e2d\u6587 \u2705 \u20ac42 arrow \u2192 em \u2014"
_PAYLOAD = {
    "messages": [
        {
            "id": 24142,
            "from_node": "workstation-lc",
            "created_at": "2026-08-17T00:00:00Z",
            "content": _HOSTILE,
        }
    ]
}


def _run(code: str, encoding: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        timeout=60,
        encoding=encoding,
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": encoding, "PYTHONPATH": "src"},
    )


def test_bare_print_to_cp1252_wrapper_raises():
    """CAN-FAIL control: a bare print into a cp1252 sink dies. No Windows needed."""
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    raised = False
    try:
        with contextlib.redirect_stdout(buf):
            print(_HOSTILE)
    except UnicodeEncodeError:
        raised = True
    assert raised, "control must raise — otherwise this file tests nothing"


def test_channel_format_print_safe_survives_cp1252_redirect():
    """Portable guard (razorpeter 32116): redirect_stdout to cp1252, must not raise."""
    raw = io.BytesIO()
    buf = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    with contextlib.redirect_stdout(buf):
        print_safe(_format_channel_messages(_PAYLOAD))
    buf.flush()
    out = raw.getvalue()
    assert out, "errors=replace must emit something rather than nothing"
    # ASCII scaffold of the formatter survives even when glyphs are replaced
    assert b"24142" in out
    assert b"workstation-lc" in out


def test_configure_stdio_lets_hostile_print_exit_0_under_pythonioencoding_cp1252():
    """Entry reconfigure covers the stock Windows console path (ACP=1252)."""
    r = _run(
        f"""
        from swarph_cli.console_safe import configure_stdio
        configure_stdio()
        print({_HOSTILE!r})
        print("SURVIVED")
        """,
        "cp1252",
    )
    assert r.returncode == 0, r.stderr
    assert "SURVIVED" in r.stdout


def test_main_calls_configure_stdio_before_verb_dispatch():
    r = _run(
        f"""
        from swarph_cli import main as main_mod
        def _fake_verb(rest):
            print({_HOSTILE!r})
            print("SURVIVED")
            return 0
        main_mod._VERB_HANDLERS["__encoding_probe__"] = "unused"
        orig = main_mod._dispatch_verb
        main_mod._dispatch_verb = lambda verb, rest: _fake_verb(rest)
        try:
            rc = main_mod.main(["__encoding_probe__"])
        finally:
            main_mod._dispatch_verb = orig
            main_mod._VERB_HANDLERS.pop("__encoding_probe__", None)
        raise SystemExit(rc)
        """,
        "cp1252",
    )
    assert r.returncode == 0, r.stderr
    assert "SURVIVED" in r.stdout
