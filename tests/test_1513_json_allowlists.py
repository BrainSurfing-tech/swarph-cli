"""Card #1513 — allowlisted `--json` output for `swarph mesh peers`,
`swarph cell selfcheck`, and `swarph cell probe` (A-I-D chantier 37).

Invariants pinned here:
1. `swarph mesh peers --json` prints a JSON list where each peer has EXACTLY
   the keys `{"name", "swarph_cli_version", "last_seen", "health_verdict"}`,
   built from `_PEERS_JSON_ALLOWLIST` and never by dumping the `/peers` row or
   its `capabilities` blob. `swarph_cli_version` is labeled as the
   registration-time report.
2. `swarph cell probe --json` and `swarph cell selfcheck --json` print a JSON
   list of verdicts only (`{"check", "verdict", "reason"}`), built from
   `_PROBE_JSON_ALLOWLIST` / `_SELFCHECK_JSON_ALLOWLIST`, and NEVER emit a
   value read by a probe (no path, URL, token, or file/memory content).
3. Text output without `--json` is unchanged across all three commands.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import cell, cell_probe as cp, cell_selfcheck as sc, mesh


# Exact 11 columns returned by today's mesh-gateway GET /peers (server.py:4700-4703).
# Note: last_health is a TIMESTAMP (server.py:5257), not a health status verdict.
TODAYS_GATEWAY_PEERS_COLUMNS = (
    "name",
    "url",
    "capabilities",
    "registered_at",
    "last_health",
    "last_seen",
    "enabled",
    "ratified",
    "ratified_at",
    "ratified_by",
    "ratification_reason",
)

TODAYS_GATEWAY_PEERS_PAYLOAD = {
    "peers": [
        {
            "name": "alpha-cell",
            "url": "http://10.42.0.11:8787",
            "capabilities": {
                "swarph_cli_version": "0.72.47",
                "can_claim_tasks": True,
                "health": "claude-service /health internal dump on 127.0.0.1:8787 /home/ubuntu/secret",
                "allowed_cwds_localhost": ["/home/ubuntu/private-repo"],
            },
            "registered_at": "2026-09-30T12:00:00+00:00",
            "last_health": "2026-11-09T13:55:00+00:00",
            "last_seen": "2026-11-09T14:00:00+00:00",
            "enabled": 1,
            "ratified": True,
            "ratified_at": "2026-10-01T00:00:00+00:00",
            "ratified_by": "lab-ovh",
            "ratification_reason": "bootstrap handshake",
        }
    ]
}

# FUTURE GATEWAY SHAPE (not today's GET /peers in server.py:4700-4703):
# carries a hypothetical future `last_health_status` column alongside extra
# synthetic upstream fields to verify forward-compatibility and allowlist filtering.
FAKE_PEERS_FUTURE_GATEWAY_PAYLOAD = {
    "peers": [
        {
            "name": "alpha-cell",
            "url": "http://10.42.0.11:8787",
            "enabled": 1,
            "ratified": True,
            "ratified_at": "2026-10-01T00:00:00+00:00",
            "ratified_by": "lab-ovh",
            "ratification_reason": "bootstrap handshake",
            "registered_at": "2026-09-30T12:00:00+00:00",
            "last_seen": "2026-11-09T14:00:00+00:00",
            "last_health": "2026-11-09T13:55:00+00:00",
            "last_health_status": "healthy",  # future gateway shape, not today's GET /peers
            "owner": "operator-internal",
            "extra_upstream_secret": " MUST_NEVER_LEAK_UPSTREAM_FIELD ",
            "capabilities": {
                "swarph_cli_version": "0.72.47",
                "can_claim_tasks": True,
                "health": "claude-service /health internal dump on 127.0.0.1:8787 /home/ubuntu/secret",
                "allowed_cwds_localhost": ["/home/ubuntu/private-repo"],
            },
        },
        {
            "name": "silent-cell",
            "url": "http://10.42.0.12:8787",
            "enabled": 1,
            "ratified": False,
            "last_seen": None,
            "last_health": "2026-11-09T12:00:00+00:00",
            "capabilities": {
                "can_claim_tasks": True,
                "internal_token_hint": "tok_should_not_appear",
            },
        },
    ]
}
FAKE_PEERS_PAYLOAD = FAKE_PEERS_FUTURE_GATEWAY_PAYLOAD


def test_peers_json_todays_gateway_peers_row_shape(monkeypatch, capsys):
    """Today's GET /peers row (exact columns from mesh-gateway server.py:4700-4703):
    last_health is a timestamp (server.py:5257), so health_verdict MUST be null
    (None), and url and capabilities (plus all other non-allowlisted columns)
    must not leak."""
    row = TODAYS_GATEWAY_PEERS_PAYLOAD["peers"][0]
    assert set(row.keys()) == set(TODAYS_GATEWAY_PEERS_COLUMNS)
    assert list(row.keys()) == list(TODAYS_GATEWAY_PEERS_COLUMNS)

    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (200, TODAYS_GATEWAY_PEERS_PAYLOAD),
    )

    rc = mesh.run_mesh(["peers", "--json", "--gateway", "http://127.0.0.1:8788"])
    assert rc == 0
    raw_out = capsys.readouterr().out
    data = json.loads(raw_out)

    assert data == [
        {
            "name": "alpha-cell",
            "swarph_cli_version": "0.72.47",
            "last_seen": "2026-11-09T14:00:00+00:00",
            "health_verdict": None,
        }
    ]
    assert data[0]["health_verdict"] is None

    for forbidden in (
        "url",
        "10.42.0.11",
        "8787",
        "capabilities",
        "can_claim_tasks",
        "127.0.0.1:8787",
        "/home/ubuntu",
        "private-repo",
        "registered_at",
        "2026-09-30T12:00:00+00:00",
        "last_health",
        "2026-11-09T13:55:00+00:00",
        "enabled",
        "ratified",
        "2026-10-01T00:00:00+00:00",
        "ratification_reason",
        "bootstrap handshake",
    ):
        assert forbidden not in raw_out, f"leaked {forbidden!r} from today's /peers row"


def test_peers_json_allowlist_exact_keys_and_no_leak(monkeypatch, capsys):
    """Future gateway shape fixture (carrying last_health_status alongside extra
    upstream fields): `swarph mesh peers --json` emits a JSON list where each
    peer has EXACTLY {name, swarph_cli_version, last_seen, health_verdict} and
    never leaks capabilities, last_health timestamps, or extra /peers columns."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (200, FAKE_PEERS_FUTURE_GATEWAY_PAYLOAD),
    )

    rc = mesh.run_mesh(["peers", "--json", "--gateway", "http://127.0.0.1:8788"])
    assert rc == 0
    raw_out = capsys.readouterr().out
    data = json.loads(raw_out)

    assert isinstance(data, list)
    assert len(data) == 2
    expected_keys = {"name", "swarph_cli_version", "last_seen", "health_verdict"}
    assert set(mesh._PEERS_JSON_ALLOWLIST) == expected_keys

    for item in data:
        assert set(item.keys()) == expected_keys, f"extra or missing keys in {item!r}"
        assert list(item.keys()) == list(mesh._PEERS_JSON_ALLOWLIST)

    assert data[0] == {
        "name": "alpha-cell",
        "swarph_cli_version": "0.72.47",
        "last_seen": "2026-11-09T14:00:00+00:00",
        "health_verdict": "healthy",
    }
    assert data[1] == {
        "name": "silent-cell",
        "swarph_cli_version": None,
        "last_seen": None,
        "health_verdict": None,
    }

    for forbidden in (
        "capabilities",
        "10.42.0.11",
        "127.0.0.1:8787",
        "/home/ubuntu",
        "private-repo",
        "MUST_NEVER_LEAK_UPSTREAM_FIELD",
        "tok_should_not_appear",
        "ratification_reason",
        "operator-internal",
        "2026-11-09T13:55:00+00:00",
        "2026-11-09T12:00:00+00:00",
    ):
        assert forbidden not in raw_out, f"leaked {forbidden!r} into peers --json"


def test_peers_help_labels_version_as_registration_time_report():
    parser = mesh._build_parser()
    peers_sub = [
        action
        for action in parser._actions
        if isinstance(action, pytest.importorskip("argparse")._SubParsersAction)
    ][0].choices["peers"]
    peers_help = " ".join(peers_sub.format_help().split())
    assert "--json" in peers_help
    assert "registration-time report" in peers_help
    assert "health_verdict is null on today's gateway" in peers_help
    assert "GET /peers does not return a status" in peers_help
    assert "server.py:4700-4703" in peers_help


def test_peers_text_output_without_json_is_unchanged(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (200, FAKE_PEERS_FUTURE_GATEWAY_PAYLOAD),
    )
    rc = mesh.run_mesh(["peers", "--gateway", "http://127.0.0.1:8788"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "alpha-cell  0.72.47\nsilent-cell  UNREPORTED\n"


def test_cell_probe_json_emits_verdicts_only_and_never_leaks_read_values(
    monkeypatch, tmp_path, capsys
):
    """(2) `swarph cell probe --json` emits verdicts only (check, verdict,
    reason) and never a value a probe read (no path, URL, token, or slug)."""
    cfg_file = tmp_path / "secret_claude_config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "swarph": {
                        "env": {
                            "SWARPH_BRAIN_MCP": "http://10.88.77.66:8792/mcp-secret-url"
                        }
                    }
                },
                "projects": {
                    "/home/secret-operator/top-secret-project": {
                        "mcpServers": {"swarph": {"env": {}}}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cp, "DEFAULT_CLAUDE_JSON", str(cfg_file))
    monkeypatch.setenv("SWARPH_BRAIN_MCP", "http://10.88.77.66:8792/cli-secret-url")
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://10.88.77.66:8788")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "secret-bearer-token-xyz987")
    monkeypatch.setattr(
        cp,
        "gateway_memory_list",
        lambda gw, tok, timeout=15: ["confidential-memory-slug-42"],
    )
    monkeypatch.setattr(cp, "_mcp_memory_list", lambda: [])

    # First verify text mode STILL prints the detailed diagnostic values
    rc_text = cell.run_cell(["probe"])
    assert rc_text == cp.State.ABSENT
    text_out = capsys.readouterr().out
    assert "/home/secret-operator/top-secret-project" in text_out
    assert "http://10.88.77.66:8792/cli-secret-url" in text_out
    assert "confidential-memory-slug-42" in text_out

    # Now verify --json mode prints ONLY allowlisted keys and NO read values
    rc_json = cell.run_cell(["probe", "--json"])
    assert rc_json == cp.State.ABSENT
    json_out = capsys.readouterr().out
    rows = json.loads(json_out)

    assert isinstance(rows, list)
    assert len(rows) == 3
    expected_keys = {"check", "verdict", "reason"}
    assert set(cp._PROBE_JSON_ALLOWLIST) == expected_keys

    for row in rows:
        assert set(row.keys()) == expected_keys, f"extra or missing keys in {row!r}"
        assert list(row.keys()) == list(cp._PROBE_JSON_ALLOWLIST)
        assert isinstance(row["check"], str) and row["check"]
        assert isinstance(row["verdict"], str) and row["verdict"]
        assert isinstance(row["reason"], str) and row["reason"]

    by_check = {r["check"]: r for r in rows}
    assert by_check["mcp-config"]["verdict"] == "ABSENT"
    assert by_check["cli-path"]["verdict"] == "PRESENT"
    assert by_check["gbrain-two-path"]["verdict"] == "ABSENT"

    for forbidden in (
        str(cfg_file),
        "secret_claude_config.json",
        "/home/secret-operator",
        "top-secret-project",
        "10.88.77.66",
        "8792",
        "mcp-secret-url",
        "cli-secret-url",
        "secret-bearer-token-xyz987",
        "confidential-memory-slug-42",
    ):
        assert forbidden not in json_out, f"probe --json leaked read value {forbidden!r}"


def test_cell_selfcheck_json_emits_verdicts_only_and_never_leaks_read_values(
    monkeypatch, tmp_path, capsys
):
    """(2) `swarph cell selfcheck --json` emits verdicts only (check, verdict,
    reason) and never leaks paths, URLs, tokens, or file contents."""
    surfaces = [
        {
            "name": "/etc/systemd/system/swarph-monitor-lab.service",
            "kind": "unit",
            "text": (
                "ExecStart=swarph monitor start --as lab "
                "--state-dir /home/secretuser/swarph_state/lab/mesh-sidecar "
                "--gateway http://10.77.66.55:8788\n"
            ),
            "live": True,
        },
        {
            "name": "/etc/default/swarph-lab (via swarph-monitor-lab.service)",
            "kind": "envfile",
            "text": "MESH_GATEWAY_TOKEN=tok_live_secret_value_abcdef123456\n",
            "live": True,
            "owner": "lab",
        },
        {
            "name": "(crontab)",
            "kind": "cron",
            "text": (
                "*/5 * * * * swarph watchdog --cell lab "
                "--cursor /home/secretuser/wrong_dir/cursor.json\n"
            ),
            "live": True,
            "shared": True,
        },
        {
            "kind": "coverage",
            "class": "systemd units",
            "read": True,
            "detail": "/etc/systemd/system, /home/secretuser/.config/systemd/user; 2 scanned",
        },
    ]
    resolver = {
        "gateway": "http://10.99.88.77:8788",
        "token": ("tok_resolved_different_999999", "peer file /home/secretuser/.config/swarph/lab.peer_token"),
        "listeners": set(),
        "socket_table": "read",
    }
    monkeypatch.setattr(sc, "discover_surfaces", lambda: surfaces)
    monkeypatch.setattr(sc, "resolve_cell_inputs", lambda self_name: resolver)
    decl = tmp_path / "cell_expected.lab.json"

    # Text mode without --json is unchanged and includes the diagnostic paths/URLs
    rc_text = cell.run_cell(["selfcheck", "--as", "lab", "--declaration", str(decl)])
    assert rc_text == 1
    text_out = capsys.readouterr().out
    assert "/home/secretuser/wrong_dir/cursor.json" in text_out
    assert "http://10.77.66.55:8788" in text_out

    # --json mode prints ONLY (check, verdict, reason) and zero paths/URLs/tokens
    rc_json = cell.run_cell(["selfcheck", "--as", "lab", "--declaration", str(decl), "--json"])
    assert rc_json == 1
    json_out = capsys.readouterr().out
    rows = json.loads(json_out)

    assert isinstance(rows, list)
    assert len(rows) >= 1
    expected_keys = {"check", "verdict", "reason"}
    assert set(sc._SELFCHECK_JSON_ALLOWLIST) == expected_keys

    for row in rows:
        assert set(row.keys()) == expected_keys, f"extra or missing keys in {row!r}"
        assert list(row.keys()) == list(sc._SELFCHECK_JSON_ALLOWLIST)

    verdicts_by_check = {r["check"]: r["verdict"] for r in rows}
    assert verdicts_by_check["relation:cursor-state-dir"] == "RELATION BROKEN"
    assert verdicts_by_check["resolver:gateway"] == "RESOLVER DRIFT"
    assert verdicts_by_check["resolver:token"] == "TOKEN MISMATCH"
    assert verdicts_by_check["selfcheck"] == "DRIFT"

    for forbidden in (
        "/home/secretuser",
        "wrong_dir",
        "mesh-sidecar",
        "swarph-monitor-lab.service",
        "10.77.66.55",
        "10.99.88.77",
        "tok_live_secret_value_abcdef123456",
        "tok_resolved_different_999999",
        "lab.peer_token",
    ):
        assert forbidden not in json_out, f"selfcheck --json leaked {forbidden!r}"


def test_mutant_swapping_allowlist_for_full_row_is_caught(monkeypatch, tmp_path, capsys):
    """Mutant check required by #1513: if the allowlist projection is swapped
    for the full row in mesh._peer_json_row, cell_probe._probe_json_row, or
    cell_selfcheck._selfcheck_json_row, the allowlist assertions fail."""
    orig_peer_row = mesh._peer_json_row
    orig_probe_row = cp._probe_json_row
    orig_selfcheck_row = sc._selfcheck_json_row

    # 1. mesh._peer_json_row mutant: return the raw /peers row instead of allowlist
    monkeypatch.setattr(mesh, "_peer_json_row", lambda p: dict(p))
    with pytest.raises(AssertionError):
        test_peers_json_allowlist_exact_keys_and_no_leak(monkeypatch, capsys)
    monkeypatch.setattr(mesh, "_peer_json_row", orig_peer_row)

    # 2. cell_probe._probe_json_row mutant: return the full internal probe row
    monkeypatch.setattr(cp, "_probe_json_row", lambda raw_row: dict(raw_row))
    with pytest.raises(AssertionError):
        test_cell_probe_json_emits_verdicts_only_and_never_leaks_read_values(
            monkeypatch, tmp_path, capsys
        )
    monkeypatch.setattr(cp, "_probe_json_row", orig_probe_row)

    # 3. cell_selfcheck._selfcheck_json_row mutant: return the full internal selfcheck row
    monkeypatch.setattr(sc, "_selfcheck_json_row", lambda raw_row: dict(raw_row))
    with pytest.raises(AssertionError):
        test_cell_selfcheck_json_emits_verdicts_only_and_never_leaks_read_values(
            monkeypatch, tmp_path, capsys
        )
    monkeypatch.setattr(sc, "_selfcheck_json_row", orig_selfcheck_row)
