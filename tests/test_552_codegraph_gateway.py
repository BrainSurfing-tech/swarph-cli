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


def test_gateway_chain_ends_at_the_canonical_mesh_variable(monkeypatch):
    """>>> THE CHAIN MUST END AT MESH_GATEWAY_URL, NOT SWARPH_BRAIN_GATEWAY. <<<

    The first draft copied `highlight`'s chain. drop-on-meta-edge measured what that
    resolves to on a real box: SWARPH_CODEGRAPH_GATEWAY and SWARPH_GATEWAY unset,
    SWARPH_BRAIN_GATEWAY mis-set to the MESH gateway's address, and MESH_GATEWAY_URL
    correct and never consulted. The relay worked only by that mis-set variable --
    and #546 canonicalised MESH_GATEWAY_URL across four modules the same morning.
    """
    for k in ("SWARPH_CODEGRAPH_GATEWAY", "SWARPH_GATEWAY", "MESH_GATEWAY_URL",
              "SWARPH_BRAIN_GATEWAY"):
        monkeypatch.delenv(k, raising=False)
    assert cg._resolve_gateway(None) == ""

    monkeypatch.setenv("MESH_GATEWAY_URL", "http://mesh:8788")
    assert cg._resolve_gateway(None) == "http://mesh:8788", "the canonical variable must be read"

    monkeypatch.setenv("SWARPH_GATEWAY", "http://generic:8788")
    assert cg._resolve_gateway(None) == "http://generic:8788", "the swarph-specific var outranks it"

    monkeypatch.setenv("SWARPH_CODEGRAPH_GATEWAY", "http://cg:8788")
    assert cg._resolve_gateway(None) == "http://cg:8788", "the specific var outranks both"

    assert cg._resolve_gateway("http://explicit:8788") == "http://explicit:8788", "--gateway wins over all env"


def test_brain_gateway_is_never_consulted(monkeypatch):
    """>>> THE FAILURE IS TRIGGERED BY SOMEONE DOING THE RIGHT THING. <<<

    SWARPH_BRAIN_GATEWAY names the brain -- :8792, mint :8793, a different service on
    different ports. The day anyone points it where its NAME says, a chain containing
    it POSTs to :8792/codegraph: a mesh peer token handed to a service it was not
    minted for, a non-2xx, the fallback, and droplet's original symptom returns ON THE
    DAY SOMEONE FIXED A VARIABLE.

    A fallback to a differently-named service is not a safety net; it re-arms the
    collision on the day it fires.
    """
    for k in ("SWARPH_CODEGRAPH_GATEWAY", "SWARPH_GATEWAY", "MESH_GATEWAY_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://100.107.222.72:8792")
    assert cg._resolve_gateway(None) == "", (
        "the brain gateway leaked into the codegraph relay chain"
    )


def test_503_is_index_absent_not_an_empty_result(monkeypatch):
    """>>> THE AVAILABILITY AXIS IS NOT THE VISIBILITY AXIS. <<<

    The gateway refuses to return `200 []` for a missing index precisely because
    a consumer cannot tell that apart from a real negative and stops looking.
    The client must carry the same distinction rather than flattening it back.
    """
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("swarph_cli.commands.mesh._post_json",
                        lambda url, body, token, **kw: (503, {"detail": "codegraph index not available"}))
    rows, present, freshness = cg._query_via_gateway("http://gw:8788", "edge", 8, None)
    assert rows == []
    assert present is False, "a 503 must report index-absent, NOT an empty result set"
    assert freshness == [], "there is no index, so there is nothing to be fresh or stale"


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
    assert rc == cg.RC_NO_INDEX
    cap = capsys.readouterr()
    # --json STAYS A BARE LIST: .claude/hooks/codegraph-on-grep.sh does
    # json.loads(...) and iterates. Wrapping it would break the hook that fires
    # on every grep in the fleet.
    assert json.loads(cap.out) == []
    assert "NO INDEX AVAILABLE" in cap.err, "absence must be announced, not flattened into []"


def test_no_index_does_not_print_a_claim_about_the_code(monkeypatch, tmp_path, capsys):
    """>>> A WARNING ABOVE A CONTRADICTING RESULT IS NOT A FIX. <<<

    The commander ran `swarph codegraph mamma` -- a word he KNEW was absent, which
    establishes what a true negative looks like -- and got the identical line a cell
    with no index at all gets. Adding a stderr warning left "No structural matches
    for: 'mamma'" printed underneath it: a claim about the CODE, made when nothing
    was searched, and the only line a stdout-only reader sees.

    The human path owns both streams, so it simply does not make the claim.
    """
    rc = cg.run_codegraph(["mamma", "--local", "--index", str(tmp_path / "absent.db")])
    assert rc == cg.RC_NO_INDEX
    cap = capsys.readouterr()
    assert "No structural matches" not in cap.out, (
        "a negative claim about the code was printed for a search that never ran"
    )
    assert cap.out.strip() == "", f"stdout must stay silent when nothing was searched: {cap.out!r}"
    assert "NO INDEX AVAILABLE" in cap.err


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
    assert rc == cg.RC_NO_INDEX
    err = capsys.readouterr().err
    assert "unreachable" in err and "falling back" in err, "the degrade must be announced on stderr"


def test_rc_carries_availability_without_touching_stdout(tmp_path, capsys, monkeypatch):
    """>>> rc IS THE CHANNEL THE FROZEN STDOUT CONTRACT LEFT FREE (drop, PR #279). <<<

    Two things must hold at once, and they are what makes rc the right answer rather
    than an envelope:

      1. --json stays a BARE LIST, byte-identical to what codegraph-on-grep.sh already
         parses on every grep in the fleet. A consumer ignoring rc sees no change.
      2. A consumer that READS rc gets the availability axis -- the same distinction
         the gateway already expresses as a 503, and the one thing an empty list can
         never carry.

    It also closes the ambiguity the previous commit MOVED: with stderr redirected
    (which is exactly what the hook does), no-stdout + rc 0 read as "ran fine, nothing
    found". Silence and success are not the same claim.
    """
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    absent = str(tmp_path / "absent.db")

    rc_absent = cg.run_codegraph(["edge", "--local", "--index", absent, "--json"])
    out_absent = capsys.readouterr().out
    assert json.loads(out_absent) == [], "the stdout contract moved -- the grep hook parses this"
    assert rc_absent == cg.RC_NO_INDEX

    # And a REAL negative against a PRESENT index must still be rc 0: the two cases
    # differ in rc precisely because they differ in what was searched.
    real = tmp_path / "present.db"
    real.write_bytes(b"")
    monkeypatch.setattr(cg, "structural_query", lambda *a, **k: [])
    monkeypatch.setattr(cg.os.path, "exists", lambda p: True)
    rc_present = cg.run_codegraph(["edge", "--local", "--index", str(real), "--json"])
    assert json.loads(capsys.readouterr().out) == []
    assert rc_present == 0, "a true negative is not an availability failure"
