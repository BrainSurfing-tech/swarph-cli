"""#247 — VibeMembrane: the EU-domiciled Mistral lane as a durable swarph CELL.

Grounded against real `vibe 2.23.3` on 2026-08-05, not against a guess:
  · `HOME=<tmp> vibe --check-upgrade` CREATES `<tmp>/.vibe` -> vibe honours $HOME,
    so an isolated HOME genuinely separates cell state from the operator's.
    (`vibe --version` does NOT touch state and proves nothing — the probe that
    settles it had to be found, and I recorded "unverified" until it was.)
  · operator state at ~/.vibe: `.env` (0600, the credential), `config.toml`,
    `vibehistory` (durable conversation log), `trusted_folders.toml`, logs/.
  · argv surface: PROMPT positional · -p programmatic · --max-price · --agent
    NAME (a PROFILE selector) · --auto-approve · --trust · -c/--resume [ID].

>>> AND THE ORDER THIS SHIPS IN IS LOAD-BEARING. The membrane must land in a
RELEASED swarph-cli BEFORE `vibe` returns to swarph_shared.VALID_PROVIDERS.
Reversed, spawn.py's `VALID_PROVIDERS ⊆ MEMBRANES` guard raises AT IMPORT and
kills `swarph spawn` for every fresh install — measured 2026-08-05, ~5h, because
board #247's title stated the dependency backwards. <<<
"""
from pathlib import Path

import pytest

from swarph_cli.commands.spawn import (
    MEMBRANES,
    VibeMembrane,
    _build_vibe_argv,
    _scrub_vibe_namespace,
    _vibe_env,
    _VIBE_CELL_HOME_SUBDIR,
)


def _cell(tmp_path, starter=None):
    class _C:
        cwd = tmp_path
        name = "vibe-1"
        provider = "vibe"
        role = "worker"
        starter_prompt_path = starter
    return _C()


# ── REGISTRATION AND THE ORDERING INVARIANT ─────────────────────────────────

def test_vibe_is_registered_and_the_membrane_is_AHEAD_of_the_whitelist():
    """>>> THE SAFE DIRECTION, ASSERTED. <<< spawn.py's guard is a SUBSET check
    (`VALID_PROVIDERS ⊆ MEMBRANES`), so an extra membrane is INERT. This test
    pins that we are in the harmless state — membrane present, whitelist not yet
    widened — which is precisely the state the 2026-08-05 outage was NOT in."""
    from swarph_shared.cell import VALID_PROVIDERS
    assert isinstance(MEMBRANES["vibe"], VibeMembrane)
    assert not (set(VALID_PROVIDERS) - set(MEMBRANES)), (
        "a whitelisted provider has no membrane — spawn.py raises AT IMPORT and "
        "`swarph spawn` dies for every install")


def test_vibe_does_NOT_override_pre_launch_it_inherits_the_base_tmux_hoist():
    """>>> CARD #2 HOISTED `_launch_via_tmux` TO THE BASE MEMBRANE BECAUSE
    PER-PROVIDER OVERRIDES WERE THE DISCRIMINATION. A new membrane that
    re-declares pre_launch quietly re-introduces exactly what that card
    removed. <<< Asserted on the CLASS DICT, not by calling it: inheritance is
    the property, and a call would pass either way."""
    assert "pre_launch" not in VibeMembrane.__dict__, (
        "VibeMembrane overrides pre_launch — it must INHERIT the base tmux hoist")


def test_vibe_does_not_use_a_pinned_session():
    """vibe owns its sessions and `--resume` takes an OPTIONAL id, so a
    swarph-pinned UUID has nothing to bind to (same as grok)."""
    assert MEMBRANES["vibe"].uses_pinned_session() is False


# ── THE SECURITY POSTURE THIS MEMBRANE DELIBERATELY DOES NOT WIDEN ──────────

def test_argv_NEVER_carries_auto_approve_yolo_or_trust(tmp_path):
    """>>> A MEMBRANE MUST NOT WIDEN A SECURITY POSTURE ON THE OPERATOR'S
    BEHALF. <<< `--auto-approve` / `--yolo` / `--trust` relax TOOL APPROVAL.
    grok's sibling exposes autonomy through cell.yaml rather than hardcoding it;
    the same axis belongs there for vibe, as an explicit opt-in with its own
    review. This test exists so adding one is a deliberate act that breaks a
    test, not a convenience someone slips in."""
    argv = _build_vibe_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    joined = " ".join(argv)
    for flag in ("--auto-approve", "--yolo", "--trust"):
        assert flag not in joined, f"{flag} was added to the default cell argv"


def test_argv_is_the_INTERACTIVE_tui_not_the_one_shot_programmatic_mode(tmp_path):
    """`-p` is "send prompt, output response, and exit" — the opposite of a
    durable cell. A cell that exits after one turn is not a cell."""
    argv = _build_vibe_argv(_cell(tmp_path), no_starter=True, passthrough=[])
    assert argv[0] == "vibe"
    assert "-p" not in argv and "--prompt" not in argv


def test_the_starter_is_POSITIONAL_because_vibe_has_no_system_prompt_flag(tmp_path):
    """vibe's surface offers no `--system-prompt-override` sibling, and `--agent`
    is a PROFILE selector — passing a swarph role there selects a non-existent
    agent rather than conferring identity. The positional PROMPT is the only
    identity channel available."""
    sp = tmp_path / "starter.md"
    sp.write_text("you are vibe-1, a swarph cell")
    argv = _build_vibe_argv(_cell(tmp_path, starter=sp), no_starter=False, passthrough=[])
    assert "you are vibe-1, a swarph cell" in argv
    assert "--agent" not in argv


def test_no_starter_suppresses_it(tmp_path):
    sp = tmp_path / "starter.md"
    sp.write_text("identity")
    argv = _build_vibe_argv(_cell(tmp_path, starter=sp), no_starter=True, passthrough=[])
    assert "identity" not in argv


# ── THE ENV MEMBRANE: BILLING AND REDIRECT CLOSURE ──────────────────────────

def test_MISTRAL_API_KEY_is_scrubbed_this_is_the_BILLING_leak():
    """>>> THE ONE THAT COSTS MONEY SILENTLY. An inherited MISTRAL_API_KEY moves
    the cell OFF the $0 subscription path onto a METERED one WITH NO VISIBLE
    CHANGE — it works, it just bills. A leak that reads as working is the shape
    this mesh has been burned by all week. <<<"""
    env = {"MISTRAL_API_KEY": "sk-live", "VIBE_HOME": "/somewhere/else",
           "VIBE_CONFIG_DIR": "/elsewhere", "PATH": "/usr/bin"}
    _scrub_vibe_namespace(env)
    assert "MISTRAL_API_KEY" not in env
    assert "PATH" in env, "the scrub is namespace-scoped, not a wipe"


def test_VIBE_redirect_vars_are_scrubbed_they_would_DEFEAT_the_isolated_home():
    """`VIBE_HOME` / `VIBE_CONFIG_DIR` / any future redirect would point the cell
    back at the operator's state, silently undoing the isolation. Deny-by-
    default over the whole namespace, because enumerating the known ones re-opens
    on the next release — the enumerate-the-others failure, in an env var."""
    env = {"VIBE_HOME": "/x", "VIBE_CONFIG_DIR": "/y", "VIBE_SOMETHING_NEW": "/z"}
    _scrub_vibe_namespace(env)
    assert env == {}, f"a VIBE_* var survived: {env}"


def test_env_sets_HOME_into_the_cell_and_creates_the_isolated_vibe_dir(tmp_path):
    """Grounded: vibe honours $HOME (verified by `--check-upgrade` creating
    `<tmp>/.vibe`), so this is what actually separates the cell's vibehistory
    from the operator's."""
    env = _vibe_env(_cell(tmp_path))
    assert env["HOME"] == str(tmp_path / _VIBE_CELL_HOME_SUBDIR)
    assert (tmp_path / _VIBE_CELL_HOME_SUBDIR / ".vibe").is_dir()
    assert env.get("SWARPH_SPAWN") == "1"


def test_env_does_NOT_pop_the_gateway_token(tmp_path, monkeypatch):
    """Identical posture to every other membrane: the cell inherits the gateway
    token so its mesh DMs work. Popping it here would SILENTLY MUTE the cell —
    and a mute cell cannot report that it is mute."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    env = _vibe_env(_cell(tmp_path))
    assert env.get("MESH_GATEWAY_TOKEN") == "tok"


# ── MEMORY SYNC ROUND-TRIP ──────────────────────────────────────────────────

def test_memory_sync_and_restore_round_trip(tmp_path):
    """The membrane declares its OWN memory (card #51's non-discriminatory
    design): sync must find it and restore must put it back in the same place."""
    m = MEMBRANES["vibe"]
    vibe_dir = tmp_path / _VIBE_CELL_HOME_SUBDIR / ".vibe"
    vibe_dir.mkdir(parents=True)
    (vibe_dir / "vibehistory").write_text("turn 1")
    (vibe_dir / "config.toml").write_text("k = 1")
    files = dict(m.memory_sync_files(_cell(tmp_path)))
    assert "vibe-memory/vibehistory" in files
    assert "vibe-memory/config.toml" in files
    dest = m.memory_restore_dest(("vibe-memory", "vibehistory"), _cell(tmp_path))
    assert dest == vibe_dir / "vibehistory"


def test_memory_sync_is_EMPTY_on_a_fresh_cell_not_an_error(tmp_path):
    """A cell with no history yet returns [], not a crash and not a phantom
    entry. Absence is a state, not a failure."""
    assert MEMBRANES["vibe"].memory_sync_files(_cell(tmp_path)) == []


def test_memory_restore_dest_returns_None_for_a_foreign_prefix(tmp_path):
    """The membrane must claim ONLY its own namespace — returning a path for
    another provider's key would let one membrane write into another's memory."""
    assert MEMBRANES["vibe"].memory_restore_dest(
        ("grok-memory", "MEMORY.md"), _cell(tmp_path)) is None


def test_guard_file_is_None_vibe_has_no_cwd_project_doc(tmp_path):
    """No CLAUDE.md/AGENTS.md sibling exists for vibe, so there is no empty-file
    clobber to guard against — always-sync is correct here (same as grok)."""
    assert MEMBRANES["vibe"].memory_guard_file(_cell(tmp_path)) is None


# ── gpu-wsl's DEFECT: failure must not be rendered as absence ───────────────

def test_a_FAILED_symlink_is_LOUD_not_swallowed_as_an_absent_credential(tmp_path, capsys, monkeypatch):
    """>>> A FAILED SYMLINK IS NOT AN ABSENT SOURCE, AND THE FIRST DRAFT RENDERED
    THEM IDENTICALLY. <<< (gpu-wsl, PR #181 — a defect I did not ask about.)

    `except OSError: pass` swallowed permissions / cross-device / read-only cwd
    alongside the intended no-credential case, so the cell started
    unauthenticated with NO SIGNAL and the two causes were indistinguishable.
    FAILURE RENDERED AS ABSENCE.

    The discriminator is that we KNOW the operator had a credential: `src`
    exists and the link still failed."""
    from swarph_cli.commands import spawn as sp
    fake_home = tmp_path / "op"
    (fake_home / ".vibe").mkdir(parents=True)
    (fake_home / ".vibe" / ".env").write_text("KEY=1")
    monkeypatch.setattr(sp.Path, "home", staticmethod(lambda: fake_home))

    real_symlink = Path.symlink_to
    def boom(self, target, target_is_directory=False):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(sp.Path, "symlink_to", boom)

    sp._link_vibe_credential(tmp_path / "cell" / ".vibe" / ".env")
    err = capsys.readouterr().err
    assert "could not link the vibe credential" in err, (
        "a REAL link failure was silent — indistinguishable from 'operator has "
        "no credential', which is the intended case")
    assert "UNAUTHENTICATED" in err


def test_an_ABSENT_credential_stays_QUIET_it_is_the_intended_case(tmp_path, capsys, monkeypatch):
    """The positive leg. Without it the fix could warn on every spawn, and an
    operator with deliberately no credential would be told they had a problem
    they do not have — the inverse false signal."""
    from swarph_cli.commands import spawn as sp
    empty_home = tmp_path / "nobody"
    empty_home.mkdir()
    monkeypatch.setattr(sp.Path, "home", staticmethod(lambda: empty_home))
    sp._link_vibe_credential(tmp_path / "cell" / ".vibe" / ".env")
    assert capsys.readouterr().err == "", (
        "warned about an absent credential, which is the DOCUMENTED intended "
        "case — a false alarm on the normal path")
