"""MuseMembrane must launch MUSE, not Claude.

>>> IT USED TO SUBCLASS ClaudeMembrane WITH ONLY ``name = "muse"``. <<< So
`swarph spawn` on a muse cell started the ``claude`` binary wearing muse's mesh
identity, while the real cell on this box ran ``muse-bin`` started by hand. Mesh
attribution said muse; the process was Claude Code. A label, not a membrane.

The two CLIs are not interchangeable, and the difference is structural:

  * claude PINS a session UUID at creation (``--session-id``). MUSE HAS NO SUCH
    FLAG — it resumes via a ``resume`` SUBCOMMAND. So muse belongs on codex's
    resume-by-discovery pattern, and subclassing ClaudeMembrane was wrong about
    more than the binary path.
  * ``muse resume`` WITH NO ARGUMENT OPENS AN INTERACTIVE PICKER. On a spawned
    cell there is nobody to pick: it hangs forever, and a hung pane looks alive.
"""
from __future__ import annotations

import sqlite3
import types
from pathlib import Path

from swarph_cli.commands import spawn


def _cell(cwd: Path, name: str = "meta-muse"):
    return types.SimpleNamespace(name=name, cwd=cwd)


def _argv(cell, passthrough=None):
    return spawn.MEMBRANES["muse"].build_argv(
        cell, session_id=None, no_starter=True,
        passthrough=passthrough or [], effective_role=None,
    )


_INDEX_SEQ = [0]


def _index(tmp_path: Path, workspace_root: str | None) -> Path:
    """A minimal stand-in for muse's own session index.

    A FRESH FILE PER CALL. The first version reused one path, so a test that
    built two indexes hit `table sessions already exists` — the helper carried
    state between the cases it was meant to isolate.
    """
    _INDEX_SEQ[0] += 1
    db = tmp_path / f"session-index-{_INDEX_SEQ[0]}.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (session_id TEXT, workspace_root TEXT)")
    if workspace_root is not None:
        con.execute("INSERT INTO sessions VALUES ('sid-1', ?)", (workspace_root,))
    con.commit()
    con.close()
    return db


def test_the_membrane_targets_the_MUSE_binary_not_claude(monkeypatch):
    """>>> THE HEADLINE. <<< Asserted on the resolver, not on argv, because a
    membrane that built muse-shaped argv while resolving `claude` would still
    launch Claude Code."""
    seen = []
    monkeypatch.setattr(spawn.shutil, "which", lambda n: seen.append(n) or f"/usr/bin/{n}")
    assert spawn.MEMBRANES["muse"].resolve_binary() == "/usr/bin/muse"
    assert seen == ["muse"], f"looked for {seen}, not 'muse'"


def test_it_is_no_longer_a_claude_subclass():
    """The defect was inheritance, so the fix is asserted against inheritance.

    A future refactor that re-parents this to ClaudeMembrane would silently
    restore claude's binary and claude's --session-id assumption at once.
    """
    assert not issubclass(spawn.MuseMembrane, spawn.ClaudeMembrane)
    assert issubclass(spawn.MuseMembrane, spawn.ProviderMembrane)


def test_a_workspace_with_NO_prior_session_starts_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    assert _argv(_cell(tmp_path)) == ["muse"]


def test_a_workspace_WITH_a_prior_session_resumes_it(tmp_path, monkeypatch):
    """`--last`, never bare `resume` — bare opens the picker."""
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, str(tmp_path))))
    argv = _argv(_cell(tmp_path))
    assert argv == ["muse", "resume", "--last"]


def test_ANOTHER_workspaces_session_does_not_trigger_resume(tmp_path, monkeypatch):
    """>>> THE CONTROL THAT MAKES THE RESUME TEST MEAN SOMETHING. <<<

    An implementation that resumed whenever the index held ANY row would pass
    the test above. muse's `--last` is scoped to THIS workspace, so resuming on
    a foreign row would hand the cell somebody else's conversation — and it
    would look like a working resume.
    """
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX",
                        str(_index(tmp_path, "/some/other/workspace")))
    assert _argv(_cell(tmp_path)) == ["muse"]


def test_an_UNREADABLE_index_starts_fresh_rather_than_risking_the_picker(tmp_path, monkeypatch):
    """>>> THE FAILURE DIRECTION, AND IT IS THE WHOLE SAFETY ARGUMENT. <<<

    A missing/locked/corrupt index means "I cannot tell". Guessing `resume`
    there risks `--last` failing, or worse a bare picker that HANGS THE PANE
    while looking alive. Guessing `fresh` costs continuity, which is
    recoverable. Cannot-evaluate must resolve toward the recoverable side.
    """
    broken = tmp_path / "not-a-database.db"
    broken.write_text("this is not sqlite", encoding="utf-8")
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(broken))
    assert _argv(_cell(tmp_path)) == ["muse"]

    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(tmp_path / "absent.db"))
    assert _argv(_cell(tmp_path)) == ["muse"]


def test_bare_resume_is_NEVER_emitted(tmp_path, monkeypatch):
    """Pinned separately from the argv equality tests: bare `resume` is the one
    form that hangs, and it must not appear on any path."""
    for ws in (None, str(tmp_path), "/elsewhere"):
        monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, ws)))
        argv = _argv(_cell(tmp_path))
        if "resume" in argv:
            assert "--last" in argv or len(argv) > argv.index("resume") + 1, (
                f"bare `muse resume` would open an interactive picker: {argv}"
            )


def test_passthrough_is_appended(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    assert _argv(_cell(tmp_path), ["--model", "x"]) == ["muse", "--model", "x"]


def test_safety_defaults_are_NOT_weakened(tmp_path, monkeypatch):
    """muse ships approval and sandboxing ON; `--yolo` disables both.

    A membrane is the wrong place to silently weaken a provider's safety
    posture. An operator who wants it passes it through, where it is visible in
    the spawn command rather than buried in a library default.
    """
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    argv = _argv(_cell(tmp_path))
    for unsafe in ("--yolo", "--disable-approval", "--disable-sandbox", "--trust-workspace"):
        assert unsafe not in argv, f"the membrane injected {unsafe}"


def test_the_stamp_still_applies_to_muse(tmp_path):
    """#360 must reach this lane too — it is a new membrane on the base."""
    env = spawn.MEMBRANES["muse"].spawn_env(_cell(tmp_path, "meta-muse"))
    assert env["SWARPH_SELF"] == "meta-muse"
    assert env["SWARPH_SPAWN"] == "1"
