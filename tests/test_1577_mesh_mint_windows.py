"""#1577 — native Windows refuses ``swarph mesh mint`` before any register.

This module stays free of fastapi, jwt, and uvicorn. The Windows CI leg
installs only ``.[dev,mcp]``, which does not include fastapi, so a test
that importorskips those packages never runs there. On win32 this test is
not skipped. The node-id floor in ``.github/workflows/windows.yml`` fails
the leg if it was.
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="not win32")


def test_mesh_mint_refuses_native_windows_before_register(monkeypatch, tmp_path, capsys):
    import os

    from swarph_cli.commands import mesh

    posted: list = []

    def _post(*args, **kwargs):
        posted.append(args)
        raise AssertionError("register POST must not run on native Windows")

    def _fchmod(*args, **kwargs):
        raise AssertionError("os.fchmod must not be called on native Windows")

    monkeypatch.setattr(mesh, "_post_json", _post)
    # Windows Python 3.11/3.12 has no os.fchmod. raising=False installs the
    # guard anyway, so a mint that reaches the call fails here instead of
    # dying in setattr before the command runs.
    monkeypatch.setattr(os, "fchmod", _fchmod, raising=False)

    out = tmp_path / "win-cell.peer_token"
    rc = mesh.run_mesh([
        "mint",
        "win-cell",
        "--out",
        str(out),
        "--gateway",
        "http://127.0.0.1:9",
        "--token-file",
        str(tmp_path / "missing-operator-token"),
    ])
    captured = capsys.readouterr()
    assert rc != 0
    assert "Linux" in captured.err
    assert "WSL" in captured.err
    assert posted == []
    assert not out.exists()
