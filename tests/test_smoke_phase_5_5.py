"""Phase 5.5 falsifiability gate per PLAN.md §13:

    swarph onboard onboard-smoke
        → registered_unratified=true on gateway
    swarph ratify onboard-smoke --reason "smoke test"
        → ratified=true + audit row appended

Live test against the deployed mesh-gateway. Skipped unless
``MESH_GATEWAY_TOKEN`` is set in env (i.e., we have credentials to
talk to the gateway). Cleans up the smoke peer's registry row at
the end; audit row stays per drop's DM #726 #4 — append-only by
design.

The gate uses ``lab-ovh`` as the witness peer (already ratified via
the Phase 5.5 PR-A grandfather backfill) rather than the §15.6 #8
default ``science-claude`` so the test runs with no extra auth dance.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest


GATEWAY = os.environ.get("MESH_GATEWAY_URL", "http://localhost:8788")
TOKEN = os.environ.get("MESH_GATEWAY_TOKEN")
SMOKE_PEER = "onboard-smoke"
WITNESS = "lab-ovh"


pytestmark = pytest.mark.skipif(
    not TOKEN,
    reason="MESH_GATEWAY_TOKEN not set — Phase 5.5 live smoke skipped",
)


def _delete_peer(name: str) -> None:
    """Best-effort cleanup. Audit rows in peer_ratifications stay
    forever; only the registry row is removed."""
    try:
        req = urllib.request.Request(
            f"{GATEWAY}/peers/{name}",
            method="DELETE",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError:
        pass


@pytest.fixture(autouse=True)
def _cleanup_smoke_peer():
    """Make the gate idempotently re-runnable. Pre-clean any leftover
    onboard-smoke row from a prior run, then post-clean after each
    test."""
    _delete_peer(SMOKE_PEER)
    yield
    _delete_peer(SMOKE_PEER)


def test_phase_5_5_falsifiability_gate(monkeypatch, tmp_path):
    """End-to-end: synthetic peer onboards → registered_unratified=true,
    witness ratifies → ratified=true + audit row landed."""
    from swarph_cli.commands import onboard, ratify

    # Stub verify_subscription_setup so the smoke runs on hosts without
    # Claude credentials (the test isn't about Claude binary discovery).
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    # ── ONBOARD ─────────────────────────────────────────────────────
    rc = onboard.run_onboard(
        [
            SMOKE_PEER,
            "--gateway",
            GATEWAY,
            "--state-dir",
            str(tmp_path / "state"),
            "--capability",
            "can_claim_tasks=true",
        ]
    )
    assert rc == 0

    # Gateway state check: peer registered_unratified=true
    req = urllib.request.Request(
        f"{GATEWAY}/peers/{SMOKE_PEER}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        import json as _json

        peer = _json.loads(resp.read())
    assert peer["ratified"] is False, f"expected unratified, got {peer}"

    # ── RATIFY (witness=lab-ovh, grandfathered) ─────────────────────
    monkeypatch.setenv("SWARPH_WITNESS", WITNESS)
    rc = ratify.run_ratify(
        [
            SMOKE_PEER,
            "--gateway",
            GATEWAY,
            "--reason",
            "Phase 5.5 falsifiability gate smoke test",
        ]
    )
    assert rc == 0

    # Gateway state check: peer ratified=true
    req = urllib.request.Request(
        f"{GATEWAY}/peers/{SMOKE_PEER}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        import json as _json

        peer = _json.loads(resp.read())
    assert peer["ratified"] is True, f"expected ratified, got {peer}"
    assert peer["ratified_by"] == WITNESS

    # Audit log check: at least one row recording the witness flip
    req = urllib.request.Request(
        f"{GATEWAY}/peers/{SMOKE_PEER}/ratifications",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        import json as _json

        audit = _json.loads(resp.read())
    rows = audit["ratifications"]
    assert len(rows) >= 1
    assert rows[-1]["ratified_by"] == WITNESS
    assert "Phase 5.5 falsifiability gate" in (rows[-1].get("reason") or "")

    # Cleanup of the handshake template file (smoke artifact)
    from pathlib import Path

    Path(f"/tmp/{SMOKE_PEER}-handshake.md").unlink(missing_ok=True)
