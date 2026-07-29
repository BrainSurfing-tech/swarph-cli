"""END-TO-END: the CLI must survive an 8-bit console with a redirected stdout.

>>> THIS IS THE GAP THE WINDOWS CI LEGS CANNOT COVER, AND THE REASON IS STRUCTURAL. <<<
Under pytest, `sys.stdout` is a TextIOWrapper with encoding utf-8 and isatty()==False
ON EVERY PLATFORM — capture substitutes the very object that fails. So a
console-encoding fault is unreachable from inside a captured test, and adding more
Windows runners would not have changed that. MEASURED: pytest stdout is utf-8 on Linux
and on windows-latest alike.

These tests therefore spawn a REAL SUBPROCESS with a REAL 8-bit stdout, which is the
condition workstation-lc hit: French Windows (cp1252) + Task Scheduler, which REDIRECTS
stdout — and a redirected stream on Windows uses locale.getpreferredencoding(), not the
UTF-16 console API. `PYTHONIOENCODING=cp1252` reproduces that exactly, on Linux, in CI,
with no French Windows anywhere.

WHAT IT COST WHEN NOTHING CHECKED THIS: a hardcoded U+2192 in `_log_dm` raised on every
DM, `_drain_iteration` aborted, THE CURSOR FROZE AND ZERO DMS WERE DELIVERED — while
Get-ScheduledTask reported Running and every other surface was green.
"""
import os
import subprocess
import sys
import textwrap

import pytest

# The exact shape of the operator line that failed, plus content a French cell sends.
_HOSTILE = "id=1 from=lab-ovh kind=answer -> 'réponse — arrow → and emoji ✅'"


def _run(code: str, encoding: str) -> subprocess.CompletedProcess:
    """Run in a subprocess with a REDIRECTED, 8-bit stdout — Task Scheduler's shape."""
    # THE PARENT MUST DECODE WHAT THE CHILD ACTUALLY WROTE. `text=True` decodes with
    # the PARENT's locale (utf-8 here), so it raises on the child's cp1252 bytes — the
    # harness for an encoding test having an encoding bug. Decode explicitly, and
    # errors="replace" so a mangled byte is data rather than an exception.
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, timeout=60,
        encoding=encoding, errors="replace",
        # INHERIT the real environment and override only what this test controls.
        # An earlier version passed env={"PATH": "/usr/bin:/bin", ...} — a POSIX path,
        # in the test written to prove we handle a WINDOWS console. It failed both
        # Windows legs: the interpreter cannot resolve, and SYSTEMROOT/COMSPEC are
        # required for a subprocess there. >>> A CROSS-PLATFORM TEST THAT HARDCODES ONE
        # PLATFORM'S PATH TESTS THE PLATFORM IT RUNS ON, WHICH IS THE ONE THAT WAS
        # ALREADY FINE. <<<
        env={**os.environ, "PYTHONIOENCODING": encoding, "PYTHONPATH": "src"},
    )


def test_the_bug_reproduces_without_the_guard():
    """Sanity: a bare print of non-ASCII to cp1252 DOES fail. If this ever stops
    failing, this whole file is testing nothing and must be re-examined."""
    r = _run(f"print({_HOSTILE!r})", "cp1252")
    assert r.returncode != 0
    assert "UnicodeEncodeError" in r.stderr


def test_log_dm_survives_a_cp1252_stdout(tmp_path):
    """THE GUARD. The delivery path must not raise on a console that cannot represent
    the DM — the audit trail is written first and the operator line degrades."""
    r = _run(f"""
        import sys
        from swarph_cli.commands import daemon
        class S:
            inbox_log_path = __import__('pathlib').Path({str(tmp_path / 'inbox.log')!r})
        daemon._log_dm(S(), {{'id': 1, 'created_at': 'now', 'from_node': 'lab-ovh',
                              'kind': 'answer',
                              'content': 'réponse — arrow → and emoji ✅'}})
        print("SURVIVED")
    """, "cp1252")
    assert r.returncode == 0, f"delivery path died on an 8-bit console:\n{r.stderr}"
    assert "SURVIVED" in r.stdout


def test_the_audit_trail_is_byte_faithful_under_cp1252(tmp_path):
    """The console may mangle; the record may not. inbox.log is utf-8 explicitly."""
    log = tmp_path / "inbox.log"
    content = "réponse — arrow → and emoji ✅"
    r = _run(f"""
        from pathlib import Path
        from swarph_cli.commands import daemon
        class S:
            inbox_log_path = Path({str(log)!r})
        daemon._log_dm(S(), {{'id': 7, 'created_at': 'now', 'from_node': 'x',
                              'kind': 'answer', 'content': {content!r}}})
    """, "cp1252")
    assert r.returncode == 0, r.stderr
    import json
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["content"] == content, "the audit trail lost characters the console could not print"
