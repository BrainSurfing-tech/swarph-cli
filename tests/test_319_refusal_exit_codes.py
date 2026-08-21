"""#319 — a refusal that exits 0 is INVISIBLE to `set -e`, CI, and every
scripted install guide: the tool reports that it did the thing it refused to
do. Measured on 0.41.6 from PyPI in a clean venv: three consecutive failures,
three exit-0s.

The class was fixed in code between 0.41.6 and 0.45.x — but NOTHING PINNED IT,
and an unpinned fix is one refactor from regressing with no signal. This file
is the net: every refusal path in init/spawn/onboard asserts a NON-ZERO exit.

The one LIVE case found by the re-measure (2026-08-21, main @ post-#286):
bare `swarph spawn` with no cell, no role, and no discoverable cell.yaml
printed the usage and returned 0 — now 2, matching init's refusal convention.
"""
from __future__ import annotations

import sys

import pytest

from swarph_cli.commands import init as init_mod
from swarph_cli.commands import onboard as onboard_mod
from swarph_cli.commands import spawn as spawn_mod


# ── init ────────────────────────────────────────────────────────────────────

def test_init_no_name_refuses_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = init_mod.run_init([])
    assert rc != 0, "a refusal that exits 0 reports success to set -e"


def test_init_name_without_provider_noninteractive_refuses_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = init_mod.run_init(["c319", "--cwd", str(tmp_path), "-y"])
    assert rc != 0


# ── spawn ───────────────────────────────────────────────────────────────────

def test_spawn_bare_with_nothing_to_spawn_refuses_nonzero(tmp_path, monkeypatch):
    """THE LIVE ONE. Bare `swarph spawn` in an empty HOME/cwd: nothing to
    spawn, so it prints the usage — and used to return 0, telling a script
    the spawn HAPPENED. Usage-on-empty is a refusal (rc 2, like init), not a
    success."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(spawn_mod, "discover_cell_in_cwd", lambda: None)
    rc = spawn_mod.run_spawn([])
    assert rc == 2, f"bare spawn with nothing to spawn must refuse, got rc={rc}"


def test_spawn_nonexistent_cell_refuses_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    rc = spawn_mod.run_spawn(["no-such-cell-319"])
    assert rc != 0


def test_spawn_provider_binary_absent_is_127(tmp_path, monkeypatch):
    """The card's headline case: 'binary not found on PATH' must exit 127 (the
    conventional command-not-found), never 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = init_mod.run_init(["c319b", "--provider", "claude", "--cwd",
                            str(tmp_path), "-y"])
    assert rc == 0
    monkeypatch.chdir(tmp_path)
    membrane = spawn_mod.MEMBRANES["claude"]
    monkeypatch.setattr(membrane, "resolve_binary", lambda: None)
    rc = spawn_mod.run_spawn(["c319b", "--no-banner"])
    assert rc == 127, f"binary-not-found must be 127, got rc={rc}"


# ── onboard ─────────────────────────────────────────────────────────────────

def test_onboard_no_args_refuses_nonzero(tmp_path, monkeypatch):
    """onboard's peer is a required positional — argparse raises SystemExit(2)
    rather than run_onboard returning. Either shape is a non-zero refusal;
    what must never happen is a 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        rc = onboard_mod.run_onboard([])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    assert rc != 0


# ── the doc/requirement agreement (the card's fix b) ────────────────────────

def test_getting_started_names_the_command_that_actually_works(capsys):
    """`swarph init <name>` alone FAILS non-interactively (--provider is
    required) — so the getting-started block must name the command that
    works, not the one that prompts. The doc and the requirement may not
    disagree."""
    from swarph_cli import main as main_mod
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    gs = out[out.index("getting started:"):]
    init_line = next(l for l in gs.splitlines() if "swarph init" in l)
    assert "--provider" in init_line, (
        "the documented first command must work non-interactively: "
        f"{init_line.strip()!r} omits --provider and fails outside a TTY")


def test_init_next_line_scopes_mesh_steps_to_mesh_users(tmp_path, capsys):
    """The card's fix (d): a standalone outsider has no mesh, needs no mesh,
    and cannot ratify — so the mesh path must be LABELED as conditional, not
    presented as the next thing to do. `swarph spawn <name>` is the step that
    works with zero mesh."""
    rc = init_mod.run_init(["c319c", "--provider", "claude", "--cwd",
                            str(tmp_path), "-y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"swarph spawn c319c" in out
    next_line = next(l for l in out.splitlines() if l.strip().startswith("next:"))
    assert "if you're joining a mesh" in next_line.lower() or \
           "if joining a mesh" in next_line.lower(), (
        "the mesh path must be marked conditional for an outsider: "
        f"{next_line.strip()!r}")
