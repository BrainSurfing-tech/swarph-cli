"""A console that cannot print a DM must not stop the mesh delivering it.

MEASURED on workstation-lc 2026-07-29 — French Windows, cp1252 console, swarph-cli
0.39.4, Python 3.14, launched by Task Scheduler via `cmd.exe /c`:

    [swarph-daemon] iteration error (continuing): UnicodeEncodeError:
      'charmap' codec can't encode character '→' in position 65

`_log_dm` printed a HARDCODED U+2192 to a charmap stdout. Every DM raised; every
`_drain_iteration` aborted; THE CURSOR FROZE AT last_msg_id=10050 AND ZERO DMS WERE
DELIVERED. The outer handler swallowed it as "iteration error (continuing)", so
Get-ScheduledTask said Running, the process list looked healthy, and every surface was
green. >>> THE CELL WAS DM-BLIND AND NOTHING ANYWHERE SAID SO. <<<

Note what was NOT at risk: inbox.log was already opened with encoding="utf-8", so the
audit trail was intact throughout. Only the OPERATOR-VISIBLE line was unprintable — and
it took the delivery loop down with it. Hence the ordering fix: audit first, display
best-effort.
"""
import io
import json

import pytest

from swarph_cli.commands import daemon


class _Cp1252Stdout(io.TextIOBase):
    """A console that can only encode cp1252 — a French Windows box, faithfully."""

    encoding = "cp1252"

    def __init__(self):
        self.written = []

    def write(self, s):
        s.encode("cp1252")          # raises UnicodeEncodeError exactly as Windows does
        self.written.append(s)
        return len(s)

    def flush(self):
        pass


class _State:
    def __init__(self, tmp_path):
        self.inbox_log_path = tmp_path / "inbox.log"


ARROW_DM = {"id": 1, "created_at": "2026-07-29T02:00Z", "from_node": "lab-ovh",
            "kind": "answer", "content": "see -> the thing → and été"}


def test_no_hardcoded_non_ascii_in_the_operator_line():
    """The separator was ours, not the DM's — so it failed on EVERY message, not only
    ones containing an arrow. An ASCII separator removes the guaranteed-failure case."""
    import inspect
    src = inspect.getsource(daemon._log_dm)
    body = src.split('"""')[-1]          # skip the docstring, which describes the bug
    assert "→" not in body, "operator line must not hardcode non-ASCII"


def test_cp1252_console_does_not_stop_delivery(tmp_path, monkeypatch):
    """THE LOAD-BEARING TEST. A console that refuses the content must cost a mangled
    character, never a poll iteration."""
    fake = _Cp1252Stdout()
    monkeypatch.setattr(daemon.sys, "stdout", fake)
    daemon._log_dm(_State(tmp_path), ARROW_DM)        # must NOT raise
    assert fake.written, "should still have written something to the console"


def test_audit_trail_is_written_even_when_the_console_fails(tmp_path, monkeypatch):
    """Ordering is the fix: audit FIRST, display second. Losing a console line is
    cosmetic; losing the cursor is a silent outage."""
    monkeypatch.setattr(daemon.sys, "stdout", _Cp1252Stdout())
    st = _State(tmp_path)
    daemon._log_dm(st, ARROW_DM)
    rows = [json.loads(l) for l in st.inbox_log_path.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["id"] == 1
    assert rows[0]["content"] == ARROW_DM["content"], "audit must be byte-faithful"


def test_broken_stdout_does_not_stop_delivery(tmp_path, monkeypatch):
    """A detached service or a full disk closes stdout. Still not a reason to stop."""
    class _Broken(io.TextIOBase):
        encoding = "utf-8"
        def write(self, s): raise OSError("stdout is closed")
        def flush(self): pass

    monkeypatch.setattr(daemon.sys, "stdout", _Broken())
    daemon._log_dm(_State(tmp_path), ARROW_DM)        # must NOT raise
    assert (tmp_path / "inbox.log").exists()


def test_utf8_console_is_unaffected(tmp_path, monkeypatch, capsys):
    """The common path must not be degraded by the fallback."""
    daemon._log_dm(_State(tmp_path), ARROW_DM)
    out = capsys.readouterr().out
    assert "id=1" in out and "lab-ovh" in out


# ── COVERAGE, not presence — droplet's finding on PR #158 ────────────────────

def test_no_unguarded_print_in_ANY_module_the_daemon_loop_reaches():
    """>>> A SYMBOL GREP MEASURES PRESENCE, NOT COVERAGE — AND SO DID THE FIRST VERSION
        OF THIS TEST, WHICH WALKED ONE MODULE. <<<

    History, because it is the whole lesson:
      1. The cp1252 guard shipped covering 1 output call out of 13 in daemon.py.
         `grep -c _print_safe` returned 1 on it — the same answer it returns now.
      2. This test was added, walking daemon.py. It closed those 12.
      3. droplet then found a bare print in the PUBLISHED 0.40.1 artifact at
         delivery_queue.py:43, and a second lived at stall_alert.py:49. BOTH ON ERROR
         PATHS INTERPOLATING AN EXCEPTION MESSAGE — the exact crash shape.

    The guard was sited INSIDE daemon.py, so the modules daemon IMPORTS could not use
    it. THE GUARD'S HOME WAS ITSELF A COVERAGE BOUNDARY, and a test scoped to the
    guard's own module could never see past it — it reported full coverage of the
    place the bug was not.

    So this walks every module the loop can reach, and the LIST is the assertion: a new
    daemon-reachable module must be added here or it is unaudited by construction.
    """
    import ast
    from pathlib import Path

    import swarph_cli

    root = Path(swarph_cli.__file__).parent
    # Every module the daemon's poll loop can execute output from.
    REACHED = [
        "commands/daemon.py",
        "delivery_queue.py",
        "stall_alert.py",
        "session_bridge.py",
    ]

    offenders = {}
    for rel in REACHED:
        path = root / rel
        assert path.exists(), f"{rel} listed as daemon-reachable but does not exist"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bare = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print"
        ]
        if bare:
            offenders[rel] = bare

    assert not offenders, (
        f"unguarded print() on a daemon-reachable path: {offenders} — on a cp1252 "
        f"console an arrow or emoji in that line raises UnicodeEncodeError, and if the "
        f"line is inside an except block the raise escapes the try and kills the loop"
    )


def test_the_guard_itself_is_the_only_place_a_bare_print_is_allowed():
    """The implementation must contain exactly one bare print — the one it wraps.

    Guards the degenerate fix for the test above: routing everything through a
    `print_safe` that no longer prints would pass a coverage walk while emitting
    nothing at all. Silence is the failure mode this whole feature exists to end.
    """
    import ast
    from pathlib import Path

    from swarph_cli import console_safe

    tree = ast.parse(Path(console_safe.__file__).read_text(encoding="utf-8"))
    bare = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print"]
    assert len(bare) == 1, f"expected exactly one real print in the guard, found {bare}"


def test_the_delivery_error_handler_survives_a_character_it_cannot_render():
    """THE CRASH droplet MEASURED: a raise inside an `except` propagates past the
    `try` entirely, so the handler whose comment promises it never crashes the loop
    was itself the crash — reached only when something has ALREADY gone wrong, i.e.
    exactly when the text is most likely to carry an arrow or an emoji.

    Not a test of the happy DM path: that path was already guarded, so a clean
    non-ASCII DM round-trip PASSES while this crash survives. The failure path is
    the one that has to be driven.
    """
    import io

    from swarph_cli.commands import daemon as mod

    class Cp1252Stream(io.StringIO):
        encoding = "cp1252"

        def write(self, s):  # raises exactly where a French Windows console raises
            s.encode("cp1252")
            return super().write(s)

    for payload in ("plain ascii", "arrow -> →", "emoji \U0001f512", "check ✓"):
        stream = Cp1252Stream()
        try:
            raise RuntimeError(payload)
        except RuntimeError as exc:
            mod._print_safe(
                f"[swarph-daemon] delivery error (continuing): "
                f"{type(exc).__name__}: {exc}",
                stream=stream,
            )  # must not raise — that IS the bug

    # canary: the model must still be able to fail, or this file asserts nothing
    import pytest
    with pytest.raises(UnicodeEncodeError):
        Cp1252Stream().write("→")
