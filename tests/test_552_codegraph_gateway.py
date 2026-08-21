"""`swarph codegraph` gains the search-relay path its siblings already had.

WHY (2026-08-21). droplet ran `swarph codegraph edge` and got nothing, while the
same query on lab-ovh returned 8 matches. Neither a config mistake nor a rights
problem -- his peer token queried the gateway fine by raw HTTP. The verb simply
had NO gateway concept:

    swarph codegraph [-h] [--index] [--caller-cell] [--limit] [--json]

Compare `brain-ask` (--gateway, $SWARPH_BRAIN_GATEWAY) and `highlight`
(--gateway, $SWARPH_HIGHLIGHT_GATEWAY/$SWARPH_GATEWAY/$SWARPH_BRAIN_GATEWAY),
both built gateway-first. codegraph was built local-first, so every cell reads
its OWN index and a cell without one gets silence.

>>> THE GATEWAY ENDPOINT ALREADY EXISTED -- POST /codegraph, authorised, with the
caller taken from the token and no spoofable caller field. AN ENDPOINT NOTHING
COULD CALL: the exact inverse of card #496, where a published verb called an
endpoint that did not exist. Same missing join, opposite direction. <<<

A RELAY rather than a shipped index, per the commander: the server searches and
returns only the answer. One index, one truth -- copying the db to every cell
makes N stores that drift, which is the divergence this fleet hit four separate
ways in the same week.
"""

import json
import os

import pytest

from swarph_cli.commands import codegraph as cg


def test_gateway_resolves_through_the_same_env_chain_as_highlight(monkeypatch):
    """ZERO new per-cell config: a cell already carrying SWARPH_GATEWAY from the
    brain-ask rollout gets relayed search for free."""
    for k in ("SWARPH_CODEGRAPH_GATEWAY", "SWARPH_GATEWAY", "SWARPH_BRAIN_GATEWAY"):
        monkeypatch.delenv(k, raising=False)
    assert cg._resolve_gateway(None) == ""

    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://brain:8788")
    assert cg._resolve_gateway(None) == "http://brain:8788"

    monkeypatch.setenv("SWARPH_GATEWAY", "http://mesh:8788")
    assert cg._resolve_gateway(None) == "http://mesh:8788", "SWARPH_GATEWAY outranks the brain fallback"

    monkeypatch.setenv("SWARPH_CODEGRAPH_GATEWAY", "http://cg:8788")
    assert cg._resolve_gateway(None) == "http://cg:8788", "the specific var outranks both"

    assert cg._resolve_gateway("http://explicit:8788") == "http://explicit:8788", "--gateway wins over all env"


def test_503_is_index_absent_not_an_empty_result(monkeypatch):
    """>>> THE AVAILABILITY AXIS IS NOT THE VISIBILITY AXIS. <<<

    The gateway refuses to return `200 []` for a missing index precisely because
    a consumer cannot tell that apart from a real negative and stops looking.
    The client must carry the same distinction rather than flattening it back.
    """
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json",
                        lambda url, body, token, **kw: (503, {"detail": "codegraph index not available"}))
    rows, present = cg._query_via_gateway("http://gw:8788", "edge", 8, None)
    assert rows == []
    assert present is False, "a 503 must report index-absent, NOT an empty result set"


def test_gateway_error_raises_rather_than_returning_empty(monkeypatch):
    """A 4xx/5xx that is not 503 must not masquerade as 'no matches'."""
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json",
                        lambda url, body, token, **kw: (403, {"detail": "nope"}))
    with pytest.raises(RuntimeError):
        cg._query_via_gateway("http://gw:8788", "edge", 8, None)


def test_limit_is_clamped_to_the_endpoint_contract(monkeypatch):
    """The endpoint pins limit to 1..25; sending 99 would 422 at the gateway and
    read to the user as a broken relay rather than a bad argument."""
    seen = {}

    def _fake_post(url, body, token, **kw):
        seen.update(body)
        return 200, {"results": [], "index_present": True}

    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json", _fake_post)
    cg._query_via_gateway("http://gw:8788", "edge", 99, None)
    assert seen["limit"] == 25
    cg._query_via_gateway("http://gw:8788", "edge", 0, None)
    assert seen["limit"] == 1


def test_no_caller_field_is_sent(monkeypatch):
    """>>> THE RELAYED PATH IS STRICTLY LESS SPOOFABLE THAN THE LOCAL ONE. <<<

    The endpoint takes the caller from the bearer token and has no caller field
    by design ("closes it by construction, by having no field to spoof").
    Sending --caller-cell over the wire would hand back the spoof this verb
    still allows locally.
    """
    seen = {}

    def _fake_post(url, body, token, **kw):
        seen.update(body)
        return 200, {"results": [], "index_present": True}

    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json", _fake_post)
    cg._query_via_gateway("http://gw:8788", "edge", 8, None)
    assert set(seen) == {"query", "limit"}, f"unexpected fields sent to the endpoint: {seen}"


def test_local_flag_bypasses_a_configured_gateway(monkeypatch, tmp_path, capsys):
    """An offline or deliberately-local cell keeps working."""
    monkeypatch.setenv("SWARPH_GATEWAY", "http://gw:8788")

    def _boom(*a, **k):
        raise AssertionError("--local must not touch the gateway")

    monkeypatch.setattr(cg, "_query_via_gateway", _boom)
    rc = cg.run_codegraph(["edge", "--local", "--index", str(tmp_path / "absent.db"), "--json"])
    assert rc == 0
    cap = capsys.readouterr()
    # --json STAYS A BARE LIST: .claude/hooks/codegraph-on-grep.sh does
    # json.loads(...) and iterates. Wrapping it would break the hook that fires
    # on every grep in the fleet.
    assert json.loads(cap.out) == []
    assert "NO INDEX AVAILABLE" in cap.err, "absence must be announced, not flattened into []"


def test_relay_failure_is_loud(monkeypatch, tmp_path, capsys):
    """>>> A SILENT DEGRADE TO AN EMPTY LOCAL INDEX IS THE BUG, NOT THE FALLBACK. <<<

    Falling back is fine; falling back *quietly* reproduces exactly the confusion
    this change removes -- the user sees 'no matches' and stops looking.
    """
    monkeypatch.setenv("SWARPH_GATEWAY", "http://gw:8788")

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(cg, "_query_via_gateway", _boom)
    rc = cg.run_codegraph(["edge", "--index", str(tmp_path / "absent.db")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "unreachable" in err and "falling back" in err, "the degrade must be announced on stderr"
