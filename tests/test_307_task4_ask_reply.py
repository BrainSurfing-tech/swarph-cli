"""#307 Task 4 — the CLI surface for minting an obligation and closing it.

Tasks 1/2/5 built the row, the close, and the surface INSIDE the gateway. None of
them is reachable by a person. This is the half an operator actually types, and the
card's failure mode was never a missing table — lab said "#91 is waiting on a seat"
three times over four hours because ASKING was not an act that left a record.

TWO PROPERTIES THIS FILE EXISTS TO PIN, both of which a happy-path test would miss:

1. >>> A THREADLESS REPLY AND A THREADED ONE MUST NOT PRINT THE SAME LINE. <<< Most
   DMs carry no thread. Refusing them would make `reply` useless for the common case;
   sending them silently would let an operator believe they had closed an obligation
   when nothing could have. Both are sent, and the two outcomes are DISTINGUISHABLE
   in the output. One message for two causes hides which one happened.

2. >>> AN OBLIGATION WITH NO TIMEOUT MUST SAY SO AT CREATION. <<< #145's lesson one
   layer up: "0 overdue" must never be able to mean "nobody set dates". After the
   fact, an obligation that can never go red looks exactly like one that is not late
   yet — so the only honest moment to say it is when the operator mints it.

`reply` is NOT gated by kind, per the spec's standing constraint, and that has its
own test: a gate would let a peer owe a delivery on a DM whose kind nobody thought
to allow, leaving the obligation unclosable through the product — the precise
"waiting on a seat" shape the card exists to kill.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import board, mesh


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    """Identity and credentials passed IN, never read off the host.

    >>> THE FIRST DRAFT PASSED LOCALLY AND FAILED IN CI, FOR THE REASON I HAD FILED A
    CARD ABOUT THREE HOURS EARLIER. <<< It patched `mesh._resolve_token` but not
    `board._resolve_token`, so the board path ran the REAL resolver and read
    ~/.config/swarph/lab-ovh.peer_token — which exists on this box and on no runner.
    Board #479 is exactly this: tests that read host env/config pass where there is
    nothing to test and fail where the product runs. I wrote a fresh instance of it
    while the card was open.

    >>> AND `raising=False` IS WHAT LET IT HIDE. <<< It was on the board patch, so if
    the attribute had been absent the patch would have silently done nothing and the
    test would still have passed on ambient state. Every patch here is strict now: a
    rename must break the fixture LOUDLY rather than quietly return it to reading the
    host.
    """
    for mod in (mesh, board):
        monkeypatch.setattr(mod, "_resolve_self_name", lambda a: a or "lab-ovh")
        monkeypatch.setattr(mod, "_resolve_token", lambda n, f: "tok")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)


def _posts(monkeypatch, mod, status=200, payload=None):
    """Capture (url, body) and return a canned gateway response."""
    seen = []

    def fake(url, body, token):
        seen.append((url, body))
        return status, (payload if payload is not None else {"id": 7})

    monkeypatch.setattr(mod, "_post_json", fake)
    return seen


# ── ask ─────────────────────────────────────────────────────────────────────

def test_ask_posts_to_the_ask_endpoint(monkeypatch, capsys):
    seen = _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": None, "thread_uuid": "t1"})

    rc = board.run_board(["cards", "ask", "42", "droplet", "please review",
                          "--gateway", "http://gw", "--as", "lab-ovh"])

    assert rc == 0
    url, body = seen[0]
    assert url == "http://gw/board/cards/42/ask"
    assert body["holder"] == "droplet"
    assert body["what"] == "please review"
    assert body["created_by"] == "lab-ovh"


def test_ask_SAYS_when_the_obligation_can_never_go_red(monkeypatch, capsys):
    """>>> #145's LESSON, ONE LAYER UP. <<< An obligation with no timeout never goes
    overdue on its own. Afterwards it is indistinguishable from one that simply is
    not late yet, so creation is the only honest moment to say it."""
    _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": None, "thread_uuid": "t1"})

    board.run_board(["cards", "ask", "42", "droplet", "review",
                     "--gateway", "http://gw", "--as", "lab-ovh"])

    out = capsys.readouterr().out
    assert "NO TIMEOUT" in out and "--timeout-hours" in out


def test_ask_with_a_timeout_names_the_deadline_instead(monkeypatch, capsys):
    seen = _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": "2026-08-19T00:00:00Z", "thread_uuid": "t1"})

    board.run_board(["cards", "ask", "42", "droplet", "review", "--timeout-hours", "24",
                     "--gateway", "http://gw", "--as", "lab-ovh"])

    assert seen[0][1]["timeout_hours"] == 24
    out = capsys.readouterr().out
    assert "2026-08-19T00:00:00Z" in out and "NO TIMEOUT" not in out


# ── reply ───────────────────────────────────────────────────────────────────

def _inbox(monkeypatch, messages):
    """Canned inbox that also RECORDS THE URL IT WAS ASKED FOR.

    >>> THE FIRST VERSION DISCARDED THE URL, SO NO TEST COULD SEE WHAT THE CODE
    ACTUALLY REQUESTED. <<< grok, non-blocking on #253: asserting "37" appears in the
    refusal proves only that the CLI arg was interpolated into a MESSAGE — the code
    could ignore the bound entirely and still print a confident sentence about it.
    Third instance of the same proxy-vs-thing collapse in one session. Return the
    calls so the test can assert on the REQUEST, not on prose about the request.
    """
    calls = []

    def fake(url, token):
        calls.append(url)
        return 200, {"messages": messages}

    monkeypatch.setattr(mesh, "_http_get_json", fake)
    return calls


def test_reply_posts_IN_THE_ORIGINAL_THREAD(monkeypatch, capsys):
    """The whole mechanism: Task 2 closes on a reply carrying the obligation's
    thread_id. Drop the thread_id and the obligation never closes."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    seen = _posts(monkeypatch, mesh, payload={"id": 100})

    rc = mesh.run_mesh(["reply", "99", "--content", "done", "--gateway", "http://gw"])

    assert rc == 0
    url, body = seen[0]
    assert url == "http://gw/messages"
    assert body["thread_id"] == "t-abc"
    assert body["to_node"] == "droplet", "a reply goes to the ORIGINAL SENDER"
    assert body["kind"] == "answer"


def test_threaded_reply_reports_only_what_the_send_returned(monkeypatch, capsys):
    """>>> grok's BLOCKING REVIEW ON PR #253, AND HE WAS RIGHT. <<< POST /messages
    returns id/from/to/kind/thread_id/created_at — NO CLOSE FACT. Closing is Task 2's
    side effect and it fails OPEN for a ghost holder, a non-holder, or no row at all.
    The first version printed "an open obligation ... is now closed" on exit 0: prose
    asserted as fact on a successful exit, THE EXACT SHAPE OF "#91 is waiting on a
    seat", inside the tool built to kill it.

    >>> AND MY FIRST FIX SHIPPED A NAMESAKE TEST WITH A DOCSTRING AND NO ASSERTS. <<<
    grok caught it: pytest collects it and it passes forever, so the pin was a NAME.
    A test that cannot fail is worse than no test — it occupies the slot where the
    real one would have gone and reports green from it. Merged into this one, which
    actually asserts."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100})

    mesh.run_mesh(["reply", "99", "--content", "done", "--gateway", "http://gw"])

    out = capsys.readouterr().out
    assert "is now closed" not in out, (
        "the send returns no close fact — asserting one is the card's own defect"
    )
    assert "CANNOT CONFIRM" in out


def test_a_THREADLESS_reply_is_SENT_but_says_it_closes_nothing(monkeypatch, capsys):
    """>>> THE PROPERTY THAT MATTERS MOST HERE. <<< Sending silently would let an
    operator believe an obligation closed when nothing could have. Refusing would
    make the verb useless for the common case. Send, and SAY WHICH."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": None}])
    seen = _posts(monkeypatch, mesh, payload={"id": 100})

    rc = mesh.run_mesh(["reply", "99", "--content", "ok", "--gateway", "http://gw"])

    assert rc == 0, "a threadless reply must still be SENT"
    assert "thread_id" not in seen[0][1]
    out = capsys.readouterr().out
    assert "NOT IN A THREAD" in out and "closes no obligation" in out


def test_the_two_reply_outcomes_do_not_print_the_same_line(monkeypatch, capsys):
    """Stated as its own test because it is the actual invariant. The two branches
    above could each pass while emitting identical text if someone 'simplified' the
    message later — one message for two causes hides which."""
    _inbox(monkeypatch, [{"id": 1, "from_node": "d", "thread_id": "t"}])
    _posts(monkeypatch, mesh, payload={"id": 10})
    mesh.run_mesh(["reply", "1", "--content", "x", "--gateway", "http://gw"])
    threaded = capsys.readouterr().out

    _inbox(monkeypatch, [{"id": 2, "from_node": "d", "thread_id": None}])
    _posts(monkeypatch, mesh, payload={"id": 11})
    mesh.run_mesh(["reply", "2", "--content", "x", "--gateway", "http://gw"])
    threadless = capsys.readouterr().out

    # >>> COMPARING THE RAW STRINGS IS VACUOUS AND A MUTATION PROVED IT. <<< The two
    # lines carry different message ids (10 vs 11), so they differ ALWAYS — including
    # when the threadless branch was mutated to print the threaded text verbatim,
    # which is exactly the collapse this test claims to catch. It passed. Strip the
    # digits and the confound goes with them, leaving only the WORDING to compare.
    import re
    norm = lambda t: re.sub(r"\d+", "N", t)
    assert norm(threaded) != norm(threadless), (
        "the two outcomes must be distinguishable by their WORDING, not merely by the "
        "ids that happen to differ in every pair of sends"
    )
    assert "NOT IN A THREAD" in threadless and "NOT IN A THREAD" not in threaded


@pytest.mark.parametrize("kind", ["answer", "fyi", "status", "question", "unblock"])
def test_reply_is_NOT_gated_by_kind(monkeypatch, kind):
    """Spec constraint: universal across every DM kind. A gate would let a peer owe a
    delivery on a kind nobody thought to allow, leaving the obligation unclosable
    through the product — the 'waiting on a seat' shape, rebuilt."""
    _inbox(monkeypatch, [{"id": 5, "from_node": "d", "thread_id": "t", "kind": kind}])
    seen = _posts(monkeypatch, mesh, payload={"id": 6})

    rc = mesh.run_mesh(["reply", "5", "--content", "x", "--kind", kind,
                        "--gateway", "http://gw"])

    assert rc == 0 and seen[0][1]["kind"] == kind


def test_a_MISSING_message_refusal_names_the_SEARCH_BOUND(monkeypatch, capsys):
    """>>> THERE IS NO GET /messages/{id}, SO THE SEARCH IS BOUNDED. <<< A message
    older than the window is indistinguishable from one that never existed unless the
    refusal says what was searched — otherwise the operator hunts a delivery bug that
    is really a paging window."""
    calls = _inbox(monkeypatch, [{"id": 1, "from_node": "d", "thread_id": "t"}])
    _posts(monkeypatch, mesh)

    rc = mesh.run_mesh(["reply", "404", "--content", "x", "--gateway", "http://gw",
                        "--search-limit", "37"])

    err = capsys.readouterr().err
    assert rc == 1
    # >>> ASSERT ON THE REQUEST, NOT ON PROSE ABOUT THE REQUEST. <<< The bound must
    # reach the GET; a message naming a limit the code never applied is the confident
    # sentence this whole file exists to distrust.
    assert calls and "limit=37" in calls[0], \
        f"--search-limit must reach the gateway query, got {calls}"
    # grok, optional on the second review: the URL was checked for the BOUND and not
    # for the SCOPE. A reply that searched the wrong peer's inbox would satisfy every
    # other assertion here — including the refusal, which would then be TRUE and
    # MISLEADING. Pinned rather than deferred: "next time we touch this file" is the
    # decay this whole card exists to kill.
    assert "to_node=lab-ovh" in calls[0], \
        f"the scan must be scoped to THIS peer's inbox, got {calls[0]}"
    assert "37" in err, f"the refusal must name the bound it searched: {err}"
    assert "older than that window" in err


def test_a_reply_is_NOT_sent_when_the_original_cannot_be_found(monkeypatch):
    """The refusal must be a real abort. A warning followed by a send would deliver a
    DM to nobody in particular, or worse, to a guessed recipient."""
    _inbox(monkeypatch, [])
    seen = _posts(monkeypatch, mesh)

    rc = mesh.run_mesh(["reply", "404", "--content", "x", "--gateway", "http://gw"])

    assert rc == 1, "a refusal must be a real abort, not a warning that returns 0"
    assert seen == [], "nothing may be posted when the target is unknown"


def test_JSON_output_never_claims_a_closure_it_cannot_see(monkeypatch, capsys):
    """>>> gpu-wsl's FINDING: BOTH OUTCOMES EXIT 0 AND MOST CALLERS HERE ARE
    AUTOMATED. <<< Without a machine-readable result the only signal is stdout prose,
    which no script reads. --json exposes what this command KNOWS —
    attached_to_thread — and reports closed_obligation as null RATHER THAN OMITTING
    IT. Omission would let a caller's `.get("closed_obligation", False)` read a
    missing key as a confident False; an explicit null is unignorable."""
    import json as _json
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])

    d = _json.loads(capsys.readouterr().out)
    assert d["attached_to_thread"] is True
    assert d["closed_obligation"] is None, "this command cannot see a closure"
    assert d["thread_id"] == "t-abc"
    # #525: the gateway in this fixture returns NO obligation_check, which is what an
    # older gateway is. The disclaimer must survive that -- and `null` on both keys is
    # how the caller tells "this server cannot say" from "nothing closed".
    assert d["obligation_check"] is None
    assert d["closed_obligations"] is None


def test_JSON_marks_a_threadless_reply_as_unattached(monkeypatch, capsys):
    import json as _json
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": None}])
    _posts(monkeypatch, mesh, payload={"id": 100})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])

    d = _json.loads(capsys.readouterr().out)
    assert d["attached_to_thread"] is False
    assert d["closed_obligation"] is None


# ── #525: the CLI reports the server's fact when the server states it ────────

def test_JSON_reports_the_close_fact_WHEN_THE_SERVER_STATES_IT(monkeypatch, capsys):
    """>>> THE DISCLAIMER WAS RIGHT UNTIL #525 AND WOULD HAVE STAYED WRONG AFTER IT. <<<
    grok blocked PR #253 to put the refusal in, correctly: the endpoint returned no close
    fact, so claiming one was prose on exit 0. #525 removed the reason for it.

    Gated on the FIELD'S PRESENCE, never on a version or a date. A merge is not a deploy:
    #123 merged while the live gateway had been serving pre-#525 code for 22 hours with
    nothing anywhere joining the two. Keying on a version would make this command assert a
    fact the running server does not return. (drop-on-meta-edge.)
    """
    import json as _json
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100, "closed_obligations": [12],
                                       "obligation_check": "checked"})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])

    d = _json.loads(capsys.readouterr().out)
    assert d["closed_obligations"] == [12]
    assert d["obligation_check"] == "checked"
    assert d["closed_obligation"] == 12, (
        "the legacy singular key must carry the fact too -- a caller doing "
        ".get('closed_obligation', False) would otherwise read a real closure as False")


def test_a_LOOKED_AND_FOUND_NONE_reply_is_not_reported_as_a_closure(monkeypatch, capsys):
    """NON-VACUITY partner. Same field present, empty list: the server looked and closed
    nothing. That must read as a FINDING, and must not become a claim of closure."""
    import json as _json
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100, "closed_obligations": [],
                                       "obligation_check": "checked"})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])

    d = _json.loads(capsys.readouterr().out)
    assert d["closed_obligations"] == []
    assert d["obligation_check"] == "checked"
    assert d["closed_obligation"] is None


def test_an_OLD_GATEWAY_still_gets_the_disclaimer_not_a_claim(monkeypatch, capsys):
    """>>> THE BRANCH THE LIVE MESH IS ON RIGHT NOW. <<< No obligation_check in the
    response means the server did not say, so this command must not say either -- the
    rule grok drew on #253, unchanged. Stdout, not --json: prose is what a human reads."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw"])

    out = capsys.readouterr().out
    assert "CANNOT CONFIRM" in out
    assert "CLOSED obligation" not in out


def test_the_stdout_line_names_the_obligation_when_the_server_did(monkeypatch, capsys):
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100, "closed_obligations": [12],
                                       "obligation_check": "checked"})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw"])

    out = capsys.readouterr().out
    assert "CLOSED obligation #12" in out
    assert "CANNOT CONFIRM" not in out



def test_the_gate_is_the_FIELD_BEING_PRESENT_not_its_value_being_truthy(monkeypatch, capsys):
    """>>> THE COMMENT SAYS PRESENCE; `if check:` WOULD SAY TRUTHINESS. <<< The two agree
    today only because all four values are non-empty strings, so a falsy one -- a server
    that reports the field with an empty value -- would flip this command back to "the
    server did not say" while the server plainly did.

    FOURTH INSTANCE IN ONE DAY of a comment stating the property correctly with a line
    under it implementing something narrower that happened to agree: len(hits)==1 for
    "name matches win", a unit test for "the helper is consulted", obligation_check set
    before the query it describes, and this. The comment is the SPEC, not decoration.
    (drop-on-meta-edge, PR #270.)
    """
    import json as _json
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    _posts(monkeypatch, mesh, payload={"id": 100, "closed_obligations": [],
                                       "obligation_check": ""})

    mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])

    d = _json.loads(capsys.readouterr().out)
    assert d["closed_obligations"] == [], (
        "the server DID report closed_obligations -- nulling it because the companion "
        "field was falsy would hide a fact the server stated")
    assert d["obligation_check"] == ""


def test_closed_obligation_ALONE_cannot_tell_the_three_nulls_apart(monkeypatch, capsys):
    """>>> THE AMBIGUITY #525 EXISTS TO KILL, SURVIVING INSIDE THE PR THAT SHIPS THE FIX --
    AND KEPT ON PURPOSE. <<< The legacy singular key reads null for all three of:

        old gateway (cannot tell) | checked, none closed | never looked

    A caller reading only that key is exactly where it started. The key stays as a
    precaution against silently downgrading a hypothetical `.get(key, False)` caller to a
    confident False -- there is NO in-tree consumer, so this is a precaution rather than a
    measured need, and those justify different amounts of permanence. This test exists so
    the ambiguity is DOCUMENTED rather than discovered. (drop-on-meta-edge, PR #270.)
    """
    import json as _json
    seen = []
    for payload in ({"id": 100},
                    {"id": 100, "closed_obligations": [], "obligation_check": "checked"},
                    {"id": 100, "closed_obligations": [], "obligation_check": "no_thread"}):
        _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
        _posts(monkeypatch, mesh, payload=payload)
        mesh.run_mesh(["reply", "99", "--content", "x", "--gateway", "http://gw", "--json"])
        seen.append(_json.loads(capsys.readouterr().out))

    assert [d["closed_obligation"] for d in seen] == [None, None, None]
    assert [d["obligation_check"] for d in seen] == [None, "checked", "no_thread"], (
        "obligation_check is the ONLY field that separates the three -- if this ever "
        "collapses, the card's defect is back")
