"""#649: one `except ImportError` catches two different worlds; the hint must say which.

The FAIL condition is a message that reads the same whether the SDK is absent or
merely too new — that is the defect this replaced (drop-on-meta-edge, 2026-09-02),
not a formatting preference. The second assert in each case pins the BOUND: an
unbounded `mcp>=1.0` resolves to 2.x, so the old hint's remedy reproduced the
failure it diagnosed.
"""
import importlib.metadata

import pytest

from swarph_cli.commands import mcp_server


def _both(monkeypatch, fake):
    monkeypatch.setattr(importlib.metadata, "version", fake)
    return mcp_server._mcp_missing_hint()


def test_absent_sdk_says_not_installed_and_bounds_the_remedy(monkeypatch):
    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError("mcp")

    msg = _both(monkeypatch, _raise)
    assert "is not installed" in msg
    assert "mcp>=1.0,<2" in msg
    assert "mcp>=1.0'" not in msg, "unbounded remedy reproduces the failure it diagnoses"


def test_too_new_sdk_names_the_version_and_does_not_claim_absence(monkeypatch):
    # The too-new claim is only made on EVIDENCE that fastmcp is what failed —
    # a version alone does not license it (see the third-world case below).
    monkeypatch.setattr(
        mcp_server,
        "_MCP_IMPORT_ERROR",
        ImportError("No module named 'mcp.server.fastmcp'", name="mcp.server.fastmcp"),
    )
    msg = _both(monkeypatch, lambda _name: "2.1.1")
    assert "2.1.1" in msg
    assert "IS installed" in msg
    assert "is not installed" not in msg, "the two faults must not read identically"
    assert "mcp>=1.0,<2" in msg


def test_the_three_worlds_produce_three_different_texts(monkeypatch):
    """Identical text IS the defect. Assert they differ, not that each parses."""

    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError("mcp")

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    absent = mcp_server._mcp_missing_hint()

    monkeypatch.setattr(importlib.metadata, "version", lambda _n: "2.1.1")
    monkeypatch.setattr(
        mcp_server,
        "_MCP_IMPORT_ERROR",
        ImportError("No module named 'mcp.server.fastmcp'", name="mcp.server.fastmcp"),
    )
    too_new = mcp_server._mcp_missing_hint()

    monkeypatch.setattr(importlib.metadata, "version", lambda _n: "1.29.1")
    monkeypatch.setattr(
        mcp_server, "_MCP_IMPORT_ERROR", ImportError("No module named 'anyio'", name="anyio")
    )
    other = mcp_server._mcp_missing_hint()

    assert len({absent, too_new, other}) == 3


def test_third_world_transitive_import_failure_is_not_filed_as_too_new(monkeypatch):
    """drop-on-meta-edge's NIT on PR #369: the `except ImportError` wraps the whole
    tool-definition block, so an import failure UNDER a supported mcp is a third
    world. Two branches would misfile it as "too new — pin it back", sending the
    reader to a downgrade that cannot help. It must surface what was raised."""
    monkeypatch.setattr(importlib.metadata, "version", lambda _n: "1.29.1")
    monkeypatch.setattr(
        mcp_server, "_MCP_IMPORT_ERROR", ImportError("No module named 'anyio'", name="anyio")
    )
    msg = mcp_server._mcp_missing_hint()
    assert "anyio" in msg
    assert "pin it back" not in msg.lower()
    assert "mcp>=1.0,<2" not in msg, "a version pin cannot fix a transitive import failure"
