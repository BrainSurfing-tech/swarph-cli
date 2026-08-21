"""#464 F-init — every ENABLED provider must be offerable by the init wizard.

THE DEFECT (fresh-eyes onboarding audit, 2026-08-18): `_LLM_CHOICES` was a
hardcoded list of three (claude, codex, antigravity) while CLI_ENABLED_PROVIDERS
held seven. The wizard prompted "[1-3]", so a grok / cursor / muse / vibe cell
could not complete `swarph init` interactively — it had to already know to pass
--provider, which the wizard never mentioned.

An escape hatch existed the whole time (typing the provider name fell through to
the CLI_ENABLED_PROVIDERS membership check) and NOTHING DISCLOSED IT. An
undiscoverable escape hatch is not a feature; it is the bug plus a secret.

>>> THE PROOF THAT A HAND-MAINTAINED MIRROR DRIFTS: `cursor` was added to
CLI_ENABLED_PROVIDERS on the same day the Cursor membrane shipped, and this menu
never learned. <<< The menu is now DERIVED, so the next provider appears by
construction rather than by somebody remembering.
"""
from __future__ import annotations

from swarph_cli.cell import CLI_ENABLED_PROVIDERS
from swarph_cli.commands.init import _llm_choices, _LLM_BLURBS


def test_every_enabled_provider_is_offered():
    """>>> THE REGRESSION GUARD. <<< If a provider is enabled but unlisted, a cell
    of that type cannot finish the wizard. This is the assertion the old hardcoded
    list could not make about itself."""
    offered = {p for p, _ in _llm_choices()}
    missing = set(CLI_ENABLED_PROVIDERS) - offered
    assert not missing, f"enabled but not offerable in the wizard: {sorted(missing)}"


def test_nothing_is_offered_that_is_not_enabled():
    """The other direction: offering a provider the CLI refuses is a dead end that
    fails only after the user has committed to a choice."""
    offered = {p for p, _ in _llm_choices()}
    assert not offered - set(CLI_ENABLED_PROVIDERS)


def test_cursor_specifically_is_offerable():
    """Named because it is the one that proved the drift: shipped, enabled, and
    invisible in the wizard on the same day."""
    assert "cursor" in {p for p, _ in _llm_choices()}


def test_an_unblurbed_provider_still_lists():
    """A provider with no prose must still be SELECTABLE. An enabled provider the
    user cannot see is the defect; a missing description is cosmetic — so the
    fallback must never drop the entry."""
    fake = "someprovider"
    assert fake not in _LLM_BLURBS
    blurb = _LLM_BLURBS.get(fake, f"{fake} membrane")
    assert blurb and fake in blurb


def test_menu_is_derived_not_a_second_copy():
    """NON-VACUITY: the tests above pass trivially if someone re-hardcodes a list
    that happens to match today. Assert the menu tracks the enabled set by
    CONSTRUCTION — same length, same members, sorted."""
    offered = [p for p, _ in _llm_choices()]
    assert offered == sorted(CLI_ENABLED_PROVIDERS)


# ── #464 refinements from cursor-lin's LIVED onboarding audit ────────────────

def _probe_with_peer_row(monkeypatch, peer_row):
    """Drive the REAL _probe_onboarding with a fabricated registry row.

    Stubs the two network reads and the token file so the classifier under test is
    the only live logic. Written this way after a first attempt reimplemented the
    branch inside the test and asserted on its own copy — a test that passes with
    the fix reverted, which is precisely the defect this PR exists to remove.
    """
    import io, json as _json
    from swarph_cli.commands import onboard

    class _Resp:
        def __init__(self, payload): self._p = _json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(req, timeout=8):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/peers"):
            return _Resp({"peers": [peer_row]})
        return _Resp({"messages": []})

    monkeypatch.setattr(onboard.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(onboard.pathlib.Path, "exists", lambda self: True)
    monkeypatch.setattr(onboard.pathlib.Path, "read_text", lambda self, **k: "tok")
    monkeypatch.setattr(onboard.pathlib.Path, "stat",
                        lambda self: type("S", (), {"st_mode": 0o600})())
    return {label: (mark, detail)
            for mark, label, detail in onboard._probe_onboarding(peer_row["name"], "http://gw:8788")}


def test_outbound_only_cell_does_not_carry_a_permanent_health_gap(monkeypatch):
    """>>> N/A IS NOT A GAP. <<< A cell registered outbound-only:// has no inbound
    endpoint, so `last_health` can NEVER become non-null. Marking it MISSING mints a
    red mark no action can close — and a checklist everyone permanently fails is one
    nobody reads. openwolf's 2.0.3 lesson (false-positive warnings train users to
    ignore the system), caught by cursor-lin, which is outbound-only and would have
    carried this forever.
    """
    rows = _probe_with_peer_row(monkeypatch, {
        "name": "cellA", "url": "outbound-only://cellA", "last_health": None,
        "ratified": True, "capabilities": {"can_claim_tasks": True, "provider": "x"},
        "registered_at": "2026-08-18T00:00:00"})
    mark, detail = rows["health check has succeeded"]
    assert mark == "ok", "outbound-only must not read as a gap"
    assert "N/A" in detail


def test_a_LISTENING_cell_with_no_health_STILL_reports_a_gap(monkeypatch):
    """NON-VACUITY for the test above: the N/A branch must be reachable ONLY via the
    outbound-only sentinel. If it swallowed every null last_health, it would hide the
    real defect — a cell that should be answering health checks and is not."""
    rows = _probe_with_peer_row(monkeypatch, {
        "name": "cellB", "url": "http://cellB:8787", "last_health": None,
        "ratified": True, "capabilities": {"can_claim_tasks": True, "provider": "x"},
        "registered_at": "2026-08-18T00:00:00"})
    mark, detail = rows["health check has succeeded"]
    assert mark == "MISSING"
    assert "last_seen is not a substitute" in detail


def test_token_rung_names_the_recovery_not_just_the_gap():
    """The rung that cannot be self-served must say what unblocks it. Tonight the
    recovery (DELETE then re-register mints a new generation) existed only in a
    human's head, and the CLI's own error surface said 'existing'."""
    import inspect
    from swarph_cli.commands import onboard
    src = inspect.getsource(onboard._probe_onboarding)
    assert "RECOVERY" in src
    assert "DELETE /peers/" in src
    assert "token_status=existing" in src, (
        "must warn that re-registering returns a success-shaped 200 with no token"
    )


def test_identity_rung_uses_a_DISCRIMINATING_route_not_a_readable_one(monkeypatch):
    """>>> THE RUNG THAT COST A FALSE RATIFICATION. <<<

    It used to assert `GET /messages?to_node=<peer>` returned 200. Peers may read ALL
    mesh DMs (commander ruling 2026-08-05), so ANY valid token returns 200 there — the
    check could not fail for the reason it appeared to pass. lab ratified meta-muse on
    exactly that evidence and retracted 40 minutes later: the file named
    meta-muse.peer_token authenticates as meta-muse-2.

    /whoami is discriminating: it ANSWERS DIFFERENTLY FOR A DIFFERENT PRINCIPAL. This
    test is the operational form of that — same call, two bindings, answer must move.
    """
    import json as _json
    from swarph_cli.commands import onboard

    class _Resp:
        def __init__(s, p): s._p = _json.dumps(p).encode()
        def read(s): return s._p
        def __enter__(s): return s
        def __exit__(s, *a): return False

    def _run(bound_to):
        def _urlopen(req, timeout=8):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith("/whoami"):
                return _Resp({"peer": bound_to, "regime": "per_peer_token",
                              "key_generation": 1})
            if url.endswith("/peers"):
                return _Resp({"peers": []})
            return _Resp({})
        monkeypatch.setattr(onboard.urllib.request, "urlopen", _urlopen)
        monkeypatch.setattr(onboard.pathlib.Path, "exists", lambda self: True)
        monkeypatch.setattr(onboard.pathlib.Path, "read_text", lambda self, **k: "tok")
        monkeypatch.setattr(onboard.pathlib.Path, "stat",
                            lambda self: type("S", (), {"st_mode": 0o600})())
        return {lbl: (mk, dt) for mk, lbl, dt in
                onboard._probe_onboarding("cellA", "http://gw:8788")}

    match = _run("cellA")["token identifies as this peer"]
    assert match[0] == "ok"

    mismatch = _run("cellB")["token identifies as this peer"]
    assert mismatch[0] == "MISSING", (
        "a token bound to a DIFFERENT peer must not pass an identity rung"
    )
    assert "MISMATCH" in mismatch[1] and "cellB" in mismatch[1]


# ── journey-walkthrough findings (2026-08-21, scratch fork-gateway run) ──────

def _probe_with_whoami_error(monkeypatch, http_code):
    """Drive _probe_onboarding with /whoami raising HTTPError(http_code).

    Same stub discipline as _probe_with_peer_row: the network and the token file
    are fabricated, the classifier under test is the only live logic.
    """
    import io, json as _json
    import urllib.error
    from swarph_cli.commands import onboard

    class _Resp:
        def __init__(self, payload): self._p = _json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(req, timeout=8):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/whoami"):
            raise urllib.error.HTTPError(url, http_code, "err", None, io.BytesIO(b""))
        if url.endswith("/peers"):
            return _Resp({"peers": [{"name": "cellA", "ratified": True,
                                     "capabilities": {"x": 1},
                                     "registered_at": "2026-08-21",
                                     "last_health": "2026-08-21"}]})
        return _Resp({})

    monkeypatch.setattr(onboard.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(onboard.pathlib.Path, "exists", lambda self: True)
    monkeypatch.setattr(onboard.pathlib.Path, "read_text", lambda self, **k: "tok")
    monkeypatch.setattr(onboard.pathlib.Path, "stat",
                        lambda self: type("S", (), {"st_mode": 0o600})())
    return {lbl: (mk, dt) for mk, lbl, dt in
            onboard._probe_onboarding("cellA", "http://gw:8788")}


def test_whoami_404_is_UNDETERMINED_not_a_dead_token(monkeypatch):
    """>>> THE RUNG THAT PRESCRIBES DEREGISTERING A HEALTHY TOKEN. <<<

    Demonstrated 2026-08-21 on a scratch fork-gateway: the fork does not serve
    /whoami (deployed-gateway route), so the identity rung's HTTPError branch
    fired on EVERY fresh cell with "HTTP 404 ... token is DEAD ... recovery needs
    a deregister" — a destructive remedy prescribed for a token minted seconds
    earlier. 404 means THE ROUTE is absent: the probe cannot see the rung, which
    is the exact state '?' exists for. A checklist that cries DEAD on every fresh
    cell trains operators to deregister working tokens.
    """
    mark, detail = _probe_with_whoami_error(monkeypatch, 404)["token identifies as this peer"]
    assert mark == "?", f"route-absent must be undetermined, got {mark!r}: {detail}"
    assert "DEAD" not in detail and "deregister" not in detail.lower(), (
        "a route the gateway does not serve must not prescribe token recovery"
    )
    assert "/whoami" in detail


def test_whoami_403_is_STILL_a_dead_token(monkeypatch):
    """NON-VACUITY for the split above: a gateway that ANSWERS the identity call
    with a refusal (401/403) is making a claim about the token — that reading
    stays MISSING/DEAD. Only route-absence (404) and gateway-failure (5xx) move
    to '?'."""
    mark, detail = _probe_with_whoami_error(monkeypatch, 403)["token identifies as this peer"]
    assert mark == "MISSING"
    assert "DEAD" in detail


def test_whoami_500_is_undetermined_not_a_dead_token(monkeypatch):
    """A gateway 500 is the GATEWAY failing, not the token being refused — the
    probe cannot see the rung."""
    mark, detail = _probe_with_whoami_error(monkeypatch, 500)["token identifies as this peer"]
    assert mark == "?"
    assert "DEAD" not in detail
