"""Card #725 / #125 — CLI stdout must survive a non-UTF-8 Windows console.

Razorpeter 2026-09-05: `swarph channel read` on PowerShell 5.1 / IBM437 with
swarph 0.54.0 raises UnicodeEncodeError inside the single print() of the whole
batch when one post carries non-ASCII — exit 1, NOTHING printed. `--json` works
because ensure_ascii escapes. Distinct from #724 (.ps1 decode) and subprocess
encoding= (#725 list): this is OUR stdout ENCODE.

configure_stdio() at CLI entry reconfigures stdout/stderr to utf-8 + replace.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_HOSTILE = "cafe - naive resume \u4e2d\u6587 \u2705 \u20ac42 arrow \u2192"


def _run(code: str, encoding: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        timeout=60,
        encoding=encoding,
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": encoding, "PYTHONPATH": "src"},
    )


def test_bare_print_still_fails_on_cp1252_without_reconfigure():
    """CAN-FAIL control: without the guard, non-ASCII print dies on cp1252."""
    r = _run(f"print({_HOSTILE!r})", "cp1252")
    assert r.returncode != 0
    assert "UnicodeEncodeError" in r.stderr


def test_configure_stdio_lets_hostile_print_exit_0_on_cp1252():
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


def test_main_entry_reconfigures_before_verb_dispatch():
    """main() must call configure_stdio before any verb can print."""
    r = _run(
        f"""
        import sys
        from swarph_cli.main import main
        # Simulate a tiny verb that prints hostile text after entry.
        from swarph_cli import main as main_mod
        def _fake_verb(argv):
            print({_HOSTILE!r})
            print("SURVIVED")
            return 0
        main_mod._VERB_HANDLERS["__encoding_probe__"] = "unused"
        # Patch dispatch path used after configure_stdio
        orig = main_mod._dispatch_verb
        def _dispatch(verb, rest):
            return _fake_verb(rest)
        main_mod._dispatch_verb = _dispatch
        try:
            rc = main(["__encoding_probe__"])
        finally:
            main_mod._dispatch_verb = orig
            main_mod._VERB_HANDLERS.pop("__encoding_probe__", None)
        raise SystemExit(rc)
        """,
        "cp1252",
    )
    assert r.returncode == 0, r.stderr
    assert "SURVIVED" in r.stdout
