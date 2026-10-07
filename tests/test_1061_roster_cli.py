"""card #1061 — ``swarph roster`` list + set against a faked gateway transport."""
from __future__ import annotations

from swarph_cli.commands import roster


def _env(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw.test:8788")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def _fake_get(monkeypatch, payload, status=200):
    calls = []

    def _fake(url, token, **kw):
        calls.append((url, token))
        return status, payload

    monkeypatch.setattr(roster, "_http_get_json", _fake)
    return calls


def _fake_put(monkeypatch, payload=None, status=200):
    calls = []

    def _fake(url, body, token, **kw):
        calls.append((url, body, token))
        return status, ({"peer": "p", "updated_by": "shared-token",
                         **body} if payload is None else payload)

    monkeypatch.setattr(roster, "_http_put_json", _fake)
    return calls


ROWS = {"roster": [
    {"peer": "meta-muse", "model": None, "effort": "MAX", "roles": ["build"],
     "quota_pool": None, "quota_used_pct": 4.0,
     "quota_resets_at": None, "quota_as_of": "2026-10-07T17:00:00Z",
     "unavailable_until": None, "note": None,
     "updated_by": "seed #1061", "updated_at": "t"},
    {"peer": "grok-researcher", "model": None, "effort": None, "roles": None,
     "quota_pool": None, "quota_used_pct": None,
     "quota_resets_at": None, "quota_as_of": None,
     "unavailable_until": "2026-10-09T22:37:00Z", "note": None,
     "updated_by": "seed #1061", "updated_at": "t"},
]}


def test_list_prints_one_line_per_peer(monkeypatch, capsys):
    _env(monkeypatch)
    _fake_get(monkeypatch, ROWS)
    assert roster.run_roster([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0].startswith("meta-muse") and "roles=build" in out[0]
    assert "out-until=2026-10-09T22:37:00Z" in out[1]


def test_list_peer_filter_and_missing(monkeypatch, capsys):
    _env(monkeypatch)
    _fake_get(monkeypatch, ROWS)
    assert roster.run_roster(["--peer", "grok-researcher"]) == 0
    assert capsys.readouterr().out.startswith("grok-researcher")
    assert roster.run_roster(["--peer", "nobody"]) == 1


def test_list_gateway_error_is_exit_1(monkeypatch, capsys):
    _env(monkeypatch)
    _fake_get(monkeypatch, {"detail": "nope"}, status=403)
    assert roster.run_roster([]) == 1
    assert "403" in capsys.readouterr().err


def test_set_sends_partial_body(monkeypatch, capsys):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    rc = roster.run_roster(["set", "grok-researcher",
                            "--until", "2026-10-09T22:37Z",
                            "--note", "quota out"])
    assert rc == 0
    url, body, token = calls[0]
    assert url == "http://gw.test:8788/roster/grok-researcher"
    assert body == {"unavailable_until": "2026-10-09T22:37Z",
                    "note": "quota out"}
    assert token == "tok"
    assert "shared-token" in capsys.readouterr().out


def test_set_clear_until_sends_null(monkeypatch):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    assert roster.run_roster(["set", "grok-researcher", "--clear-until"]) == 0
    assert calls[0][1] == {"unavailable_until": None}


def test_set_quota_without_as_of_refused_client_side(monkeypatch, capsys):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    assert roster.run_roster(["set", "p", "--quota-used", "70"]) == 2
    assert roster.run_roster(["set", "p", "--resets", "2026-10-13"]) == 2
    assert calls == []  # refused before any transport
    assert "as-of" in capsys.readouterr().err


def test_set_quota_with_as_of_sends_all_three(monkeypatch):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    assert roster.run_roster(["set", "cursor-lin", "--quota-pool", "Cursor",
                              "--quota-used", "70",
                              "--quota-as-of", "2026-10-07T17:00:00Z",
                              "--resets", "2026-10-13"]) == 0
    assert calls[0][1] == {"quota_pool": "Cursor", "quota_used_pct": 70.0,
                           "quota_as_of": "2026-10-07T17:00:00Z",
                           "quota_resets_at": "2026-10-13"}


def test_set_unknown_role_refused_client_side(monkeypatch, capsys):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    assert roster.run_roster(["set", "p", "--roles", "build,pilot"]) == 2
    assert calls == []
    assert "pilot" in capsys.readouterr().err


def test_set_gateway_403_names_the_gate(monkeypatch, capsys):
    _env(monkeypatch)
    _fake_put(monkeypatch, {"detail": "roster PUT requires an operator"},
              status=403)
    assert roster.run_roster(["set", "p", "--model", "M"]) == 1
    assert "403" in capsys.readouterr().err


def test_set_with_no_fields_is_usage_error(monkeypatch, capsys):
    _env(monkeypatch)
    calls = _fake_put(monkeypatch)
    assert roster.run_roster(["set", "p"]) == 2
    assert calls == []
