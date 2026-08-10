"""Muse must ship with the matching swarph-shared compatibility boundary."""

from pathlib import Path

from swarph_cli.cell import load_cell
from swarph_cli.commands.spawn import ClaudeMembrane, MEMBRANES, MuseMembrane, _validate_routing
from swarph_shared.cell import VALID_PROVIDERS


def test_muse_membrane_matches_the_shared_provider_whitelist():
    assert isinstance(MEMBRANES["muse"], MuseMembrane)
    assert isinstance(MEMBRANES["muse"], ClaudeMembrane)
    assert VALID_PROVIDERS <= set(MEMBRANES)


def test_cli_explicitly_enables_muse_for_its_matching_membrane(tmp_path):
    path = tmp_path / "cell.yaml"
    path.write_text(
        "schema_version: v1\nname: muse-1\nrole: worker\ncwd: .\nprovider: muse\n",
        encoding="utf-8",
    )
    assert load_cell(path).provider == "muse"


def test_muse_routes_through_anthropic(tmp_path):
    for index, routing in enumerate(("{}\n", "\n  native: anthropic\n")):
        path = tmp_path / f"muse-{index}.yaml"
        path.write_text(
            "schema_version: v1\nname: muse-1\nrole: worker\ncwd: .\n"
            f"provider: muse\nrouting: {routing}",
            encoding="utf-8",
        )
        _validate_routing(load_cell(path))


def test_muse_inherits_claude_assisted_memory_restore_behavior():
    assert isinstance(MEMBRANES["muse"], ClaudeMembrane)


def test_muse_injects_restored_task_with_claude_system_prompt(
    tmp_path, monkeypatch
):
    from swarph_cli.commands import memory_sync, spawn

    path = tmp_path / "cell.yaml"
    path.write_text(
        "schema_version: v1\nname: muse-1\nrole: worker\ncwd: .\n"
        "provider: muse\nassisted_memory:\n  enabled: true\n  repo: test-repo\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(memory_sync, "perform_restore", lambda _cell: "Restored task")
    monkeypatch.setattr(spawn.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(spawn, "_launch_via_tmux", lambda *_args: False)
    monkeypatch.setattr(spawn, "_relaunch_in_windows_terminal", lambda *_args: False)

    launched = []

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(
        spawn.subprocess,
        "run",
        lambda argv, **_kwargs: launched.append(argv) or Result(),
    )

    assert spawn.run_spawn([]) == 0
    claude_argv = next(argv for argv in launched if argv[0] == "claude")
    assert "--append-system-prompt" in claude_argv
    assert any("Restored task" in arg for arg in claude_argv)


def test_muse_release_requires_the_shared_compatibility_boundary():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '"swarph-shared>=0.7.0,<0.8"' in text
