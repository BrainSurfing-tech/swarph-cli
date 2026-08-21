"""The relay must carry FRESHNESS, not just rows.

MEASURED (drop-on-meta-edge, review of PR #280). The first cut of the relay did:

    return (payload or {}).get("results", []), True

dropping the envelope's `freshness` on the floor. The consequence is a split:

    codegraph_hook.py:193   fresh = env.get("freshness") or []   -> warns on stale
    codegraph.py (relay)    dropped                              -> silent

>>> THE AUTOMATIC HOOK WARNED ABOUT A STALE INDEX WHILE THE HAND-TYPED VERB DID NOT
-- and the hand-typed verb is the consumer this relay was BUILT for, because typing
it by hand is how droplet hit the original problem. <<<

Card #193 exists because this index has run BACKWARD before (a nightly sweep rebuilt
from per-repo dbs it never refreshed). So a stale index answering confidently is an
OBSERVED failure mode, not a hypothetical -- and it is the failure that looks most
like a good answer.

`codegraph.py:170` already states the rule this violated: "EVERY OBSERVABLE -- rows,
counts, scores, freshness -- must be a function of only the caller-visible subset".
The relay dropped one of the four the line names, in the same file that names it.
"""

import pytest

from swarph_cli.commands import codegraph as cg

# Captured from a live POST /codegraph, not hand-written (the lesson from #555).
LIVE_FRESHNESS = [{
    "repo": "swarph-cli",
    "indexed_at": "2026-08-21T04:30:41.216720+00:00",
    "index_age_hours": 6.31490069,
    "stale": False,
    "basis": "age_heuristic_pending_indexed_commit_193",
}]


def _stub(monkeypatch, payload, status=200):
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json",
                        lambda url, body, token, **kw: (status, payload))


def test_freshness_is_carried_not_dropped(monkeypatch):
    _stub(monkeypatch, {"results": [], "index_present": True, "freshness": LIVE_FRESHNESS})
    _, _, freshness = cg._query_via_gateway("http://gw:8788", "edge", 8, None)
    assert freshness == LIVE_FRESHNESS


def test_a_stale_index_warns_on_stderr(monkeypatch, capsys, tmp_path):
    """>>> A STALE INDEX ANSWERING CONFIDENTLY LOOKS EXACTLY LIKE A GOOD ANSWER. <<<

    stderr rather than stdout on purpose: the rows are REAL and a machine consumer
    should still receive them unmodified. It is the human who needs to know the line
    numbers may have moved.
    """
    stale = [dict(LIVE_FRESHNESS[0], stale=True, index_age_hours=97.4)]
    _stub(monkeypatch, {"results": [], "index_present": True, "freshness": stale})
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw:8788")
    rc = cg.run_codegraph(["edge", "--json"])
    cap = capsys.readouterr()
    assert rc == 0, "stale is not unavailable -- the index answered"
    assert "STALE INDEX" in cap.err
    assert "97.4h" in cap.err
    assert "swarph-cli" in cap.err
    import json
    assert json.loads(cap.out) == [], "the stdout contract is untouched by a warning"


def test_a_fresh_index_says_nothing(monkeypatch, capsys):
    """A warning that fires on every run is one nobody reads."""
    _stub(monkeypatch, {"results": [], "index_present": True, "freshness": LIVE_FRESHNESS})
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw:8788")
    cg.run_codegraph(["edge", "--json"])
    assert "STALE" not in capsys.readouterr().err


def test_the_body_index_present_field_is_not_consulted(monkeypatch):
    """>>> THE ENDPOINT HARDCODES index_present=True ON ITS ONLY 200 PATH. <<<

    So the field is decorative on the wire and the 503 is the real signal. This test
    pins that: a 200 body CLAIMING index_present False must still report present,
    because believing the body would mean branching on a constant -- and a check that
    can only ever return one answer has stopped being a check.
    """
    _stub(monkeypatch, {"results": [], "index_present": False, "freshness": []})
    _, present, _ = cg._query_via_gateway("http://gw:8788", "edge", 8, None)
    assert present is True
