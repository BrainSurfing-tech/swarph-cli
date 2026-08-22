"""#535 — a cell does not report what version it runs, so no guard's coverage
can ever be measured. swarph_cli_version sat on ZERO of 28 peers: "who is
stale" was not merely unanswered but UNANSWERABLE — and "the fix propagates"
was unfalsifiable, the exact shape the falsifier week exists to kill.

THE BUILD:
1. register submits swarph_cli_version on EVERY call — a submitted key, so
   the #124 merge refreshes it on re-register and preserves it across
   partial updates.
2. `swarph mesh peers` lists the registry with each peer's reported version;
   `--stale-than X` filters to peers REPORTING older than X.
3. Peers with NO reported version are named UNREPORTED — never silently
   counted as current. Absence of data is not health; that is the falsifier
   doctrine applied to the question this card exists to make askable.

ACCEPT CHECK (the card's own): the registry answers "which peers run older
than X" from a STORED field. FAIL = the question still requires asking each
cell out of band — or worse, assuming.
"""
from __future__ import annotations

import pytest

import swarph_cli
from swarph_cli.commands import mesh


# ── the report: register carries the version ────────────────────────────────

def test_register_submits_swarph_cli_version(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    captured = {}
    monkeypatch.setattr(mesh, "_http_get_json",
                        lambda url, token, *, timeout=10.0: (404, {}))
    monkeypatch.setattr(
        mesh, "_post_json",
        lambda url, body, token, *, timeout=10.0: (
            captured.setdefault("body", body),
            (200, {"status": "registered", "name": body["name"],
                   "peer_token": None, "token_status": "existing"}))[1])
    rc = mesh.run_mesh(["register", "--as", "p535"])
    assert rc == 0
    caps = captured["body"]["capabilities"]
    assert caps["swarph_cli_version"] == swarph_cli.__version__, (
        "the version is a SUBMITTED key — the #124 merge refreshes it on "
        "re-register and it survives partial updates")


# ── the question: peers --stale-than ────────────────────────────────────────

PEERS_PAYLOAD = {"peers": [
    {"name": "current-cell", "capabilities": {"swarph_cli_version": "0.45.1"}},
    {"name": "stale-cell", "capabilities": {"swarph_cli_version": "0.44.0"}},
    {"name": "silent-cell", "capabilities": {"can_claim_tasks": True}},
]}


def _peers_harness(monkeypatch, payload=PEERS_PAYLOAD):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(mesh, "_http_get_json",
                        lambda url, token, *, timeout=10.0: (200, payload))


def test_stale_than_names_only_the_peers_reporting_older(monkeypatch, capsys):
    _peers_harness(monkeypatch)
    rc = mesh.run_mesh(["peers", "--stale-than", "0.45.1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale-cell" in out, "reports 0.44.0 < 0.45.1 — stale"
    assert "current-cell" not in out.split("UNREPORTED")[0], (
        "reports 0.45.1 — NOT stale, must not appear in the stale list")


def test_unreported_is_named_not_counted_as_current(monkeypatch, capsys):
    """THE FALSIFIER CLAUSE: a peer with no version field is UNMEASURABLE.
    Listing it as current would be assuming; omitting it would be hiding."""
    _peers_harness(monkeypatch)
    rc = mesh.run_mesh(["peers", "--stale-than", "0.45.1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "silent-cell" in out and "UNREPORTED" in out, (
        "a peer with no reported version is named as unreported — "
        "the question 'who is stale' must not pretend an answer about them")


def test_peers_without_filter_lists_everyone_with_versions(monkeypatch, capsys):
    _peers_harness(monkeypatch)
    rc = mesh.run_mesh(["peers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "current-cell" in out and "0.45.1" in out
    assert "stale-cell" in out and "0.44.0" in out
    assert "silent-cell" in out, "unreported peers still LIST, marked"


# ── the comparison ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reported,cutoff,stale", [
    ("0.44.0", "0.45.1", True),
    ("0.45.1", "0.45.1", False),   # equal is not stale
    ("0.46.0", "0.45.1", False),
    ("0.45.10", "0.45.9", False),  # numeric, not lexicographic
    ("0.9.0", "0.10.0", True),     # numeric, not lexicographic
])
def test_version_compare(reported, cutoff, stale):
    assert mesh._version_is_stale(reported, cutoff) is stale


def test_unparseable_version_is_not_stale_is_unreported():
    """A version string we cannot parse is unmeasurable, not current."""
    assert mesh._version_is_stale("not-a-version", "0.45.1") is None
    assert mesh._version_is_stale(None, "0.45.1") is None
