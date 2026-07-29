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

def test_every_output_path_in_the_daemon_goes_through_the_guard():
    """>>> A SYMBOL GREP MEASURES PRESENCE, NOT COVERAGE. <<<

    The first version of the cp1252 guard shipped covering 1 output call out of 13.
    `grep -c _print_safe` returned 1 on it — the same answer it returns now, with all
    13 covered. Any check that asks "is the guard there?" passes on both, so this asks
    the only question that distinguishes them: IS THERE AN UNGUARDED `print`?

    Walks the AST rather than the text so a `print` reached through a differently
    formatted call, a new function, or a future author's copy-paste is caught. Same
    reason the merge-check no-merge guard is behavioural and not a substring scan:
    the claim is about what the module DOES, and text is a proxy for that.
    """
    import ast
    from pathlib import Path

    import swarph_cli.commands.daemon as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    guard = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_print_safe")
    allowed = {id(n) for n in ast.walk(guard)}

    unguarded = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "print" and id(n) not in allowed
    ]
    assert not unguarded, (
        f"unguarded print() at daemon.py lines {unguarded} — on a cp1252 console an "
        f"arrow or emoji in that line raises UnicodeEncodeError and takes the loop with it"
    )


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
