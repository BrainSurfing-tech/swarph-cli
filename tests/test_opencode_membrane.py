"""OpencodeMembrane — the OpenCode CLI as a durable swarph CELL.

Grounded against real `opencode 1.18.30` on 2026-09-11 by EXECUTION, following
the cursor membrane's "probe first, never assume" discipline. The probes that
decided the design (each recorded in the spec's "Probe findings" block):

  · `XDG_DATA_HOME=/tmp/x opencode --pure db path` -> `/tmp/x/opencode/opencode.db`
    => the session DB is XDG-data-dir scoped, so the cell isolates db + auth by
    relocating XDG_DATA_HOME alone (no fake $HOME).
  · `XDG_CONFIG_HOME=/tmp/x opencode --pure debug config` -> `"plugin": []`
    => the CONFIG dir (and its auto-loaded `plugins/`) is XDG-config-dir scoped.
    Relocating XDG_CONFIG_HOME keeps the operator's plugins out of the cell — and
    unlike cursor, that relocation DOES NOT break opencode auth, because opencode
    reads auth from the DATA dir (`~/.local/share/opencode/auth.json`), not config.
  · `opencode --pure session list --format json` returns rows with `id`,
    `directory`, `updated` => residency is per-directory and discoverable, so the
    membrane resumes the newest matching session via `--session <id>` rather than
    guessing global `--continue`.

>>> AND THE RELEASE ORDER IS LOAD-BEARING, per the #247 outage: the membrane ships
in a RELEASED swarph-cli (registered + CLI_ENABLED_PROVIDERS) BEFORE `opencode`
enters swarph_shared.VALID_PROVIDERS. spawn.py's guard is a SUBSET check, so this
direction is inert; reversed, it raises AT IMPORT and kills `swarph spawn` for
every fresh install. <<<
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from swarph_cli.cell import CLI_ENABLED_PROVIDERS, CellError, load_cell
from swarph_cli.commands.spawn import (
    MEMBRANES,
    OpencodeMembrane,
    _OPENCODE_CELL_SUBDIR,
    _build_opencode_argv,
    _opencode_cell_dir,
    _opencode_env,
    _opencode_prior_session,
    _scrub_opencode_namespace,
    _validate_routing,
)


def _cell(tmp_path, starter=None):
    return types.SimpleNamespace(
        cwd=tmp_path,
        name="opencode-1",
        provider="opencode",
        role="worker",
        starter_prompt_path=starter,
        sandbox=None,
        extra={},
    )


def _session_row(directory, sid="ses_abc", updated=1789308075000):
    return {"id": sid, "directory": directory, "updated": updated, "projectId": "global"}


def _fake_session_list(monkeypatch, rows, binary="/usr/bin/opencode"):
    """Mock the `opencode session list` subprocess to return the given rows.

    ALSO PINS THE BINARY LOOKUP, and that is the load-bearing half.
    `_opencode_prior_session` resolves the binary BEFORE it shells out and
    returns None when it cannot find one — so on a runner WITHOUT opencode
    installed the function short-circuits and this subprocess stub is never
    reached. Stubbing only `subprocess.run` made the verdict depend on the
    RUNNER'S SOFTWARE INVENTORY: green on lab-ovh (opencode present), red on
    GitHub's ubuntu image (absent). A test whose result tracks the host rather
    than the code is not testing the code.
    """
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_binary",
                        lambda: binary)

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(rows), stderr=""
        )

    monkeypatch.setattr("swarph_cli.commands.spawn.subprocess.run", fake_run)


# ── REGISTRATION AND THE ORDERING INVARIANT ─────────────────────────────────

def test_opencode_is_registered_and_the_membrane_is_AHEAD_of_the_whitelist():
    """The safe direction, asserted. An extra membrane is INERT because the guard
    is `VALID_PROVIDERS ⊆ MEMBRANES`; a whitelisted provider with no membrane is
    an import-time crash for every install."""
    from swarph_shared.cell import VALID_PROVIDERS

    assert isinstance(MEMBRANES["opencode"], OpencodeMembrane)
    assert not (set(VALID_PROVIDERS) - set(MEMBRANES))


def test_opencode_is_enabled_by_the_CLI_so_the_membrane_is_not_inert(tmp_path):
    """Registration alone does nothing: `load_cell` gates on
    CLI_ENABLED_PROVIDERS, so without the enablement a `provider: opencode`
    cell.yaml is rejected and the membrane can never be reached."""
    assert "opencode" in CLI_ENABLED_PROVIDERS
    path = tmp_path / "cell.yaml"
    path.write_text(
        "schema_version: v1\nname: opencode-1\nrole: worker\ncwd: .\nprovider: opencode\n",
        encoding="utf-8",
    )
    assert load_cell(path).provider == "opencode"


def test_opencode_does_NOT_override_pre_launch_it_inherits_the_base_hoist():
    """Linux-tmux / Windows-psmux parity is INHERITED rather than re-implemented.
    Card #2 hoisted `_launch_via_tmux` to the base membrane precisely because
    per-provider overrides WERE the discrimination."""
    from swarph_cli.commands.spawn import ProviderMembrane

    assert "pre_launch" not in OpencodeMembrane.__dict__
    assert OpencodeMembrane.pre_launch is ProviderMembrane.pre_launch


def test_opencode_does_not_use_a_pinned_session():
    """opencode owns its own session ids in a sqlite DB; swarph carries no pinned
    UUID. Residency is discovered per-directory via `session list`."""
    assert MEMBRANES["opencode"].uses_pinned_session() is False


def test_routing_native_opencode_is_accepted_and_a_mismatch_is_refused(tmp_path):
    for routing in ("{}\n", "\n  native: opencode\n"):
        path = tmp_path / f"ok-{len(routing)}.yaml"
        path.write_text(
            "schema_version: v1\nname: opencode-1\nrole: worker\ncwd: .\n"
            f"provider: opencode\nrouting: {routing}",
            encoding="utf-8",
        )
        _validate_routing(load_cell(path))

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: v1\nname: opencode-1\nrole: worker\ncwd: .\n"
        "provider: opencode\nrouting:\n  native: anthropic\n",
        encoding="utf-8",
    )
    with pytest.raises(CellError):
        _validate_routing(load_cell(bad))


# ── RESUME BY DIRECTORY DISCOVERY, NOT GLOBAL --continue ────────────────────

def test_a_VIRGIN_cell_gets_NO_session_flag(tmp_path, monkeypatch):
    """`--continue` resumes the LAST session globally, which on a multi-cell box is
    the wrong cell's session. The membrane must not emit it, and must not emit
    `--session` when there is no session for THIS directory."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    argv = _build_opencode_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert "--session" not in argv
    assert "--continue" not in argv


def test_a_cell_WITH_a_prior_session_resumes_by_id(tmp_path, monkeypatch):
    """The positive leg: continuity must actually work, or "never resume" would
    pass the test above and make every opencode cell amnesiac."""
    monkeypatch.setattr(
        "swarph_cli.commands.spawn._opencode_prior_session", lambda cell: "ses_abc"
    )
    argv = _build_opencode_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert "--session=ses_abc" in argv


def test_prior_session_is_KEYED_ON_THE_CWD_not_any_session(tmp_path, monkeypatch):
    """`session list` rows carry `directory`. A session belonging to a DIFFERENT
    workspace must not make this cell claim continuity it cannot safely resume."""
    foreign = [{"id": "ses_zzz", "directory": "/some/other/workspace", "updated": 1}]
    _fake_session_list(monkeypatch, foreign)
    assert _opencode_prior_session(_cell(tmp_path)) is None


def test_prior_session_prefers_the_NEWEST_matching_directory(tmp_path, monkeypatch):
    rows = [
        _session_row(str(tmp_path), sid="ses_old", updated=100),
        _session_row(str(tmp_path), sid="ses_new", updated=200),
    ]
    _fake_session_list(monkeypatch, rows)
    assert _opencode_prior_session(_cell(tmp_path)) == "ses_new"


def test_prior_session_probe_FAILS_TOWARD_FRESH_and_never_raises(tmp_path, monkeypatch):
    """The failure DIRECTION is the design. If the binary is absent or the DB is
    unreadable, resume is omitted and the cell starts fresh: continuity lost,
    visibly, recoverably. The other direction emits a resume the CLI cannot
    satisfy and the cell does not start."""
    def boom(cmd, **kwargs):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr("swarph_cli.commands.spawn.subprocess.run", boom)
    assert _opencode_prior_session(_cell(tmp_path)) is None


def test_prior_session_runs_the_RESOLVED_binary_with_pure(tmp_path, monkeypatch):
    """>>> #423. <<< Discovery must ``--pure``-run the SAME binary the membrane would
    exec (``resolve_binary``), not the literal ``opencode`` from PATH — on the curl
    layout ``opencode`` is off PATH, so the discovery would file-not-found and every
    spawn would become a silent fresh session, forever. ``--pure`` keeps the probe
    free of the operator's plugins (the pinned guarantee lab-ovh asked for)."""
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_binary", lambda: "/fake/bin/opencode")
    monkeypatch.setattr("swarph_cli.commands.spawn.subprocess.run", fake_run)

    assert _opencode_prior_session(_cell(tmp_path)) is None
    assert seen and seen[0][0] == "/fake/bin/opencode"
    assert "--pure" in seen[0]


def test_prior_session_returns_none_without_a_binary(tmp_path, monkeypatch):
    """No binary, no discovery: the probe must not fall back to bare ``opencode``
    and it must not call out at all."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_binary", lambda: None)
    called = []
    monkeypatch.setattr(
        "swarph_cli.commands.spawn.subprocess.run", lambda *a, **k: called.append(1)
    )
    assert _opencode_prior_session(_cell(tmp_path)) is None
    assert called == []


# ── THE SECURITY POSTURE THIS MEMBRANE DELIBERATELY DOES NOT WIDEN ──────────

def test_argv_never_carries_the_TOOL_APPROVAL_flags(tmp_path, monkeypatch):
    """opencode's `--auto` auto-approves tool use; widening approval is cell.yaml's
    to do explicitly (via `-- --auto` passthrough), never a membrane default."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    argv = _build_opencode_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    for flag in ("--auto", "--yolo"):
        assert flag not in argv, f"{flag} was added to the default cell argv"


def test_argv_is_the_INTERACTIVE_tui_not_the_one_shot_run_mode(tmp_path, monkeypatch):
    """`opencode run` is send-one-prompt-and-exit. A cell that exits after one
    turn is not a cell. argv[0] is the bare `opencode` TUI."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    argv = _build_opencode_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert argv[0] == "opencode"
    assert "run" not in argv[:2]


def test_argv_embeds_NO_absolute_path_because_launch_chdirs(tmp_path, monkeypatch):
    """#314: an absolute path re-splits crossing the exec boundary. launch()
    chdirs to cell.cwd and opencode resolves the workspace from the cwd, so no
    path-shaped string crosses the exec boundary."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    argv = _build_opencode_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert not any(str(tmp_path) in str(a) for a in argv)


def test_the_starter_uses_the_documented_prompt_flag_on_a_FRESH_session(tmp_path, monkeypatch):
    """opencode's TUI `--prompt` is the initial-prompt channel (the docs' flag, not
    an invented one), applied on a FRESH session only — on resume the starter is
    skipped rather than appending a duplicate turn."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    sp = tmp_path / "starter.md"
    sp.write_text("you are opencode-1, a swarph cell")
    argv = _build_opencode_argv(_cell(tmp_path, starter=sp), no_starter=False, passthrough=[])
    assert "--prompt=you are opencode-1, a swarph cell" in argv


def test_starter_with_yaml_frontmatter_is_NOT_eaten_as_flags(tmp_path, monkeypatch):
    """>>> --prompt=<starter>, not --prompt <starter>. <<< A cell starter routinely
    opens with a YAML frontmatter (``---``), and opencode's yargs eats a following
    ``---``/``- `` token as a flag (#423 review, verified against yargs-parser 22:
    --prompt "---\\n…" parses to {prompt:true, "-":true} and the starter is lost).
    The `=` form keeps the starter one argument, whatever it opens with."""
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    sp = tmp_path / "starter.md"
    starter = "---\nname: opencode-1\n---\nYou are a cell."
    sp.write_text(starter)
    argv = _build_opencode_argv(_cell(tmp_path, starter=sp), no_starter=False, passthrough=[])
    assert f"--prompt={starter}" in argv


def test_no_starter_suppresses_the_prompt_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    sp = tmp_path / "starter.md"
    sp.write_text("identity")
    argv = _build_opencode_argv(_cell(tmp_path, starter=sp), no_starter=True, passthrough=[])
    assert "--prompt" not in argv


def test_passthrough_reaches_the_provider_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("swarph_cli.commands.spawn._opencode_prior_session", lambda cell: None)
    argv = _build_opencode_argv(
        _cell(tmp_path), no_starter=True, passthrough=["--model", "deepseek/deepseek-v4-pro"]
    )
    assert argv[-2:] == ["--model", "deepseek/deepseek-v4-pro"]


# ── THE ENV MEMBRANE: SUBSCRIPTION, REDIRECT AND ISOLATION CLOSURE ───────────

def test_the_whole_OPENCODE_namespace_goes():
    """Deny-by-default, because enumerating today's redirect vars re-opens on the
    next release. Every `OPENCODE_*` var (config, config-dir, env overrides) is a
    potential redirect the cell must set itself, never inherit."""
    env = {
        "OPENCODE_CONFIG": "/operator/config.json",
        "OPENCODE_CONFIG_DIR": "/operator/.config/opencode",
        "OPENCODE_CONFIG_CONTENT": '{"model":"x"}',
        "PATH": "/usr/bin",
    }
    _scrub_opencode_namespace(env)
    assert env == {"PATH": "/usr/bin"}, f"an opencode-namespace var survived: {env}"


def test_env_isolates_CONFIG_and_DATA_into_the_cell(tmp_path):
    """Grounded: XDG_DATA_HOME moves the DB (probe: `db path`), XDG_CONFIG_HOME
    moves the plugin dir (probe: `debug config` -> `plugin: []`). Both relocations
    keep $HOME intact, unlike grok's fake-$HOME shape."""
    env = _opencode_env(_cell(tmp_path))
    assert env["XDG_DATA_HOME"] == str(tmp_path / _OPENCODE_CELL_SUBDIR / "data")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / _OPENCODE_CELL_SUBDIR / "config")
    assert (tmp_path / _OPENCODE_CELL_SUBDIR / "data").is_dir()
    assert (tmp_path / _OPENCODE_CELL_SUBDIR / "config").is_dir()
    assert env.get("SWARPH_SPAWN") == "1"
    assert env.get("SWARPH_SELF") == "opencode-1"


def test_an_inherited_XDG_override_cannot_win_over_the_cells_own(tmp_path, monkeypatch):
    """The scrub must not run AFTER the cell sets its own XDG_* — order is
    load-bearing (the grok/vibe lesson). Inverted, an operator's shell
    XDG_DATA_HOME silently defeats the isolation and the cell writes into the
    operator's DB."""
    monkeypatch.setenv("XDG_DATA_HOME", "/operator/data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/operator/config")
    env = _opencode_env(_cell(tmp_path))
    assert env["XDG_DATA_HOME"] == str(tmp_path / _OPENCODE_CELL_SUBDIR / "data")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / _OPENCODE_CELL_SUBDIR / "config")


def test_HOME_is_NOT_relocated_it_would_BREAK_MESH_IDENTITY(tmp_path, monkeypatch):
    """>>> XDG relocation is the WHOLE isolation here, AND the reason to refuse a
    fake $HOME. <<< A fake $HOME relocates every Path.home() lookup, costing the
    cell ~/.config/swarph/<self>.peer_token, ~/.swarph/secrets.toml and the
    codegraph hook. opencode honors XDG_DATA_HOME / XDG_CONFIG_HOME (verified), so
    the cell pays nothing to keep $HOME."""
    monkeypatch.setenv("HOME", "/home/operator")
    env = _opencode_env(_cell(tmp_path))
    assert env.get("HOME") == "/home/operator"
    # Assert the LOCATION via path PARTS, not a string `.endswith("/.../data")`:
    # the value's separator is an environment fact (\ on Windows), and a test that
    # asserts the separator fails on exactly the box it most needs to pass.
    assert Path(env["XDG_DATA_HOME"]).parts[-2:] == (_OPENCODE_CELL_SUBDIR, "data")


def test_env_does_NOT_pop_the_gateway_token(tmp_path, monkeypatch):
    """Identical posture to every other membrane: the cell inherits the gateway
    token so its mesh DMs work. Popping it would SILENTLY MUTE the cell."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    env = _opencode_env(_cell(tmp_path))
    assert env.get("MESH_GATEWAY_TOKEN") == "tok"


def test_the_operator_credential_is_SYMLINKED_in_when_it_exists(tmp_path, monkeypatch):
    """opencode reads auth from the DATA dir, which this membrane relocates — so
    the cell must link the operator's `auth.json` in or it starts unauthenticated.
    Symlink, not copy: a copy is a second credential that never rotates."""
    fake_home = tmp_path / "op"
    src = fake_home / ".local" / "share" / "opencode" / "auth.json"
    src.parent.mkdir(parents=True)
    src.write_text("{}")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    _opencode_env(_cell(tmp_path))
    link = tmp_path / _OPENCODE_CELL_SUBDIR / "data" / "opencode" / "auth.json"
    assert link.is_symlink()
    # .resolve(), not readlink(): Windows returns the \\?\ extended-length form,
    # so a raw target comparison fails on the box where the assertion matters.
    assert link.resolve() == src.resolve()


def test_auth_link_is_IDEMPOTENT_second_spawn_does_NOT_relink(tmp_path, monkeypatch):
    """>>> #423 review. <<< A raw ``readlink() == op_auth`` comparison is False under
    a home symlink, a relative target, or a Windows path spelling — so the correct
    link would be torn down and re-created on EVERY spawn (and silently, by the
    best-effort contract). The fixed check resolves both sides, so a correct link
    must survive a second spawn untouched."""
    from swarph_cli.commands import spawn

    fake_home = tmp_path / "op"
    src = fake_home / ".local" / "share" / "opencode" / "auth.json"
    src.parent.mkdir(parents=True)
    src.write_text("{}")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    _opencode_env(_cell(tmp_path))  # creates the link
    unlinks = []
    monkeypatch.setattr(spawn.Path, "unlink", lambda self, *a, **k: unlinks.append(str(self)))
    _opencode_env(_cell(tmp_path))  # must NOT re-link a correct link
    assert unlinks == [], f"auth link re-linked on 2nd spawn: {unlinks}"


def test_NO_credential_symlink_when_the_operator_has_NO_auth(tmp_path, monkeypatch):
    """Absent operator auth is intended: the cell starts unauthenticated and
    opencode reports it itself — a better failure than a spawn refusing for a
    reason the operator cannot see. Nothing to link, no link to fail silently."""
    fake_home = tmp_path / "op"  # no ~/.local/share/opencode/auth.json
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    _opencode_env(_cell(tmp_path))
    cell_dir = tmp_path / _OPENCODE_CELL_SUBDIR
    assert not [p for p in cell_dir.rglob("*") if p.is_symlink()]


def test_autoupdate_is_disabled_in_the_cell(tmp_path):
    """A cell self-updating mid-run is a silent seat change. Pin it off for the
    cell explicitly — the same reason every probe sets OPENCODE_DISABLE_AUTOUPDATE."""
    assert _opencode_env(_cell(tmp_path))["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_env_disables_claude_and_external_skills(tmp_path):
    """>>> $HOME is shared by design, so opencode would load the box owner's
    ``~/.claude/CLAUDE.md`` and auto-discovered external skills into a cell whose
    identity is the CELL, not the owner — the wrong-identity class #423 caught. <<<
    The disable knobs must be set, and set AFTER the scrub (which pops OPENCODE_*)."""
    env = _opencode_env(_cell(tmp_path))
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"


def test_disable_knobs_survive_an_operator_value_because_they_are_set_after_the_scrub(
    tmp_path, monkeypatch
):
    """The scrub is deny-by-default over OPENCODE_*, so an operator's own disable
    would be lost — the cell re-sets its own AFTER, making the value authoritative
    rather than inherited."""
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE", "0")
    env = _opencode_env(_cell(tmp_path))
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"


def test_experimental_flag_is_NOT_set_because_the_probe_measured_it_UNNEEDED(tmp_path):
    """Measured 2026-09-16 (isolated config dir, deepseek-v4-pro turn): the
    plugin's `experimental.chat.system.transform` fires WITHOUT the
    OPENCODE_EXPERIMENTAL umbrella. Not setting it refuses to widen the cell's
    experimental surface for a flag the hook does not need — the posture this
    membrane applies to tool approval, applied to the runtime flag."""
    assert "OPENCODE_EXPERIMENTAL" not in _opencode_env(_cell(tmp_path))


def test_env_writes_the_cell_hook_plugin(tmp_path):
    """The membrane wires its own hook plugin into the isolated config dir, so a
    spawned cell gets starter/wake/postcompact products without a separate step.
    The interpreter is baked (the install_codex_hooks rule), never a bare PATH name."""
    _opencode_env(_cell(tmp_path))
    target = (tmp_path / _OPENCODE_CELL_SUBDIR / "config" / "opencode"
              / "plugins" / "swarph-opencode.js")
    assert target.is_file()
    assert "@PYTHON@" not in target.read_text(encoding="utf-8")


# ── BINARY RESOLUTION ───────────────────────────────────────────────────────

def test_resolve_binary_prefers_PATH_then_the_std_install_locations(tmp_path, monkeypatch):
    """The curl installer puts the binary at `~/.opencode/bin/opencode`; npm/bun/
    brew installs resolve via PATH. Prefer PATH (unambiguous), then the std dirs."""
    from swarph_cli.commands import spawn

    monkeypatch.setattr(spawn.shutil, "which", lambda name: None)
    fake_home = tmp_path / "op"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    (fake_home / ".opencode" / "bin").mkdir(parents=True)
    (fake_home / ".opencode" / "bin" / "opencode").write_text("#!/bin/sh\n")
    assert MEMBRANES["opencode"].resolve_binary() == str(
        fake_home / ".opencode" / "bin" / "opencode"
    )


def test_resolve_binary_uses_PATH_when_present(tmp_path, monkeypatch):
    from swarph_cli.commands import spawn

    monkeypatch.setattr(spawn.shutil, "which", lambda name: "/usr/local/bin/opencode")
    assert MEMBRANES["opencode"].resolve_binary() == "/usr/local/bin/opencode"


# ── MEMORY SYNC: INSTRUCTIONS ARE MEMORY, CONFIG IS POLICY ──────────────────

def test_AGENTS_md_is_synced_and_is_the_guard_file(tmp_path):
    """opencode reads AGENTS.md from the cwd (like codex/cursor). It is the one
    workspace instruction file this membrane owns."""
    cell = _cell(tmp_path)
    (tmp_path / "AGENTS.md").write_text("you are opencode-1")
    keys = dict(MEMBRANES["opencode"].memory_sync_files(cell))
    assert "AGENTS.md" in keys
    assert MEMBRANES["opencode"].memory_guard_file(cell) == tmp_path / "AGENTS.md"


def test_memory_sync_is_EMPTY_on_a_fresh_cell_not_an_error(tmp_path):
    """Absence is a state, not a failure."""
    assert MEMBRANES["opencode"].memory_sync_files(_cell(tmp_path)) == []


def test_memory_restore_dest_returns_None_for_a_foreign_prefix(tmp_path):
    """The membrane must claim ONLY its own namespace."""
    assert (
        MEMBRANES["opencode"].memory_restore_dest(
            ("grok-memory", "MEMORY.md"), _cell(tmp_path)
        )
        is None
    )
