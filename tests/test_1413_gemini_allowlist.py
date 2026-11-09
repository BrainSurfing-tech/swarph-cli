"""Card #1413 — pin MODEL_ALLOWLIST['gemini-service'] in gateway/services_control.py.

Fails if MODEL_ALLOWLIST['gemini-service'] reverts to any gemini-2.5-* model.
"""
from __future__ import annotations

from swarph_cli.gateway import services_control as sc


def test_gemini_service_allowlist_pinned():
    allowed = sc.MODEL_ALLOWLIST["gemini-service"]
    assert allowed == (
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    )
    assert allowed[0] == "gemini-3.8-flash"
    assert all(not m.startswith("gemini-2.5") for m in allowed)
