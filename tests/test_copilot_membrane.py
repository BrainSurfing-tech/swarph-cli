"""CopilotMembrane — GitHub Copilot CLI as a durable swarph CELL (card #386).

Grounded against `GitHub Copilot CLI 1.0.89` (`copilot --help`, 2026-10-09).
The card body measured 1.0.78, which still had `--no-banner`. 1.0.89 makes the
banner opt-in via `--banner`, so the membrane omits that flag.

  · `--session-id <id>` both resumes and sets the UUID of a new session. One
    flag. The cell declares it. There is no mtime inference.
  · `--add-dir` and `--deny-tool` exist. `--allow-all` / `--yolo` /
    `--allow-all-tools` / `--allow-all-paths` / `--allow-all-urls` are the
    allow-all surface and are never the default.
  · `-p/--prompt` is non-interactive and EXITS. A cell is a TUI, so the starter
    goes through `-i/--interactive`, which starts interactive mode and runs
    the prompt.
  · `copilot login` wipes credentials when probed. The membrane never runs it.

>>> NOT A SUBCLASS OF ClaudeMembrane. <<< MuseMembrane inherited Claude's
grammar and launched the wrong binary's flags (card #382).

>>> RELEASE ORDER, per the #247 outage: this membrane is registered and added
to CLI_ENABLED_PROVIDERS. `copilot` does NOT enter swarph_shared.VALID_PROVIDERS
in this change. The spawn guard is a SUBSET check, so an extra membrane is
inert; the reverse raises at import and kills `swarph spawn`. <<<

G1–G4 (the card's integration narrative) are not claimed here. G4 needs the
detector from card #384, which does not exist yet. A membrane that builds the
right argv is not an integrated membrane, and the live `copilot` peer is not
registered by this change: the installed CLI would reject `provider: copilot`
until this release is on the box.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from swarph_cli.cell import CLI_ENABLED_PROVIDERS, CellError, load_cell
from swarph_cli.commands.spawn import (
    MEMBRANES,
    ClaudeMembrane,
    CopilotMembrane,
    ProviderMembrane,
    _build_copilot_argv,
    _validate_routing,
)

_ALLOW_ALL = (
    "--allow-all",
    "--yolo",
    "--allow-all-tools",
    "--allow-all-paths",
    "--allow-all-urls",
)
_CLAUDE_ONLY = ("--append-system-prompt", "--dangerously-skip-permissions")


def _cell(tmp_path, starter=None):
    return types.SimpleNamespace(
        cwd=tmp_path,
        name="copilot-1",
        provider="copilot",
        role="worker",
        starter_prompt_path=starter,
        sandbox=None,
        extra={},
    )


def _argv(tmp_path, *, session_id="11111111-1111-1111-1111-111111111111",
          no_starter=True, passthrough=None, starter=None):
    return _build_copilot_argv(
        _cell(tmp_path, starter=starter),
        session_id,
        no_starter,
        list(passthrough or []),
    )


# ── REGISTRATION AND THE ORDERING INVARIANT ─────────────────────────────────

def test_copilot_is_registered_and_the_membrane_is_AHEAD_of_the_whitelist():
    """The safe direction. Extra membrane is inert; a whitelist entry with no
    membrane crashes every install at import."""
    from swarph_shared.cell import VALID_PROVIDERS

    assert isinstance(MEMBRANES["copilot"], CopilotMembrane)
    assert "copilot" not in VALID_PROVIDERS
    assert not (set(VALID_PROVIDERS) - set(MEMBRANES))


def test_copilot_is_enabled_by_the_CLI_so_the_membrane_is_not_inert(tmp_path):
    """`load_cell` gates on CLI_ENABLED_PROVIDERS. Without the enablement a
    `provider: copilot` cell.yaml is rejected and the membrane is unreachable."""
    assert "copilot" in CLI_ENABLED_PROVIDERS
    path = tmp_path / "cell.yaml"
    path.write_text(
        "schema_version: v1\nname: copilot-1\nrole: worker\ncwd: .\nprovider: copilot\n",
        encoding="utf-8",
    )
    assert load_cell(path).provider == "copilot"


def test_copilot_is_NOT_a_subclass_of_ClaudeMembrane():
    """Card #382: inheriting Claude's grammar launches the wrong flags."""
    assert not issubclass(CopilotMembrane, ClaudeMembrane)
    assert isinstance(MEMBRANES["copilot"], ProviderMembrane)
    assert not isinstance(MEMBRANES["copilot"], ClaudeMembrane)


def test_copilot_does_NOT_override_launch_or_pre_launch():
    """Linux-tmux / Windows-psmux parity is inherited. Per-provider overrides
    of the hoist were the discrimination cards #318 and #2 removed."""
    assert "pre_launch" not in CopilotMembrane.__dict__
    assert "launch" not in CopilotMembrane.__dict__
    assert "env_builder" not in CopilotMembrane.__dict__
    assert CopilotMembrane.pre_launch is ProviderMembrane.pre_launch
    assert CopilotMembrane.launch is ProviderMembrane.launch


def test_copilot_PINS_a_session():
    """`--session-id` is declared, not inferred. Codex infers and has resumed
    the wrong session; this membrane must not take the fresh-session path."""
    assert MEMBRANES["copilot"].uses_pinned_session() is True


def test_routing_native_copilot_is_accepted_and_a_mismatch_is_refused(tmp_path):
    for routing in ("{}\n", "\n  native: copilot\n"):
        path = tmp_path / f"ok-{len(routing)}.yaml"
        path.write_text(
            "schema_version: v1\nname: copilot-1\nrole: worker\ncwd: .\n"
            f"provider: copilot\nrouting: {routing}",
            encoding="utf-8",
        )
        _validate_routing(load_cell(path))

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: v1\nname: copilot-1\nrole: worker\ncwd: .\n"
        "provider: copilot\nrouting:\n  native: anthropic\n",
        encoding="utf-8",
    )
    with pytest.raises(CellError):
        _validate_routing(load_cell(bad))


# ── ARGV: PIN, CONTAIN, NEVER ALLOW-ALL ─────────────────────────────────────

def test_argv_pins_the_session_and_contains_the_cell(tmp_path):
    sid = "22222222-2222-2222-2222-222222222222"
    argv = _argv(tmp_path, session_id=sid)
    assert argv[0] == "copilot"
    assert f"--session-id={sid}" in argv
    # `.` not the absolute cwd: launch chdirs first, and an absolute path in
    # argv is the #314 Windows re-split. The directory is still declared.
    assert "--add-dir=." in argv
    assert str(tmp_path) not in " ".join(argv)
    assert "--deny-tool=shell(git push)" in argv
    assert "--no-color" in argv
    assert "--no-mouse" in argv
    assert "--no-ask-user" in argv
    assert "--no-auto-update" in argv
    assert "--no-remote" in argv


def test_argv_never_defaults_to_allow_all_or_claude_flags(tmp_path):
    argv = _argv(tmp_path, passthrough=["--model", "gpt-5"])
    joined = " ".join(argv)
    for flag in _ALLOW_ALL + _CLAUDE_ONLY:
        assert flag not in joined
    assert "--banner" not in argv
    assert "--no-banner" not in argv
    assert "-p" not in argv
    assert "--prompt" not in argv
    assert "--model" in argv  # passthrough still lands


def test_a_dash_prefixed_session_id_is_not_eaten_as_a_flag(tmp_path):
    """The `=` form is load-bearing. `--session-id -foo` is two argv slots and
    the CLI reads `-foo` as its own flag."""
    argv = _argv(tmp_path, session_id="-not-a-flag")
    assert "--session-id=-not-a-flag" in argv
    assert "-not-a-flag" not in argv


def test_starter_uses_interactive_not_prompt_which_exits(tmp_path):
    """`-p/--prompt` runs one turn and exits. A cell stays in the TUI, so the
    starter is `--interactive`, which starts interactive mode and runs it."""
    starter = tmp_path / "starter.md"
    starter.write_text("hello cell\n", encoding="utf-8")
    argv = _argv(tmp_path, no_starter=False, starter=starter)
    assert any(a.startswith("--interactive=") and "hello cell" in a for a in argv)
    assert "-p" not in argv
    assert "--prompt" not in argv


def test_no_starter_omits_interactive(tmp_path):
    starter = tmp_path / "starter.md"
    starter.write_text("should not appear\n", encoding="utf-8")
    argv = _argv(tmp_path, no_starter=True, starter=starter)
    assert not any(a.startswith("--interactive=") for a in argv)


def test_binary_lookup_is_which_copilot_and_the_message_refuses_login(monkeypatch):
    """resolve_binary is shutil.which('copilot') only. The not-found message
    must say the membrane never runs `copilot login`."""
    seen = {}

    def fake_which(name):
        seen["name"] = name
        return "/usr/bin/copilot"

    monkeypatch.setattr("swarph_cli.commands.spawn.shutil.which", fake_which)
    assert MEMBRANES["copilot"].resolve_binary() == "/usr/bin/copilot"
    assert seen["name"] == "copilot"
    msg = MEMBRANES["copilot"].binary_not_found_message()
    assert "copilot login" in msg


def test_memory_files_are_the_cell_AGENTS_md_never_the_copilot_home(tmp_path):
    """~/.copilot holds policy and auth. Syncing it would copy credentials
    into the cell's memory set."""
    cell = _cell(tmp_path)
    assert MEMBRANES["copilot"].memory_sync_files(cell) == []
    (tmp_path / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    files = MEMBRANES["copilot"].memory_sync_files(cell)
    assert files == [("AGENTS.md", tmp_path / "AGENTS.md")]
    assert MEMBRANES["copilot"].memory_guard_file(cell) == tmp_path / "AGENTS.md"
    assert all(".copilot" not in str(p) for _, p in files)
