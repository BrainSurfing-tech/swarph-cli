"""#124, CLIENT HALF — `swarph mesh register` submitted PARTIAL capability
blobs by default (`caps or {"can_claim_tasks": True}`), and the gateway
replaced the stored blob wholesale: a peer updating ONE field destroyed
every other advertised key. gemini-researcher's role/agent_type/
agent_version, gone to a model_default update.

The client half: register READS the peer's currently-registered capabilities
and re-submits them MERGED (submitted keys override stored; stored-only keys
survive), so updating one field no longer requires knowing the whole blob.
`--replace` is the deliberate full replace (sends full=true; the gateway's
#131 guard accepts it without the 409).

A register that CANNOT READ the registry (first registration, gateway
unreachable) proceeds with what it has — the read must never BLOCK the
write; there is nothing to destroy on a first registration.
"""
from __future__ import annotations

import pytest

import swarph_cli
from swarph_cli.commands import mesh


STORED = {"role": "creative researcher", "agent_type": "gemini-cli",
          "model_default": "gemini-3.6-flash"}


def _harness(monkeypatch, tmp_path, *, get_result, post_result=None):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    captured = {}

    def fake_get(url, token, *, timeout=10.0):
        captured["get_url"] = url
        return get_result

    def fake_post(url, body, token, *, timeout=10.0):
        captured["body"] = body
        return 200, post_result or {
            "status": "registered", "name": body["name"],
            "peer_token": None, "token_status": "existing",
        }

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    monkeypatch.setattr(mesh, "_post_json", fake_post)
    return captured


def test_reregister_merges_over_the_stored_blob(monkeypatch, tmp_path):
    """THE CARD'S SPECIMEN, CLIENT SIDE: updating model_default must not
    destroy role/agent_type — the submitted blob carries the stored keys."""
    captured = _harness(monkeypatch, tmp_path,
                        get_result=(200, {"capabilities": STORED}))
    rc = mesh.run_mesh(["register", "--as", "gemini-researcher", "--force",
                        "--capability", "model_default=\"gemini-3.7-flash\""])
    assert rc == 0
    caps = captured["body"]["capabilities"]
    assert caps["model_default"] == "gemini-3.7-flash", "the update lands"
    assert caps["role"] == "creative researcher", "the stored keys SURVIVE"
    assert caps["agent_type"] == "gemini-cli"
    assert "full" not in captured["body"], "a merge is not a deliberate replace"


def test_replace_flag_sends_full_true_and_only_the_submitted_caps(monkeypatch, tmp_path):
    captured = _harness(monkeypatch, tmp_path,
                        get_result=(200, {"capabilities": STORED}))
    rc = mesh.run_mesh(["register", "--as", "gemini-researcher", "--force",
                        "--replace", "--capability", "model_default=\"x\""])
    assert rc == 0
    assert captured["body"].get("full") is True
    submitted = captured["body"]["capabilities"]
    assert submitted == {"model_default": "x",
                         "swarph_cli_version": swarph_cli.__version__}, (
        "--replace is the deliberate wholesale replace — no merge of STORED "
        "keys (the version is always submitted, #535)")


def test_unreadable_registry_proceeds_with_submitted_caps(monkeypatch, tmp_path):
    """First registration / gateway-down: the read fails, the write proceeds
    with what the caller sent. The read must never BLOCK the write."""
    captured = _harness(monkeypatch, tmp_path,
                        get_result=(0, {"detail": "connection refused"}))
    rc = mesh.run_mesh(["register", "--as", "fresh-peer",
                        "--capability", "can_claim_tasks=true"])
    assert rc == 0
    assert captured["body"]["capabilities"] == {
        "can_claim_tasks": True,
        "swarph_cli_version": swarph_cli.__version__}


def test_no_capability_and_no_stored_keeps_the_bootstrap_default(monkeypatch, tmp_path):
    captured = _harness(monkeypatch, tmp_path,
                        get_result=(404, {"detail": "unknown peer"}))
    rc = mesh.run_mesh(["register", "--as", "fresh-peer"])
    assert rc == 0
    assert captured["body"]["capabilities"] == {
        "can_claim_tasks": True,
        "swarph_cli_version": swarph_cli.__version__}, (
        "the bootstrap default is for FIRST registrations — a peer with no "
        "stored blob still advertises something (plus its version, #535)")


def test_no_capability_with_stored_resubmits_stored_unchanged(monkeypatch, tmp_path):
    """A bare re-register (refresh url/last_seen) must not collapse the blob
    to the bootstrap default — stored caps are the merge base."""
    captured = _harness(monkeypatch, tmp_path,
                        get_result=(200, {"capabilities": STORED}))
    rc = mesh.run_mesh(["register", "--as", "gemini-researcher", "--force"])
    assert rc == 0
    assert captured["body"]["capabilities"] == {
        **STORED, "swarph_cli_version": swarph_cli.__version__}
