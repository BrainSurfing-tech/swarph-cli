"""Smoke tests for the v0.0.1 swarph CLI scaffold.

Phase 2 tests (one-shot mode + provider routing) ship alongside that
implementation. v0.0.1 only verifies:

1. Public version exported.
2. Entry-point runs cleanly (exit 0, banner to stderr).
3. ``--version`` flag returns the expected string.
4. swarph-mesh + swarph-shared deps are resolvable.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from swarph_cli import __version__
from swarph_cli.main import main


def test_version_exported():
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2


def test_main_exits_zero(capsys):
    rc = main(argv=[])  # explicit empty argv so pytest's argv doesn't bleed in
    assert rc == 0
    captured = capsys.readouterr()
    assert "swarph" in captured.err
    assert "SCAFFOLD" in captured.err


def test_version_flag_via_subprocess():
    """``swarph --version`` should report the version string and exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "swarph_cli.main", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_swarph_mesh_dep_resolvable():
    """v0.0.1 declares swarph-mesh>=0.0.1 as a dep — verify it imports."""
    import swarph_mesh

    assert hasattr(swarph_mesh, "LLMAdapter")
    assert hasattr(swarph_mesh, "ChatMessage")


def test_swarph_shared_dep_resolvable():
    import swarph_shared

    assert hasattr(swarph_shared, "validate_node_name")
