"""#532 — CLI surface for the accept check: `--accept` on `board cards ask`,
and the mint-time warnings in `_format_ask`.

The warnings exist because of #145's lesson, one layer over: afterwards, a row
with no falsifier looks identical to a sound one, so the absence is named AT
THE MOMENT OF MINTING. These tests pin the three renderings and that the flag
actually reaches the POST body — a flag argparse accepts but the dispatch
drops would pass a parser test and ship nothing.
"""
from swarph_cli.commands import board


# ── _format_ask: the three falsifier renderings ─────────────────────────────

def _d(**kw):
    base = {"id": 7, "card_id": 42, "holder": "some-cell", "status": "open",
            "timeout_at": "2026-08-21T00:00:00Z", "thread_uuid": "t-1"}
    base.update(kw)
    return base


def test_format_ask_without_accept_names_the_red():
    out = board._format_ask(_d(accept=None))
    assert "NO ACCEPT CHECK" in out
    assert "reads RED in the sweep" in out
    assert "--accept" in out  # names the remedy, like the NO TIMEOUT line does


def test_format_ask_with_fail_less_accept_warns_distinctly():
    out = board._format_ask(_d(accept="verify it works"))
    assert "NO FAIL BRANCH" in out
    assert "NO ACCEPT CHECK" not in out  # not conflated with missing


def test_format_ask_with_sound_accept_shows_it():
    out = board._format_ask(_d(accept="PASS = x in the log | FAIL = absent"))
    assert "accept: PASS = x in the log | FAIL = absent" in out
    assert "NO ACCEPT CHECK" not in out and "NO FAIL BRANCH" not in out


def test_format_ask_still_names_missing_timeout():
    """Regression pin: the #145 warning must survive the #532 addition."""
    out = board._format_ask(_d(timeout_at=None, accept="PASS = a | FAIL = b"))
    assert "NO TIMEOUT" in out


# ── the marker regex: drop-on-meta-edge's seven phrases, pinned in BOTH repos ──

def test_fail_marker_regex_two_sided_truth():
    """Three copies of this detector exist (here, gateway server.py, gateway
    obligation_sweep.py) and no single suite reaches all of them — so each repo
    pins the same seven phrases. TWO-SIDED on purpose: over-reads are asserted
    as detections (the state names what was checked, never that a falsifier
    exists), under-reads stay on the warning surface (the safe direction)."""
    R = board._FAIL_MARKER_RE
    # detected
    assert R.search("PASS = x | FAIL = absent")
    assert R.search("the deploy must not fail")      # over-read: a wish
    assert R.search("check it doesn't fail")          # over-read: vacuous
    assert R.search("PASS = zero failures; otherwise reject")  # plural - drop's one character
    assert R.search("test failures are expected")
    # not detected
    assert not R.search("failover is out of scope for this task")
    assert not R.search("Fail-safe defaults are assumed")
    assert not R.search("PASS: exit 0 ... Otherwise reject.")   # under-read
    assert not R.search("PASS: count>0; NEGATIVE: count==0")    # under-read


# ── the flag reaches the POST body ──────────────────────────────────────────

def test_ask_posts_accept_when_given(monkeypatch):
    sent = {}
    monkeypatch.setattr(board, "_resolve_self_name", lambda s: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda n, f: "tok")

    def fake_post(url, body, token):
        sent.update(body)
        return 200, {"id": 1, "card_id": 42, "holder": body["holder"],
                     "status": "open", "timeout_at": None, "thread_uuid": "t",
                     "accept": body.get("accept")}

    monkeypatch.setattr(board, "_post_json", fake_post)
    rc = board.run_board(["cards", "ask", "42", "some-cell", "do the thing",
                          "--accept", "PASS = a | FAIL = b"])
    assert rc == 0
    assert sent["accept"] == "PASS = a | FAIL = b"


def test_ask_omits_accept_when_not_given(monkeypatch):
    sent = {}
    monkeypatch.setattr(board, "_resolve_self_name", lambda s: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda n, f: "tok")

    def fake_post(url, body, token):
        sent.update(body)
        return 200, {"id": 1, "card_id": 42, "holder": body["holder"],
                     "status": "open", "timeout_at": None, "thread_uuid": "t",
                     "accept": None}

    monkeypatch.setattr(board, "_post_json", fake_post)
    rc = board.run_board(["cards", "ask", "42", "some-cell", "do the thing"])
    assert rc == 0
    assert "accept" not in sent, "absent means absent — a null on the wire would overwrite the gateway's own normalization"
