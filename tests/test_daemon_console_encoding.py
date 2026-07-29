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
