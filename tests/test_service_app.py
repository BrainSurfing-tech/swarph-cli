"""Tests for the $0-service FastAPI app (skipped when fastapi isn't installed).

The subprocess is mocked, so no real subscription CLI runs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from swarph_cli.service import providers as pv  # noqa: E402
from swarph_cli.service.app import build_app  # noqa: E402


def _client(monkeypatch, stdout="hello from claude"):
    class _P:
        returncode = 0
        stderr = ""

    _P.stdout = stdout
    monkeypatch.setattr(pv.subprocess, "run", lambda *a, **k: _P())
    return TestClient(build_app("claude", "secret-tok"))


def test_health_ok(monkeypatch):
    r = _client(monkeypatch).get("/health")
    assert r.status_code == 200
    assert r.json()["provider"] == "claude"
    assert r.json()["ok"] is True


def test_delegate_requires_token(monkeypatch):
    r = _client(monkeypatch).post("/delegate", json={"prompt": "hi"})
    assert r.status_code == 401


def test_delegate_bad_token_rejected(monkeypatch):
    r = _client(monkeypatch).post("/delegate", json={"prompt": "hi"},
                                  headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_delegate_runs_and_returns_zero_cost(monkeypatch):
    r = _client(monkeypatch).post("/delegate", json={"prompt": "hi"},
                                  headers={"Authorization": "Bearer secret-tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "hello from claude"
    assert body["cost_usd"] == 0.0
    assert body["provider"] == "claude"


def test_build_app_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_app("nope", "tok")
