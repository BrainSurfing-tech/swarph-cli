"""#945 — same-line pairing refuses a second address or unit instead of last-wins.

If false this reads as a paired unit_bind. On main (33be362) the dict is keyed
by kind, so the later listen_addr or unit_state silently replaces the earlier.
"""
from __future__ import annotations

from swarph_cli.dreaming.candidates import _escalate_bindings, extract_candidates

GPT_SERVICE_LINE_19 = """**Status (2026-05-26): LIVE.** Commander ran `codex login` (ChatGPT subscription → `/home/ubuntu/.codex/auth.json`, owned by ubuntu) + explicitly authorized exposure ("use codex auth ... for the codex service" — the systemd-enable + tailnet-exposure was classifier-blocked until that explicit authorization, correctly; "logged in" alone didn't cover standing up the RCE-surface endpoint). systemd `gpt-service.service` **active + enabled**; binds the **Tailscale IP only** (100.107.222.72:8789, NOT public 0.0.0.0) ⚠️**SUPERSEDED 2026-09-23 (measured, ss + .env): now binds `0.0.0.0:8789` (`~/gpt-service/.env` HOST=0.0.0.0, file mtime 2026-09-12); public reach is blocked ONLY by ufw default-deny (no 8789 allow rule), not by the bind. Lab's tailnet IP is now 100.64.189.91. Not verified from an outside vantage.**; registered in mesh-gateway as peer **gpt-ovh** (url http://gpt-ovh:8789). Real `/delegate` round-trips PONG in ~8s at $0/call on the subscription default model. WebSocket-failed flag was a red herring (HTTPS fallback works). NB: `codex login status` writes "Logged in using ChatGPT" to **stderr** (stdout empty) — `_detect_codex_authed()` reads BOTH streams (initial bug: stdout-only → false-negative authed)."""


def _row(kind, ref, line_no=19, context="ctx"):
    return {"file": "project_gpt_service.md", "line": line_no, "kind": kind,
            "ref": ref, "asserted": None, "context": context}


def _binds(rows):
    return [r for r in _escalate_bindings(rows) if r["kind"] == "unit_bind"]


def _reasons(rows):
    return [r["reason"] for r in _escalate_bindings(rows) if r["kind"] == "unprobeable"]


def test_a_stale_then_correction_is_unprobeable():
    # if false this reads unit_bind asserted=0.0.0.0:8789 (the later address)
    rows = [
        _row("listen_addr", "100.107.222.72:8789"),
        _row("listen_addr", "0.0.0.0:8789"),
        _row("unit_state", "gpt-service.service"),
    ]
    assert _binds(rows) == []
    assert "multiple_addresses" in _reasons(rows)
    assert len([r for r in _escalate_bindings(rows) if r["kind"] == "listen_addr"]) == 2


def test_b_correction_then_stale_is_unprobeable():
    # if false this reads unit_bind asserted=100.107.222.72:8789 (the STALE address)
    rows = [
        _row("listen_addr", "0.0.0.0:8789"),
        _row("listen_addr", "100.107.222.72:8789"),
        _row("unit_state", "gpt-service.service"),
    ]
    assert _binds(rows) == []
    assert "multiple_addresses" in _reasons(rows)


def test_c_one_unit_one_address_still_binds():
    # if false this reads no unit_bind (the single-pair path was broken)
    rows = [
        _row("unit_state", "gpt-service.service", context="binds 100.64.189.91:8789"),
        _row("listen_addr", "100.64.189.91:8789", context="binds 100.64.189.91:8789"),
    ]
    binds = _binds(rows)
    assert len(binds) == 1
    assert binds[0]["ref"] == "gpt-service.service"
    assert binds[0]["asserted"] == "100.64.189.91:8789"
    assert "multiple_addresses" not in _reasons(rows)


def test_d_two_units_one_address_is_multiple_units():
    # if false this reads unit_bind ref=<whichever .service came last>
    rows = [
        _row("unit_state", "gpt-service.service"),
        _row("unit_state", "mesh-gateway.service"),
        _row("listen_addr", "0.0.0.0:8789"),
    ]
    assert _binds(rows) == []
    assert "multiple_units" in _reasons(rows)


def test_e_gpt_service_line_19_verbatim_is_refused(tmp_path):
    # if false this reads a unit_bind of gpt-service.service to one of the two addresses
    (tmp_path / "project_gpt_service.md").write_text(GPT_SERVICE_LINE_19 + "\n", encoding="utf-8")
    rows = extract_candidates(tmp_path)
    assert any(r["kind"] == "unprobeable" and r["reason"] == "multiple_addresses" for r in rows)
    assert not any(r["kind"] == "unit_bind" for r in rows)
    raw_addrs = [r["ref"] for r in rows if r["kind"] == "listen_addr"]
    assert "100.107.222.72:8789" in raw_addrs
    assert "0.0.0.0:8789" in raw_addrs
