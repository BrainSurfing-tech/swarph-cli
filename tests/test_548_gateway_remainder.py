"""Card #548 — the default-gateway defect remainder, swarph-cli leg.

#546 fixed the mesh-gateway default and left a source sweep guarding port
8788. This card is the remainder: the gbrain endpoint (8792) had the same
loopback-default shape, and the shipped systemd templates seeded every future
install with the MagicDNS name contra the commander's 2026-08-21 IP-over-name
decision. Both are defaults that only work where a human already fixed the
environment — invisible to exactly the people who would review them.

Measured 2026-08-23 on the gateway box: gbrain listens on
100.107.222.72:8792 ONLY; 127.0.0.1:8792 refuses. A loopback default was
deaf on every box INCLUDING the one running the service.

Run: .venv/bin/python -m pytest tests/test_548_gateway_remainder.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _src_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "swarph_cli"


def test_no_loopback_gbrain_default_anywhere_in_src():
    """No file under src/ may name a loopback gbrain endpoint as a DEFAULT."""
    offenders = []
    for path in _src_root().rglob("*.py"):
        # encoding="utf-8" is load-bearing: without it Windows reads with the
        # locale codec and the sweep crashes on the first non-ASCII byte.
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            # A COMMENT MAY NAME THE DEFECT IT DESCRIBES (same convention as
            # #546's sweep): brain_ask.py explains this exact bug.
            if stripped.startswith("#"):
                continue
            if "8792" not in line:
                continue
            if "localhost:8792" in line or "127.0.0.1:8792" in line:
                offenders.append(
                    f"{path.relative_to(_src_root())}:{i}: {line.strip()[:90]}"
                )
    assert not offenders, (
        "a loopback gbrain default survives in src/ — gbrain binds the "
        "tailnet IP only (measured 2026-08-23), so this fails as a bare "
        "'Connection refused' with no cause named:\n  " + "\n  ".join(offenders)
    )


def test_shipped_systemd_templates_use_the_tailnet_ip_not_the_magicdns_name():
    """Templates seed every future install; they must ship the strong choice."""
    offenders = []
    for path in _src_root().joinpath("systemd").glob("*.default"):
        for i, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.strip().startswith("#"):
                continue
            if "lab-ovh" in line:
                offenders.append(f"{path.name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "a shipped template still names the MagicDNS host — the IP needs only "
        "tailscale up; the name needs MagicDNS + search domain + no collision "
        "(commander 2026-08-21):\n  " + "\n  ".join(offenders)
    )


def test_the_gbrain_sweep_can_fail(tmp_path, monkeypatch):
    """>>> PROVE THE SWEEP FIRES. <<< A detector that has only ever seen a clean
    tree is indistinguishable from one that matches nothing."""
    fake = tmp_path / "swarph_cli"
    fake.mkdir()
    (fake / "bad.py").write_text(
        '_DEFAULT_GBRAIN = "http://127.0.0.1:8792/mcp"\n', encoding="utf-8"
    )
    monkeypatch.setattr(f"{__name__}._src_root", lambda: fake)
    with pytest.raises(AssertionError):
        test_no_loopback_gbrain_default_anywhere_in_src()
