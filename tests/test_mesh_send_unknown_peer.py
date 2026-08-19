"""`mesh send` must refuse a recipient the LIVE registry does not contain.

>>> 63 UNDELIVERED MESSAGES ACROSS 14 NON-PEER NAMES, MEASURED ON THE LIVE MESH
2026-08-18. <<< The gateway accepts any string as `to_node`, stores it, returns 200, and
nobody ever reads it. The top two bad names were `drop` (17) and `lab` (16) — ROLE
names, where the mesh names are drop-on-meta-edge and lab-ovh. Five were lab's own, sent
to `ws-lc` over three hours while the commander chased workstation-lc for not replying.

The commander's framing, which is the real finding: he sends from a DROP-LIST PICKER and
cannot get it wrong; every cell gets a free-text field. The actor with the least volume
got the constrained input. All 63 came from the unconstrained side.
"""
from __future__ import annotations

# NO sys.path tweak: pyproject sets `pythonpath = ["src"]` suite-wide, and every other
# mesh test imports directly (tests/test_mesh_command.py:10). A manual prepend makes
# import order depend on filesystem layout and can mask packaging regressions.
from swarph_cli.commands import mesh


PEERS = {"peers": [{"name": "workstation-lc"}, {"name": "drop-on-meta-edge"},
                   {"name": "lab-ovh"}, {"name": "droplet"}]}


def _registry(monkeypatch, status=200, payload=None):
    calls = []

    def _fake(url, token, **kw):
        calls.append(url)
        return status, (PEERS if payload is None else payload)

    monkeypatch.setattr(mesh, "_http_get_json", _fake)
    return calls


def test_a_known_peer_passes(monkeypatch):
    _registry(monkeypatch)
    assert mesh._check_recipient("droplet", "http://gw", "t") is None


def test_an_UNKNOWN_peer_is_refused_and_the_message_says_why(monkeypatch):
    _registry(monkeypatch)

    msg = mesh._check_recipient("ws-lc", "http://gw", "t")

    assert msg is not None, "an unregistered recipient must be refused"
    assert "REFUSED" in msg and "ws-lc" in msg


def test_the_refusal_SUGGESTS_the_near_match(monkeypatch):
    """The whole point of comparing against the live list rather than just rejecting."""
    _registry(monkeypatch)

    msg = mesh._check_recipient("ws-lc", "http://gw", "t")

    assert "workstation-lc" in msg, f"should suggest the near match, got: {msg}"


def test_a_ROLE_name_is_refused_and_suggests_the_mesh_name(monkeypatch):
    """`lab` (16 lost messages) and `drop` (17) are role names, not typos — the single
    biggest slice of the real damage."""
    _registry(monkeypatch)

    msg = mesh._check_recipient("lab", "http://gw", "t")

    assert msg is not None and "lab-ovh" in msg


def test_it_SUGGESTS_but_never_SUBSTITUTES(monkeypatch):
    """A mesh that silently reroutes to whichever name it guessed is a far worse
    failure than one that refuses. The function returns a MESSAGE; it has no path that
    mutates the recipient."""
    _registry(monkeypatch)

    msg = mesh._check_recipient("ws-lc", "http://gw", "t")

    assert isinstance(msg, str)
    assert "Did you mean" in msg


# ── the branch that must NOT block ──────────────────────────────────────────

def test_an_UNREACHABLE_registry_still_SENDS(monkeypatch, capsys):
    """>>> THE CANNOT-EVALUATE BRANCH, AND IT IS DELIBERATELY THE PERMISSIVE ONE. <<<
    A validator that fails closed on its OWN failure turns every gateway hiccup into
    total messaging loss — far worse than the bug it prevents. Most cannot-evaluate
    branches in this codebase refuse; this one proceeds, because the cost asymmetry
    runs the other way. It must still SAY so."""
    _registry(monkeypatch, status=0, payload={"detail": "connection refused"})

    assert mesh._check_recipient("anything-at-all", "http://gw", "t") is None
    assert "WITHOUT a recipient check" in capsys.readouterr().err


def test_an_EMPTY_registry_still_SENDS(monkeypatch, capsys):
    """Same reasoning, different cause: a registry that answers 200 with no names is
    unusable as a whitelist. Refusing every recipient because the list came back empty
    would strand the whole fleet on one bad response."""
    _registry(monkeypatch, payload={"peers": []})

    assert mesh._check_recipient("droplet", "http://gw", "t") is None
    assert "WITHOUT a recipient check" in capsys.readouterr().err


def test_the_check_is_not_vacuous_when_the_registry_IS_readable(monkeypatch):
    """NON-VACUITY. If _check_recipient returned None on every path, all the tests above
    that assert a refusal would fail — but the permissive branches would still pass, so
    this pins that a readable registry genuinely gates."""
    _registry(monkeypatch)

    assert mesh._check_recipient("droplet", "http://gw", "t") is None
    assert mesh._check_recipient("nope-not-a-peer", "http://gw", "t") is not None


# ── the trap that measurement found ─────────────────────────────────────────

def test_drop_must_not_be_answered_with_droplet_ALONE():
    """>>> THE MEASURED TRAP, AND THE REASON THIS SHOWS SEVERAL NAMES. <<<

    Against the real registry, plain difflib scores `drop` at 0.73 against `droplet`
    and 0.38 against `drop-on-meta-edge`. So a single ranked suggestion names a REAL,
    DIFFERENT, LIVE PEER while the intended one falls below any sane cutoff — and
    `drop` had 17 undelivered messages on 2026-08-18, every one meant for
    drop-on-meta-edge.

    Answering `drop` with only `droplet` is worse than answering nothing: it converts
    an undelivered message into a message delivered to the wrong cell, with a
    confident-looking hint as the cause."""
    names = ["workstation-lc", "drop-on-meta-edge", "lab-ovh", "droplet", "gpu-wsl"]

    got = mesh._near_names("drop", names)

    assert "drop-on-meta-edge" in got, f"the intended peer must appear: {got}"
    assert got != ["droplet"], "a lone droplet suggestion would misroute mail"


def test_ws_lc_finds_workstation_lc_despite_scoring_below_difflibs_default():
    """0.53 — under difflib's 0.6 default. The case that cost three hours would not
    have been suggested by an out-of-the-box cutoff."""
    names = ["workstation-lc", "gpu-wsl", "lab-ovh"]

    assert "workstation-lc" in mesh._near_names("ws-lc", names)


def test_a_name_with_no_plausible_match_suggests_NOTHING(monkeypatch):
    """Silence beats a wrong hint. `claude-service` (9 undelivered) has no near peer,
    and inventing one would be the same misroute as the drop/droplet trap."""
    assert mesh._near_names("claude-service",
                            ["workstation-lc", "lab-ovh", "droplet"]) == []


# ── Copilot's finding on #263: a 2xx of the WRONG SHAPE must not block ──────

def test_a_2xx_with_an_UNEXPECTED_SHAPE_still_SENDS(monkeypatch, capsys):
    """>>> THE GUARD MUST NOT FAIL CLOSED ON ITS OWN CONFUSION. <<< (Copilot, #263.)

    The first version guarded the STATUS and the EMPTY case but assumed the body was a
    dict of dicts. A 2xx carrying a bare list, or {detail: ...}, raised inside
    payload.get()/p.get() and PROPAGATED — blocking the send. That is the exact opposite
    of this function's contract and of the two tests written to pin it: a validator that
    fails closed on its own failure IS the outage it was meant to prevent.

    Each shape is asserted separately because they fail at different lines."""
    for shape in ([], {"detail": "nope"}, {"peers": "not-a-list"},
                  {"peers": [None, 42]}, "a string"):
        monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, shape))
        assert mesh._check_recipient("anything", "http://gw", "t") is None, (
            f"a 2xx with shape {shape!r} must fall through to the permissive path, "
            f"not raise and not refuse")
        assert "WITHOUT a recipient check" in capsys.readouterr().err


def test_the_suggestions_are_SORTED_and_one_per_line(monkeypatch):
    """Copilot, #263: one line in _near_names' own order reads as a ranked answer with a
    best guess at the front. These are OPTIONS — 'drop' legitimately means either
    droplet or drop-on-meta-edge, and difflib ranks the WRONG one first.

    ASSERTED ON THE RENDERED MESSAGE, NOT ON _near_names' RETURN. The function
    deliberately returns MATCH ORDER (prefix hits first, then fuzzy) — that ordering is
    how it FINDS candidates. Sorting is a PRESENTATION decision made where the text is
    built, and an earlier draft of this test asserted it one layer too low and failed
    against correct code."""
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"peers": [
        {"name": "droplet"}, {"name": "drop-on-meta-edge"}, {"name": "workstation-lc"}]}))

    msg = mesh._check_recipient("drop", "http://gw", "t")

    assert msg is not None
    lines = [l.strip() for l in msg.splitlines() if l.strip()]
    i = lines.index("Did you mean:")
    shown = lines[i + 1:]
    assert shown == sorted(shown), f"options must be sorted, not in match order: {shown}"
    assert set(shown) == {"droplet", "drop-on-meta-edge"}, shown
    assert len(shown) > 1, "one option would read as a ranked answer — the drop trap"


def test_a_CASE_VARIANT_of_a_real_peer_is_accepted_not_refused(monkeypatch):
    """droplet, reviewing #263: the exact-match check was case-sensitive against raw
    input, so `Droplet` would fall through to the suggestion path and be REFUSED — while
    _near_names lowercases for its own comparison and would have suggested `droplet`,
    the name the caller had effectively already typed.

    Non-blocking per its review, and fixed here only because the formal approval was
    dismissed by an earlier push anyway. An unnecessary refusal is a small failure, but
    it is a failure of this function's only job."""
    _registry(monkeypatch)

    assert mesh._check_recipient("Droplet", "http://gw", "t") is None
    assert mesh._check_recipient("DROPLET", "http://gw", "t") is None
    # non-vacuity: a name that is not a peer in ANY casing is still refused
    assert mesh._check_recipient("Nonesuch", "http://gw", "t") is not None
