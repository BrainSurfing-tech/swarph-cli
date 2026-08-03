"""SWARPH_SELF must be SUPPLIED to a spawned cell, not merely demanded of it."""
from __future__ import annotations

import pytest

from swarph_cli.commands import spawn


class _Cell:
    def __init__(self, name): self.name = name; self.extra = {}


def test_mesh_identity_names_the_cell():
    assert spawn._mesh_identity_env(_Cell("mistral")) == {"SWARPH_SELF": "mistral"}


def test_every_membrane_supplies_the_identity():
    """>>> MEASURED: EIGHT CONSUMERS, ZERO PRODUCERS. <<<

    onboard/ratify/mesh/monitor/daemon/brain_ask/codegraph_hook/cell_selfcheck
    all resolve credentials via ~/.config/swarph/$SWARPH_SELF.peer_token, and
    nothing in the spawn path ever set it — so a spawned cell was born unable to
    name itself. A refusal to guess is only correct when something upstream can
    satisfy it; otherwise it is a better-worded dead end.

    Pins the JOINING KEY across every provider, so adding a fifth membrane that
    forgets it fails here rather than in the field.
    """
    import inspect
    src = inspect.getsource(spawn)
    merged = src.count("_mesh_identity_env(cell)")
    git = src.count("_git_identity_env(cell))")
    assert merged >= git, (
        f"identity wired at {merged} sites but git author at {git} — a membrane "
        f"sets the git author and NOT the mesh identity"
    )


def test_it_is_not_hidden_inside_the_git_identity_helper():
    """Kept separate on purpose: a mesh identity is not a git author, and burying
    it in a git-named function is the name-vs-mechanism drift this codebase spent
    a day removing."""
    assert "SWARPH_SELF" not in spawn._git_identity_env(_Cell("x"))
