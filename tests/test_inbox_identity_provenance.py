"""#464 F1 — `swarph mesh inbox` must say WHOSE mailbox it opened, and say when it ate it.

THE INCIDENT (fresh-eyes onboarding audit, 2026-08-18): a brand-new cell ran
`swarph mesh inbox` as its first command and was shown 20 unread DMs belonging to
FOUR OTHER PEERS. `--as` was omitted, so the identity fell back to $SWARPH_SELF,
which is set machine-wide on this co-resident box to the box owner. Reading
consumes by default, so an unluckier first command would have marked another
peer's queue read — silently, exit 0.

>>> NOT AN AUTHZ HOLE, WHICH IS WHY IT IS DANGEROUS. <<< The token genuinely was
that peer's; the gateway enforced caller-binding correctly throughout. Nothing was
violated, so nothing alerted. Board #360's family: identity failing TOWARD lab-ovh.

>>> AND NOT FIXED BY INVERTING THE DEFAULT TO PEEK. <<< PullSink — the DEFAULT
monitor sink — advances its ledger on exactly this mark-read ACK, and computes
"you have DMs" as cursor-minus-ledger. A peek default would leave `monitor status`
reporting DMs pending forever. The defect was never that reading consumes; it was
that it consumed SILENTLY, as an identity the caller never saw.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import mesh


# ── the resolver reports its source, not just its answer ──────────────────────

def test_explicit_as_is_reported_as_the_source(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    assert mesh._resolve_self_with_source("cellA") == ("cellA", "--as")


def test_env_fallback_is_reported_as_the_source(monkeypatch):
    """The silent leg. It must still resolve — but it must NAME itself."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    assert mesh._resolve_self_with_source(None) == ("lab-ovh", "$SWARPH_SELF")


def test_no_identity_at_all_still_raises(monkeypatch):
    """NON-VACUITY: the helper must not have quietly invented a default."""
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    with pytest.raises(RuntimeError):
        mesh._resolve_self_with_source(None)


# ── the command announces identity on every path ──────────────────────────────

class _Args:
    def __init__(self, **kw):
        self.self_name = kw.get("self_name")
        self.token_file = None
        self.gateway = "http://gw.invalid:8788"
        self.limit = 20
        self.unread = False
        self.json = False
        self.peek = kw.get("peek", False)
        self.consume = kw.get("consume", False)


@pytest.fixture
def wired(monkeypatch):
    marked = []
    monkeypatch.setattr(mesh, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mesh, "_mark_read",
                        lambda gw, tok, msgs: marked.extend(m["id"] for m in msgs))
    return marked


def _serve(monkeypatch, messages):
    monkeypatch.setattr(mesh, "_http_get_json",
                        lambda url, token, *a, **k: (200, {"messages": messages}))


def test_identity_and_source_print_before_the_mail(monkeypatch, wired, capsys):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 1, "from_node": "x", "kind": "fyi", "content": "hi"}])
    mesh._run_inbox(_Args(peek=True))
    out = capsys.readouterr().out
    assert "inbox lab-ovh (identity from $SWARPH_SELF)" in out
    # Ordering matters: attribution must precede the content it attributes.
    assert out.index("identity from") < out.index("id=1")


def test_identity_prints_on_the_EMPTY_path_too(monkeypatch, wired, capsys):
    """The empty case is exactly where a wrong identity looks like good news:
    'empty' reads as 'nothing waiting for me' when it means 'nothing waiting for
    somebody else'."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [])
    mesh._run_inbox(_Args(self_name="cellA", peek=True))
    out = capsys.readouterr().out
    assert "inbox cellA (identity from --as)" in out
    assert "empty" in out


# ── consumption announces itself ──────────────────────────────────────────────

def test_consuming_says_how_many_and_as_whom(monkeypatch, wired, capsys):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 1, "from_node": "x", "kind": "fyi", "content": "a"},
                         {"id": 2, "from_node": "y", "kind": "fyi", "content": "b"}])
    # --as named: the refusal below does not apply, so the announce path runs.
    mesh._run_inbox(_Args(self_name="lab-ovh", peek=False))
    out = capsys.readouterr().out
    assert "marked 2 read as lab-ovh" in out
    assert wired == [1, 2], "the mark-read itself must still happen — PullSink needs it"


def test_peek_consumes_nothing_and_says_nothing_about_marking(monkeypatch, wired, capsys):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 9, "from_node": "x", "kind": "fyi", "content": "a"}])
    mesh._run_inbox(_Args(peek=True))
    assert wired == []
    assert "marked" not in capsys.readouterr().out


def test_the_default_STILL_consumes_under_a_NAMED_identity(monkeypatch, wired):
    """>>> GUARDS THE FIX ITSELF. <<< The tempting repair was to invert the default to
    peek. That would break PullSink, whose ledger advances only on this ACK — drop
    confirmed it by measurement on 2026-08-17, watching its own pull ledger sit at
    23866 while the cursor moved to 24135, then advance the moment it acked.

    Rewritten after the review escalation: the audited invocation (ambient identity)
    is now REFUSED, so the surviving question is narrower and still the load-bearing
    one — with an identity NAMED, does reading still consume? If someone later flips
    that, this fails and the monitor's pending-DM accounting does not silently die."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 5, "from_node": "x", "kind": "fyi", "content": "a"}])
    mesh._run_inbox(_Args(self_name="lab-ovh"))
    assert wired == [5]


# ── PR #247 review (drop): printing is not a control for an AGENT caller ──────

def test_ambient_identity_REFUSES_a_destructive_read(monkeypatch, wired, capsys):
    """>>> THE ESCALATION FROM WARN TO REFUSE. <<< The first fix printed the resolved
    identity. drop established that is insufficient: the users are AGENTS, and a line
    of output is not a control for a caller that pipes to head/tail — demonstrated
    first-party the same night, when lab piped `swarph ratify` to tail and misread a
    SUCCESS as a failure.

    And the harm is asymmetric: reading consumes, the loss is irreversible, and the
    peers whose unread state is destroyed CANNOT OBSERVE IT. A warning informs the one
    actor who is not harmed.
    """
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 1, "from_node": "x", "kind": "fyi", "content": "a"}])
    rc = mesh._run_inbox(_Args())            # ambient identity, destructive default
    assert rc == 1
    assert wired == [], "a refused read must consume nothing"
    err = capsys.readouterr().err
    assert "REFUSING" in err
    for escape in ("--as lab-ovh", "--peek", "--consume"):
        assert escape in err, "a refusal must name every way past it"


def test_named_identity_still_consumes(monkeypatch, wired):
    """The refusal is scoped to AMBIENT identity. Naming --as is the statement of
    intent, and must behave exactly as before — PullSink's ledger depends on it."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 2, "from_node": "x", "kind": "fyi", "content": "a"}])
    assert mesh._run_inbox(_Args(self_name="cellA")) == 0
    assert wired == [2]


def test_consume_flag_lifts_the_refusal(monkeypatch, wired):
    """An explicit opt-in proceeds. A refusal with no escape hatch is an outage."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 3, "from_node": "x", "kind": "fyi", "content": "a"}])
    args = _Args(); args.consume = True
    assert mesh._run_inbox(args) == 0
    assert wired == [3]


def test_peek_under_ambient_identity_is_NOT_refused(monkeypatch, wired):
    """NON-VACUITY: the refusal must key on DESTRUCTIVE-ness, not on ambience alone.
    If it refused every ambient read, it would break looking — which is the safe act
    and the one a confused cell most needs."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _serve(monkeypatch, [{"id": 4, "from_node": "x", "kind": "fyi", "content": "a"}])
    assert mesh._run_inbox(_Args(peek=True)) == 0
    assert wired == []
