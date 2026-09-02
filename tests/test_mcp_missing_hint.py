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
    msg = _both(monkeypatch, lambda _name: "2.1.1")
    assert "2.1.1" in msg
    assert "IS installed" in msg
    assert "is not installed" not in msg, "the two faults must not read identically"
    assert "mcp>=1.0,<2" in msg


def test_the_two_faults_produce_different_text(monkeypatch):
    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError("mcp")

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    absent = mcp_server._mcp_missing_hint()
    monkeypatch.setattr(importlib.metadata, "version", lambda _n: "2.1.1")
    too_new = mcp_server._mcp_missing_hint()
    assert absent != too_new
