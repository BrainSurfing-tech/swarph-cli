"""CursorMembrane — the Cursor Agent CLI as a durable swarph CELL.

Grounded against real `cursor-agent 2026.08.11` on 2026-08-17 by EXECUTION, then
confirmed against the shipped bundle's source. The probes that decided the design,
because two of them overturned the answer an analogy would have given:

  · `CURSOR_DATA_DIR=<tmp> agent status` -> "✓ Logged in"      (state isolates)
  · `HOME=<tmp> agent status`            -> "Not logged in"    (auth does NOT
    follow the data dir: `getAuthFilePath` reads $XDG_CONFIG_HOME/cursor)
    => isolate the DATA DIR, never $HOME and never $XDG_CONFIG_HOME.
  · `agent --continue` on a virgin data dir -> EXIT 1, "No previous chats found."
    Vendor source: `--continue` is rewritten to `resume: "-1"` -> getLatestChatId()
    -> exit(1) when empty. => --continue MUST be guarded, unlike agy's, whose
    unconditional `--continue` starts fresh gracefully.
  · non-interactive run in an untrusted workspace -> "Pass --trust, --yolo, or -f"
    => --trust is the NARROW answer; withholding it leaves only the wide ones.
  · chats live at `<data>/chats/<md5(resolved cwd)>/` — md5 verified against the
    live dir name for /home/ubuntu, not guessed.

>>> AND THE RELEASE ORDER IS LOAD-BEARING, per the #247 outage: the membrane ships
in a RELEASED swarph-cli (registered + CLI_ENABLED_PROVIDERS) BEFORE `cursor`
enters swarph_shared.VALID_PROVIDERS. spawn.py's guard is a SUBSET check, so this
direction is inert; reversed, it raises AT IMPORT and kills `swarph spawn` for
every fresh install. <<<
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarph_cli.cell import CLI_ENABLED_PROVIDERS, CellError, load_cell
from swarph_cli.commands.spawn import (
    MEMBRANES,
    CursorMembrane,
    _build_cursor_argv,
    _cursor_data_dir,
    _cursor_env,
    _cursor_has_prior_chat,
    _cursor_sandbox,
    _scrub_cursor_namespace,
    _validate_routing,
    _CURSOR_CELL_DATA_SUBDIR,
)


def _cell(tmp_path, starter=None, sandbox=None):
    return types.SimpleNamespace(
        cwd=tmp_path,
        name="cursor-1",
        provider="cursor",
        role="worker",
        starter_prompt_path=starter,
        sandbox=sandbox,
        extra={},
    )


def _seed_chat(cell) -> Path:
    """Create the chat dir cursor itself would create for this cwd."""
    digest = hashlib.md5(str(Path(cell.cwd).resolve()).encode("utf-8")).hexdigest()
    chat = _cursor_data_dir(cell) / "chats" / digest / "88f7bf98-a457-4eb5-a596"
    chat.mkdir(parents=True)
    (chat / "store.db").write_text("sqlite")
    return chat


# ── REGISTRATION AND THE ORDERING INVARIANT ─────────────────────────────────

def test_cursor_is_registered_and_the_membrane_is_AHEAD_of_the_whitelist():
    """The safe direction, asserted. An extra membrane is INERT because the guard
    is `VALID_PROVIDERS ⊆ MEMBRANES`; a whitelisted provider with no membrane is
    an import-time crash for every install."""
    from swarph_shared.cell import VALID_PROVIDERS

    assert isinstance(MEMBRANES["cursor"], CursorMembrane)
    assert not (set(VALID_PROVIDERS) - set(MEMBRANES))


def test_cursor_is_enabled_by_the_CLI_so_the_membrane_is_not_inert(tmp_path):
    """Registration alone does nothing: `load_cell` gates on
    CLI_ENABLED_PROVIDERS, so without the enablement a `provider: cursor`
    cell.yaml is rejected and the membrane can never be reached."""
    assert "cursor" in CLI_ENABLED_PROVIDERS
    path = tmp_path / "cell.yaml"
    path.write_text(
        "schema_version: v1\nname: cursor-1\nrole: worker\ncwd: .\nprovider: cursor\n",
        encoding="utf-8",
    )
    assert load_cell(path).provider == "cursor"


def test_cursor_does_NOT_override_pre_launch_it_inherits_the_base_hoist():
    """>>> THIS IS THE LINUX-tmux / WINDOWS-psmux PARITY, AND IT IS INHERITED
    RATHER THAN RE-IMPLEMENTED. <<< Card #2 hoisted `_launch_via_tmux` (and #314
    the Windows Terminal rescue) to the base membrane precisely because
    per-provider overrides WERE the discrimination. Asserted on the class dict:
    inheritance is the property, and calling it would pass either way."""
    from swarph_cli.commands.spawn import ProviderMembrane

    assert "pre_launch" not in CursorMembrane.__dict__
    assert CursorMembrane.pre_launch is ProviderMembrane.pre_launch


def test_cursor_does_not_use_a_pinned_session():
    """Cursor mints and owns its own chat ids. `--new-session-id` exists but is a
    HIDDEN flag AND is mutually exclusive with --continue/--resume (vendor
    source), so the pinned lane would trade a documented surface for an
    undocumented one and rebuild resume. Deferred deliberately."""
    assert MEMBRANES["cursor"].uses_pinned_session() is False


def test_routing_native_cursor_is_accepted_and_a_mismatch_is_refused(tmp_path):
    for routing in ("{}\n", "\n  native: cursor\n"):
        path = tmp_path / f"ok-{len(routing)}.yaml"
        path.write_text(
            "schema_version: v1\nname: cursor-1\nrole: worker\ncwd: .\n"
            f"provider: cursor\nrouting: {routing}",
            encoding="utf-8",
        )
        _validate_routing(load_cell(path))

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: v1\nname: cursor-1\nrole: worker\ncwd: .\n"
        "provider: cursor\nrouting:\n  native: anthropic\n",
        encoding="utf-8",
    )
    with pytest.raises(CellError):
        _validate_routing(load_cell(bad))


# ── THE GENESIS TRAP: --continue IS NOT A FRESH-START FLAG ──────────────────

def test_a_VIRGIN_cell_gets_NO_continue_because_continue_EXITS_1(tmp_path):
    """>>> THE DEFECT THIS MEMBRANE WOULD HAVE SHIPPED BY ANALOGY. <<< agy passes
    `--continue` unconditionally because agy starts fresh gracefully. Cursor does
    NOT: it rewrites --continue to `resume "-1"`, resolves getLatestChatId(), and
    exits 1 with "No previous chats found." (MEASURED: exit code 1 on a virgin
    data dir.) Unconditional, the FIRST spawn of every cursor cell would die
    instantly in a multiplexer pane that then collapses — "session created, no
    agent", indistinguishable from the Windows exec defects."""
    argv = _build_cursor_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert "--continue" not in argv


def test_a_cell_WITH_a_chat_gets_continue_so_continuity_actually_works(tmp_path):
    """The positive leg. Without it, "never emit --continue" would pass the test
    above and silently make every cursor cell amnesiac — a durable cell that
    forgets on every respawn is not durable."""
    cell = _cell(tmp_path)
    _seed_chat(cell)
    assert "--continue" in _build_cursor_argv(cell, no_starter=True, passthrough=[])


def test_the_chat_probe_is_KEYED_ON_THE_CWD_not_just_any_chat_dir(tmp_path):
    """`<data>/chats/<md5(resolved cwd)>/` — md5-of-path is cursor's own keying
    (verified against the live dir name), so a chat belonging to a DIFFERENT
    workspace must not make this cell claim continuity it cannot resume."""
    cell = _cell(tmp_path)
    foreign = _cursor_data_dir(cell) / "chats" / ("0" * 32) / "some-chat"
    foreign.mkdir(parents=True)
    assert _cursor_has_prior_chat(cell) is False


def test_a_PRINT_MODE_history_also_counts_because_it_leaves_NO_chats_dir(tmp_path):
    """Measured: a `-p` run wrote `projects/<slug>/agent-transcripts/<uuid>/` and
    created no `chats/` dir at all, yet `--continue` afterwards RESUMED. The
    primary probe alone would answer "no" for such a cell and start a fresh chat
    over a real history."""
    cell = _cell(tmp_path)
    tr = (_cursor_data_dir(cell) / "projects" / "tmp-ws" / "agent-transcripts"
          / "f80f4367-7c74-4066-915c")
    tr.mkdir(parents=True)
    (tr / "f80f4367.jsonl").write_text("{}")
    assert _cursor_has_prior_chat(cell) is True


def test_an_EMPTY_projects_dir_is_not_evidence(tmp_path):
    """The FAILED genesis run itself creates `projects/<slug>/` before exiting, so
    "the projects dir exists" is not history. Counting it would flip a virgin cell
    into the exit-1 branch on its SECOND spawn attempt."""
    cell = _cell(tmp_path)
    (_cursor_data_dir(cell) / "projects" / "tmp-ws").mkdir(parents=True)
    assert _cursor_has_prior_chat(cell) is False


def test_the_probe_FAILS_TOWARD_FRESH_and_never_raises(tmp_path, monkeypatch):
    """The failure DIRECTION is the design. If cursor moves its layout (or the dir
    is unreadable) both probes go False, --continue is omitted, and the cell
    starts a NEW chat: continuity lost, visibly, recoverably. The other direction
    emits --continue into a store that cannot satisfy it and the cell does not
    start at all."""
    cell = _cell(tmp_path)
    _seed_chat(cell)

    def boom(_self):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert _cursor_has_prior_chat(cell) is False


# ── THE SECURITY POSTURE THIS MEMBRANE DELIBERATELY DOES NOT WIDEN ──────────

def test_argv_never_carries_the_TOOL_APPROVAL_flags(tmp_path):
    """Widening tool approval is cell.yaml's to do explicitly. `--auto-review` is
    included because it is the quiet one: a server-side classifier auto-running
    "safe" calls is still a relaxation the operator did not ask for."""
    argv = _build_cursor_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    for flag in ("--force", "-f", "--yolo", "--auto-review", "--approve-mcps"):
        assert flag not in argv, f"{flag} was added to the default cell argv"


def test_argv_DOES_carry_trust_because_refusing_it_OFFERS_ONLY_WIDER_FLAGS(tmp_path):
    """Measured, non-interactively, in an untrusted workspace: "To proceed, you
    can either: run 'agent' interactively to decide / Pass --trust, --yolo, or -f
    if you trust this directory." A detached cell cannot take the first option,
    and the remaining two are STRICTLY WIDER than the flag we withheld — the same
    inversion VibeMembrane's --trust documents.

    Cursor persists the answer, but into
    `<CURSOR_DATA_DIR>/projects/<slug>/.workspace-trusted`, which for a cell is
    cell-private state. THAT CONTAINMENT IS A PROPERTY OF THE ISOLATED DATA DIR,
    NOT OF THE FLAG: drop the isolation and this becomes a write into the
    operator's ~/.cursor."""
    argv = _build_cursor_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert "--trust" in argv


def test_argv_is_the_INTERACTIVE_tui_not_the_one_shot_print_mode(tmp_path):
    """`-p`/`--print` is send-one-prompt-and-exit. A cell that exits after one
    turn is not a cell."""
    argv = _build_cursor_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert argv[0] == "cursor-agent"
    assert "-p" not in argv and "--print" not in argv


def test_argv_embeds_NO_absolute_path_because_launch_chdirs(tmp_path):
    """#314: an absolute Windows path containing spaces re-splits crossing the
    exec boundary. launch() chdirs and cursor defaults the workspace to the cwd,
    so neither --workspace nor --add-dir is needed to declare it."""
    cell = _cell(tmp_path)
    _seed_chat(cell)
    argv = _build_cursor_argv(cell, no_starter=True, passthrough=[])
    assert not any(str(tmp_path) in str(a) for a in argv)
    assert "--workspace" not in argv and "--add-dir" not in argv


def test_the_starter_is_POSITIONAL_because_cursor_has_no_system_prompt_flag(tmp_path):
    """Cursor's surface offers no --append-system-prompt / --system-prompt-override
    sibling, so the positional prompt is the only identity channel — as with vibe.
    Verified compatible with --continue in the same argv."""
    sp = tmp_path / "starter.md"
    sp.write_text("you are cursor-1, a swarph cell")
    argv = _build_cursor_argv(_cell(tmp_path, starter=sp), no_starter=False, passthrough=[])
    assert "you are cursor-1, a swarph cell" in argv


def test_no_starter_suppresses_it(tmp_path):
    sp = tmp_path / "starter.md"
    sp.write_text("identity")
    argv = _build_cursor_argv(_cell(tmp_path, starter=sp), no_starter=True, passthrough=[])
    assert "identity" not in argv


def test_passthrough_reaches_the_provider_unchanged(tmp_path):
    argv = _build_cursor_argv(
        _cell(tmp_path), no_starter=True, passthrough=["--model", "gpt-5"]
    )
    assert argv[-2:] == ["--model", "gpt-5"]


# ── SANDBOX: DECLARED OR SILENT, NEVER GUESSED ──────────────────────────────

def test_sandbox_is_ABSENT_by_default_because_the_WINDOWS_matrix_is_UNPROBED(tmp_path):
    """>>> THE ONE PLACE THIS MEMBRANE REFUSES TO COPY agy. <<< agy defaults its
    sandbox ON because firejail's availability is knowable from this box.
    `--sandbox` OVERRIDES cursor's own config, and its behavior on the SECOND
    validated environment (Windows/psmux) cannot be probed from here — so forcing
    a value is the one choice that can make a cell fail to START on the platform
    we cannot test, in exchange for a posture cursor's own default already
    provides."""
    argv = _build_cursor_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert "--sandbox" not in argv


def test_a_declared_sandbox_is_forwarded(tmp_path):
    for value in ("enabled", "disabled"):
        argv = _build_cursor_argv(
            _cell(tmp_path, sandbox=value), no_starter=True, passthrough=[]
        )
        assert argv[argv.index("--sandbox") + 1] == value


def test_a_CODEX_SHAPED_sandbox_value_is_refused_LOUDLY(tmp_path):
    """`workspace-write` is codex's vocabulary. Forwarding it would have the cell
    rejected by the CLI AFTER the pane is already up — a cell that dies at exec
    reads as a spawn bug, not as a cell.yaml typo."""
    with pytest.raises(CellError) as exc:
        _cursor_sandbox(_cell(tmp_path, sandbox="workspace-write"))
    assert "cursor" in str(exc.value)


# ── THE ENV MEMBRANE: BILLING, REDIRECT AND ISOLATION CLOSURE ───────────────

def test_CURSOR_API_ENDPOINT_is_scrubbed_the_SHARED_SWEEP_MISSES_IT():
    """The redirect that no existing rule catches: it is neither `*_API_KEY` nor
    `*_BASE_URL`, so `scrub_env_for_subprocess` leaves it. Vendor source defaults
    the endpoint to `process.env.CURSOR_API_ENDPOINT`, so an inherited one moves
    the cell off the subscription endpoint with nothing about the session looking
    different."""
    env = {"CURSOR_API_ENDPOINT": "https://relay.example", "PATH": "/usr/bin"}
    _scrub_cursor_namespace(env)
    assert "CURSOR_API_ENDPOINT" not in env
    assert "PATH" in env, "the scrub is namespace-scoped, not a wipe"


def test_the_whole_CURSOR_namespace_goes_including_the_RESTORE_HOOK():
    """Deny-by-default, because enumerating today's redirect vars re-opens on the
    next release. `__CURSOR_SANDBOX_ENV_RESTORE` is in scope because it is a HOOK
    the sandbox shell wrapper evals — a payload that outlives a scrub of the
    variables themselves — and it only exists when a cell is spawned FROM a cursor
    session, where none of it means anything to the cell."""
    env = {
        "CURSOR_API_KEY": "sk-live",
        "CURSOR_AUTH_TOKEN": "tok",
        "CURSOR_DATA_DIR": "/operator/.cursor",
        "CURSOR_SOMETHING_NEW": "/z",
        "__CURSOR_SANDBOX_ENV_RESTORE": "export CURSOR_API_KEY=sk-live",
    }
    _scrub_cursor_namespace(env)
    assert env == {}, f"a cursor-namespace var survived: {env}"


def test_env_isolates_the_DATA_DIR_into_the_cell(tmp_path):
    """Grounded: `CURSOR_DATA_DIR=<tmp> agent status` reported "Logged in" AND
    wrote its state into <tmp>, so this is what separates the cell's chats /
    projects / cli-config from the operator's ~/.cursor."""
    env = _cursor_env(_cell(tmp_path))
    assert env["CURSOR_DATA_DIR"] == str(tmp_path / _CURSOR_CELL_DATA_SUBDIR)
    assert (tmp_path / _CURSOR_CELL_DATA_SUBDIR).is_dir()
    assert env.get("SWARPH_SPAWN") == "1"
    assert env.get("SWARPH_SELF") == "cursor-1"


def test_an_inherited_CURSOR_DATA_DIR_cannot_win_over_the_cells_own(tmp_path, monkeypatch):
    """The scrub removes CURSOR_* and `_cursor_env` sets its own AFTER — order is
    load-bearing. Inverted, an operator's shell CURSOR_DATA_DIR silently defeats
    the isolation and the cell writes into the operator's state: isolation that
    reads as working."""
    monkeypatch.setenv("CURSOR_DATA_DIR", "/operator/elsewhere")
    env = _cursor_env(_cell(tmp_path))
    assert env["CURSOR_DATA_DIR"] == str(tmp_path / _CURSOR_CELL_DATA_SUBDIR)


def test_HOME_is_NOT_relocated_it_would_BREAK_AUTH_AND_MESH_IDENTITY(tmp_path, monkeypatch):
    """>>> TWO INDEPENDENT REASONS, EITHER ONE SUFFICIENT, AND THE FIRST IS
    MEASURED: `HOME=<tmp> agent status` -> "Not logged in". <<< Auth resolves off
    the CONFIG dir, not the data dir, so grok's isolated-$HOME shape would leave
    the cell unauthenticated. And a fake $HOME relocates every Path.home() lookup,
    costing the cell ~/.config/swarph/<self>.peer_token, ~/.swarph/secrets.toml and
    the codegraph hook — grok pays that because grok has no alternative; cursor
    has CURSOR_DATA_DIR, so paying it here would be a cost with no purchase."""
    monkeypatch.setenv("HOME", "/home/operator")
    env = _cursor_env(_cell(tmp_path))
    assert env.get("HOME") == "/home/operator"
    assert env["CURSOR_DATA_DIR"].endswith(_CURSOR_CELL_DATA_SUBDIR)


def test_XDG_CONFIG_HOME_is_NOT_relocated_it_is_BOTH_SIDES_credentials(tmp_path, monkeypatch):
    """>>> THE TIDIEST-LOOKING ISOLATION KNOB IS THE ONE THAT BREAKS BOTH SIDES AT
    ONCE. <<< Vendor `getAuthFilePath` reads $XDG_CONFIG_HOME/cursor/auth.json —
    and swarph's OWN `_config_root()` reads $XDG_CONFIG_HOME/swarph for the cells
    dir and the peer token. Relocating it would leave the cell unauthenticated with
    cursor AND unaddressable on the mesh, from one line that looks like hygiene."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/operator/.config")
    env = _cursor_env(_cell(tmp_path))
    assert env.get("XDG_CONFIG_HOME") == "/home/operator/.config"


def test_env_does_NOT_pop_the_gateway_token(tmp_path, monkeypatch):
    """Identical posture to every other membrane: the cell inherits the gateway
    token so its mesh DMs work. Popping it would SILENTLY MUTE the cell — and a
    mute cell cannot report that it is mute."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    env = _cursor_env(_cell(tmp_path))
    assert env.get("MESH_GATEWAY_TOKEN") == "tok"


def test_no_credential_symlink_is_needed_and_that_is_the_POINT(tmp_path):
    """grok and vibe both symlink the operator credential into the cell because
    their isolation MOVES the dir that holds it. Cursor's does not, so there is
    nothing to link and no link that can silently fail (the failure-rendered-as-
    absence class vibe's `_link_vibe_credential` had to grow a warning for).
    Pinned as a test so nobody "completes the pattern" by adding one: the cell dir
    must contain NO symlink, and the env must carry no auth-path override."""
    cell = _cell(tmp_path)
    _cursor_env(cell)
    cell_dir = tmp_path / _CURSOR_CELL_DATA_SUBDIR
    assert not [p for p in cell_dir.rglob("*") if p.is_symlink()], (
        "a credential symlink appeared — cursor's auth dir is deliberately not "
        "relocated, so there is nothing to link and nothing that can fail silently")
    assert list(cell_dir.iterdir()) == [], (
        "the membrane seeded state into the cell data dir; everything in there "
        "should be cursor's own to create")


# ── LAUNCH: THE WINDOWS PANE-ROOT SPLIT ─────────────────────────────────────

def test_windows_launch_BLOCKS_so_the_psmux_pane_survives(tmp_path, monkeypatch):
    """On Windows os.exec* is emulated as spawn-and-exit, NOT a replace: inside a
    psmux pane that collapses the pane and orphans the agent — "session created,
    no agent". A blocking subprocess.run keeps THIS process as the pane root."""
    from swarph_cli.commands import spawn

    run = MagicMock(return_value=types.SimpleNamespace(returncode=17))
    execve = MagicMock()
    monkeypatch.setattr(spawn.sys, "platform", "win32")
    monkeypatch.setattr(spawn.os, "chdir", lambda _cwd: None)
    monkeypatch.setattr(spawn.subprocess, "run", run)
    monkeypatch.setattr(spawn.os, "execve", execve)

    rc = MEMBRANES["cursor"].launch(
        _cell(tmp_path), "C:/bin/cursor-agent.exe", ["cursor-agent", "--trust"],
    )

    assert rc == 17
    assert run.call_args.args[0] == ["C:/bin/cursor-agent.exe", "--trust"]
    execve.assert_not_called()


def test_resolve_binary_prefers_the_UNAMBIGUOUS_name(tmp_path, monkeypatch):
    """The installer drops both `cursor-agent` and a bare `agent` symlink, and
    `agent` is a name any unrelated tool can occupy on a shared PATH."""
    from swarph_cli.commands import spawn

    seen = []

    def which(name):
        seen.append(name)
        return f"/usr/bin/{name}" if name == "cursor-agent" else None

    monkeypatch.setattr(spawn.shutil, "which", which)
    assert MEMBRANES["cursor"].resolve_binary() == "/usr/bin/cursor-agent"
    assert seen[0] == "cursor-agent"


# ── MEMORY SYNC: INSTRUCTIONS ARE MEMORY, CONFIG IS POLICY ──────────────────

def test_workspace_instruction_files_are_synced(tmp_path):
    """AGENTS.md and .cursor/rules/**/*.mdc are the two cwd channels the 2026.08.11
    bundle's rule loader actually reads."""
    cell = _cell(tmp_path)
    (tmp_path / "AGENTS.md").write_text("you are cursor-1")
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "mesh.mdc").write_text("always DM on completion")

    keys = dict(MEMBRANES["cursor"].memory_sync_files(cell))
    assert "AGENTS.md" in keys
    assert "cursor-rules/mesh.mdc" in keys


def test_cli_config_is_NOT_synced_it_is_the_APPROVAL_POLICY_VECTOR(tmp_path):
    """>>> VIBE'S config.toml LESSON, SAME VECTOR, DIFFERENT FILE. <<<
    `cli-config.json` carries `approvalMode` and `permissions`. The memory repo is
    keyed by cell.ROLE, not identity, so a config captured from ONE cell restores
    into EVERY same-role cell through a git remote — one operator relaxing approval
    once propagates it to the fleet, while the argv posture test above still
    passes. THE PROPERTY IS "the cell does not run with relaxed approval", AND
    ARGV IS ONLY ONE OF ITS TWO INPUTS."""
    cell = _cell(tmp_path)
    data = _cursor_data_dir(cell)
    data.mkdir(parents=True)
    (data / "cli-config.json").write_text('{"approvalMode":"run-everything"}')
    (tmp_path / "AGENTS.md").write_text("memory")

    keys = dict(MEMBRANES["cursor"].memory_sync_files(cell))
    assert "AGENTS.md" in keys, "memory must still sync"
    assert not any("cli-config" in k or "approval" in k for k in keys), (
        f"the approval policy is being synced between cells: {list(keys)}")


def test_USER_scope_rules_are_NOT_synced_they_are_the_OPERATORS(tmp_path, monkeypatch):
    """Cursor's user rules live at `~/.cursor/rules` — homedir, NOT the data dir —
    so the cell already shares them with the operator (HOME is not relocated).
    Capturing them would push operator-global rules into a per-role repo and back
    out into every cell of that role."""
    from swarph_cli.commands import spawn

    fake_home = tmp_path / "op"
    (fake_home / ".cursor" / "rules").mkdir(parents=True)
    (fake_home / ".cursor" / "rules" / "personal.mdc").write_text("operator only")
    monkeypatch.setattr(spawn.Path, "home", staticmethod(lambda: fake_home))

    keys = dict(MEMBRANES["cursor"].memory_sync_files(_cell(tmp_path)))
    assert not any("personal" in k for k in keys), keys


def test_memory_sync_and_restore_round_trip(tmp_path):
    cell = _cell(tmp_path)
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "nested" ).mkdir()
    (rules / "nested" / "deep.mdc").write_text("rule")

    files = dict(MEMBRANES["cursor"].memory_sync_files(cell))
    assert "cursor-rules/nested/deep.mdc" in files
    dest = MEMBRANES["cursor"].memory_restore_dest(
        ("cursor-rules", "nested", "deep.mdc"), cell
    )
    assert dest == rules / "nested" / "deep.mdc"


def test_memory_sync_is_EMPTY_on_a_fresh_cell_not_an_error(tmp_path):
    """Absence is a state, not a failure."""
    assert MEMBRANES["cursor"].memory_sync_files(_cell(tmp_path)) == []


def test_memory_restore_dest_returns_None_for_a_foreign_prefix(tmp_path):
    """The membrane must claim ONLY its own namespace — returning a path for
    another provider's key would let one membrane write into another's memory."""
    assert MEMBRANES["cursor"].memory_restore_dest(
        ("grok-memory", "MEMORY.md"), _cell(tmp_path)) is None


def test_guard_file_is_AGENTS_md_the_file_an_empty_restore_would_CLOBBER(tmp_path):
    assert MEMBRANES["cursor"].memory_guard_file(_cell(tmp_path)) == tmp_path / "AGENTS.md"
