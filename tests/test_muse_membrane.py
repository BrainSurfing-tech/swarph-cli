"""Muse must ship with the matching swarph-shared compatibility boundary."""

from pathlib import Path

from swarph_cli.commands.spawn import ClaudeMembrane, MEMBRANES, MuseMembrane
from swarph_shared.cell import VALID_PROVIDERS


def test_muse_membrane_matches_the_shared_provider_whitelist():
    assert isinstance(MEMBRANES["muse"], MuseMembrane)
    assert isinstance(MEMBRANES["muse"], ClaudeMembrane)
    assert VALID_PROVIDERS <= set(MEMBRANES)


def test_muse_release_requires_the_shared_compatibility_boundary():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '"swarph-shared>=0.7.0,<0.8"' in text
