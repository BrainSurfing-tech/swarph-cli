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

def _daemon_import_closure():
    """Every swarph_cli module the daemon can reach, DERIVED — never listed.

    >>> A HARDCODED LIST ASSERTS THAT ITS MEMBERS ARE CLEAN. IT CANNOT ASSERT THAT ITS
        MEMBERS ARE ALL OF THEM — AND ITS OWN COMPLETENESS IS THE CLAIM THAT MATTERS. <<<
    (droplet, PR #159.) The previous version listed five modules and was right about all
    five, while `multiplexer` sat reachable and unlisted: a future author printing there
    would have failed nothing. A list-as-assertion is only as good as a fact nobody is
    checking.

    Two traversal details, both learned by getting them wrong:

    * `from swarph_cli import session_bridge, stall_alert` is an ImportFrom whose
      SUBMODULES live in `n.names`, not `n.module`. A walk queueing only `n.module`
      resolves it to the package `__init__` and silently drops both — which is how
      droplet's first closure reported 4 modules and omitted the two under repair.
      >>> HIS INSTRUMENT HAD THE DEFECT IT WAS MEASURING: confidently correct about a
      smaller domain, exactly like a guard sited inside the module it audits. <<<
    * `daemon.py` imports `commands.onboard` INSIDE A FUNCTION BODY. Walking only
      top-level imports misses it, so this walks every Import node anywhere in the AST.
    """
    import ast
    from pathlib import Path

    import swarph_cli

    pkg_root = Path(swarph_cli.__file__).parent.parent

    def to_path(mod):
        rel = mod.replace(".", "/")
        for cand in (pkg_root / f"{rel}.py", pkg_root / rel / "__init__.py"):
            if cand.exists():
                return cand
        return None

    seen, queue = {}, ["swarph_cli.commands.daemon"]
    while queue:
        mod = queue.pop()
        if mod in seen or not mod.startswith("swarph_cli"):
            continue
        path = to_path(mod)
        if path is None:
            continue
        seen[mod] = path
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                queue += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                queue.append(n.module)
                queue += [f"{n.module}.{a.name}" for a in n.names]
    return seen


def test_no_unguarded_print_anywhere_the_daemon_can_reach():
    """>>> A SYMBOL GREP MEASURES PRESENCE, NOT COVERAGE — AND THE FIRST TWO VERSIONS OF
        THIS TEST MEASURED THE WRONG DOMAIN, EACH TIME CONFIDENTLY. <<<

    The history is the lesson, and every step passed its own check:
      1. The guard shipped covering 1 output call of 13. `grep -c` returned 1.
      2. A test walking daemon.py closed those 12 — and reported FULL COVERAGE while
         `delivery_queue.py:43` and `stall_alert.py:49` sat unguarded in the PUBLISHED
         artifact. The guard was sited inside daemon.py, so the modules daemon IMPORTS
         could not import it back: THE GUARD'S HOME WAS A COVERAGE BOUNDARY, and the
         audit inherited its scope from the thing it audited.
      3. A hardcoded five-module list closed those two — and could not see that
         `multiplexer` was reachable and unlisted.
    Each fix was correct and each left the same hole one level up, because the SCOPE was
    asserted by the same hand that wrote the claim. So the scope is now DERIVED from the
    import graph: a new reachable module joins the closure whether or not anyone
    remembers it exists.

    Why this matters more than the individual lines: on a cp1252 console an arrow or an
    emoji raises UnicodeEncodeError, and a raise inside an `except` escapes the `try`
    entirely — so an error handler becomes the crash, on precisely the text most likely
    to carry such a character (an interpolated exception message).
    """
    import ast

    from swarph_cli import console_safe

    closure = _daemon_import_closure()
    assert len(closure) >= 6, f"closure implausibly small ({sorted(closure)}) — walk is broken"

    offenders = {}
    for mod, path in closure.items():
        if mod == console_safe.__name__:
            continue  # the implementation; pinned by its own test below
        bare = [n.lineno for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "print"]
        if bare:
            offenders[mod] = bare

    assert not offenders, (
        f"unguarded print() reachable from the daemon: {offenders} — route it through "
        f"swarph_cli.console_safe.print_safe"
    )


def test_the_closure_walk_sees_the_two_import_forms_that_hid_modules():
    """Non-vacuity for the WALK, not just for the assertion it feeds.

    A closure test is only as honest as its traversal, and both forms below were
    genuinely missed by a real tool earlier today. If either regresses, the coverage
    test above keeps passing while quietly measuring a smaller world — the exact
    failure this whole file has now hit three times.
    """
    closure = _daemon_import_closure()
    # `from swarph_cli import session_bridge, stall_alert` — submodules in n.names
    assert "swarph_cli.stall_alert" in closure, "ImportFrom submodule names not traversed"
    assert "swarph_cli.session_bridge" in closure
    # function-local `from swarph_cli.commands.onboard import _resolve_token`
    assert "swarph_cli.commands.onboard" in closure, "function-local imports not traversed"


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
