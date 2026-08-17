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


def test_it_overrides_the_CLI_methods_while_keeping_claude_plumbing():
    """>>> I ASSERTED THE WRONG THING FIRST. <<<

    The original version asserted `not issubclass(MuseMembrane, ClaudeMembrane)`
    — treating the INHERITANCE as the defect. It was the MECHANISM. The defect
    was "launches the claude binary", and re-parenting also threw away launch()'s
    chdir/exec-replace and the assisted-memory restore, which are
    provider-AGNOSTIC. Four existing tests failed instantly and were right.

    So the property is: the CLI-shaped methods are muse's OWN, and everything
    else is still inherited.
    """
    own = vars(spawn.MuseMembrane)
    for method in ("build_argv", "resolve_binary", "binary_not_found_message"):
        assert method in own, f"{method} must be muse's own, not claude's"
    assert "launch" not in own, "launch() is provider-agnostic plumbing; do not fork it"
    assert issubclass(spawn.MuseMembrane, spawn.ClaudeMembrane)


def test_a_workspace_with_NO_prior_session_starts_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    assert _argv(_cell(tmp_path)) == ["muse", "--disable-sandbox"]


def test_a_workspace_WITH_a_prior_session_resumes_it(tmp_path, monkeypatch):
    """`--last`, never bare `resume` — bare opens the picker."""
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, str(tmp_path))))
    argv = _argv(_cell(tmp_path))
    assert argv == ["muse", "--disable-sandbox", "resume", "--last"]


def test_ANOTHER_workspaces_session_does_not_trigger_resume(tmp_path, monkeypatch):
    """>>> THE CONTROL THAT MAKES THE RESUME TEST MEAN SOMETHING. <<<

    An implementation that resumed whenever the index held ANY row would pass
    the test above. muse's `--last` is scoped to THIS workspace, so resuming on
    a foreign row would hand the cell somebody else's conversation — and it
    would look like a working resume.
    """
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX",
                        str(_index(tmp_path, "/some/other/workspace")))
    assert _argv(_cell(tmp_path)) == ["muse", "--disable-sandbox"]


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
    assert _argv(_cell(tmp_path)) == ["muse", "--disable-sandbox"]

    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(tmp_path / "absent.db"))
    assert _argv(_cell(tmp_path)) == ["muse", "--disable-sandbox"]


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
    assert _argv(_cell(tmp_path), ["--model", "x"]) == [
        "muse", "--disable-sandbox", "--model", "x",
    ]


def test_disable_sandbox_is_ALWAYS_passed_because_the_sandbox_is_broken(
    tmp_path, monkeypatch,
):
    """>>> NOT A POSTURE CHOICE. <<< muse's OS sandbox is broken; a cell
    launched with it enabled does not become a working cell. The flag is
    therefore unconditional on BOTH paths — a resume-only or fresh-only
    injection would leave the other path still dead.

    When the sandbox works, delete this test and the flag together. Do not
    "complete the pattern" by adding `--yolo` / `--disable-approval` to match.
    """
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    assert _argv(_cell(tmp_path))[1] == "--disable-sandbox"
    monkeypatch.setattr(
        spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, str(tmp_path)))
    )
    argv = _argv(_cell(tmp_path))
    assert argv[:3] == ["muse", "--disable-sandbox", "resume"]


def test_safety_defaults_are_NOT_weakened(tmp_path, monkeypatch):
    """Approval stays ON. `--yolo` disables approval AND sandbox together;
    `--disable-approval` / `--trust-workspace` are the other two widenings.

    `--disable-sandbox` is the known-broken exception (see the test above) and
    is deliberately NOT in this list. The property here is: we did not use the
    sandbox workaround as cover to relax the rest.
    """
    monkeypatch.setattr(spawn, "_MUSE_SESSION_INDEX", str(_index(tmp_path, None)))
    argv = _argv(_cell(tmp_path))
    for unsafe in ("--yolo", "--disable-approval", "--trust-workspace"):
        assert unsafe not in argv, f"the membrane injected {unsafe}"


def test_the_stamp_still_applies_to_muse(tmp_path):
    """#360 must reach this lane too — it is a new membrane on the base."""
    env = spawn.MEMBRANES["muse"].spawn_env(_cell(tmp_path, "meta-muse"))
    assert env["SWARPH_SELF"] == "meta-muse"
    assert env["SWARPH_SPAWN"] == "1"


# --- THE INJECTION SEAM, hoisted out of run_spawn's if/elif chain -----------
def test_muse_does_NOT_get_claudes_append_system_prompt(tmp_path):
    """>>> THE BUG THIS PR ALMOST SHIPPED. <<<

    MuseMembrane subclasses ClaudeMembrane for its launch plumbing, and
    run_spawn decided the assisted-memory injection with
    `isinstance(membrane, ClaudeMembrane)`. So muse matched claude's branch and
    was handed `--append-system-prompt` — A FLAG muse WOULD REJECT.

    An isinstance test answers "what is this built from", never "what does this
    accept". The two stopped agreeing the moment a membrane reused another's
    plumbing.
    """
    argv = ["muse"]
    spawn.MEMBRANES["muse"].apply_task_injection(_cell(tmp_path), argv, "TASK")
    assert "--append-system-prompt" not in argv
    assert argv == ["muse", "TASK"], f"expected a POSITIONAL prompt, got {argv}"


def test_claude_still_gets_its_flag(tmp_path):
    """>>> THE CONTROL. <<< Hoisting the chain must not quietly disable the
    injection for the provider it already worked for."""
    argv = ["claude"]
    spawn.MEMBRANES["claude"].apply_task_injection(_cell(tmp_path), argv, "TASK")
    assert argv == ["claude", "--append-system-prompt", "TASK"]


def test_muse_RESUME_gets_no_positional_prompt(tmp_path):
    """`muse resume` takes no prompt. Appending one would be a CLI error, so the
    restored task cannot reach a resumed session — stated as a limitation rather
    than silently producing an argv the binary rejects."""
    argv = ["muse", "resume", "--last"]
    spawn.MEMBRANES["muse"].apply_task_injection(_cell(tmp_path), argv, "TASK")
    assert argv == ["muse", "resume", "--last"]


def test_every_membrane_declares_how_it_receives_an_injection():
    """The surface property. A provider added later inherits the BASE — which
    delivers NOTHING — so it is inert rather than mis-injected with a flag
    borrowed from whichever membrane it happened to subclass.
    """
    base = spawn.ProviderMembrane.apply_task_injection
    inherits_base = [
        name for name, m in spawn.MEMBRANES.items()
        if type(m).apply_task_injection is base
    ]
    assert spawn.MEMBRANES, "empty registry — the enumeration is broken"
    # vibe legitimately has none yet; assert the KNOWN lanes declare one.
    for lane in ("claude", "codex", "antigravity", "grok", "muse"):
        assert lane not in inherits_base, (
            f"{lane} silently inherits the no-op injection — its restored task "
            f"would never arrive"
        )
