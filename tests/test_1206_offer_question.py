"""card #1206 — a commander-held row is titled from its offer message.

Fixtures are the live shapes of row #2103 and msg 74184. The choice
renderer is the #1197 one. A binary question stays Yes/No.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "swarph" / "skills" / "lej-session-board" / "scripts" / "publish_board.py"

# GET /board/obligations row #2103, card #1205, as listed 2026-10-17.
ACCEPT_2103 = (
    "PASS = the commander picks one of the three; lab carries it out without "
    "ever reading or relaying the token, and the commander (or a script the "
    "commander runs) places any token on droplet's box | FAIL = no answer, "
    "or a token passes through lab, a DM or a row"
)
# msg 74184, lab-ovh -> commander, card thread. The question is the text
# before the accept marker.
QUESTION_2103 = (
    "#1205/#2097: droplet wants a grok-service token for its Grok judge "
    "refill. Grok-service has ONE bearer, so: give droplet the shared "
    "GROK_SERVICE_TOKEN once #1205 deploys, build per-client expiring tokens "
    "first, or hold the Grok refill?"
)
MSG_74184 = {
    "id": 74184,
    "from_node": "lab-ovh",
    "to_node": "commander",
    "kind": "question",
    "content": QUESTION_2103 + "\n\nACCEPT CHECK: " + ACCEPT_2103,
}
OPTIONS_2103 = [
    "give droplet the shared GROK_SERVICE_TOKEN once #1205 deploys",
    "build per-client expiring tokens first",
    "hold the Grok refill",
]


def _load():
    spec = importlib.util.spec_from_file_location("publish_board", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(oid, accept, *, card_id=1205, holder="commander", created_by="lab-ovh", step=None):
    return {
        "id": oid,
        "holder": holder,
        "created_by": created_by,
        "card_id": card_id,
        "accept": accept,
        "state": "open",
        "step": step,
    }


def _kept(pub, row, messages):
    rows = pub.commander_rows([row], messages)
    assert len(rows) == 1
    return rows[0], pub.build_board._questions(rows)[0]


def test_2103_renders_its_three_options_not_yes_no():
    pub = _load()
    row, question = _kept(pub, _row(2103, ACCEPT_2103), [MSG_74184])
    assert row["title"] == QUESTION_2103
    assert row["source"] == "offer"
    assert question["title"] == QUESTION_2103
    assert [o["text"] for o in question["options"]] == OPTIONS_2103
    assert [o["text"] for o in question["options"]] != ["yes", "no"]
    assert all(o["outcome"] == "pass" for o in question["options"])


# Row #1999, card #1197, tagged binary. Listed 2026-10-18.
ACCEPT_1999 = (
    "[commander] Merge and restart the gateway with #234 (thread-less self-DMs "
    "get a 400; card notes unaffected) | PASS = commander taps Yes | FAIL = "
    "commander taps No"
)


def test_binary_1999_still_renders_yes_no():
    pub = _load()
    row, question = _kept(pub, _row(1999, ACCEPT_1999, card_id=1197), [])
    assert row["title"] == (
        "Merge and restart the gateway with #234 (thread-less self-DMs get a "
        "400; card notes unaffected)"
    )
    assert not row["title"].endswith("?")
    assert [(o["label"], o["text"], o["outcome"]) for o in question["options"]] == [
        ("Yes", "yes", "pass"),
        ("No", "no", "fail"),
    ]


def test_a_binary_question_still_renders_yes_no():
    pub = _load()
    accept = "PASS = the commander says yes or no | FAIL = no answer"
    message = {
        "id": 1,
        "from_node": "lab-ovh",
        "to_node": "commander",
        "content": "Ship the guard?\n\nACCEPT CHECK: " + accept,
    }
    _row_kept, question = _kept(pub, _row(2, accept), [message])
    assert question["title"] == "Ship the guard?"
    assert [(o["label"], o["text"], o["outcome"]) for o in question["options"]] == [
        ("Yes", "yes", "pass"),
        ("No", "no", "fail"),
    ]


def test_no_matching_message_keeps_the_accept_derived_title():
    pub = _load()
    row, question = _kept(pub, _row(2103, ACCEPT_2103), [])
    assert row["source"] == "untagged_title"
    assert row["title"] == pub.untagged_title(_row(2103, ACCEPT_2103))
    assert question["title"] == row["title"]
    assert not question["title"].endswith("?")


# msg 73942 and row #2076, card #1203, as listed 2026-10-17.
ACCEPT_2076 = (
    "PASS = the commander picks one of the three options and lab carries it "
    "out (for 'move aside': mv to ~/swarph_state/lab-ovh/operator.json.bak-<ts>, "
    "then swarph-me reports 'no operator config') | FAIL = no answer, or lab "
    "acts before one"
)
QUESTION_2076 = (
    "#1203: ~/.config/swarph/operator.json (identity=commander, likely left "
    "over from validate #222) lets any cell on lab-ovh run swarph-me as you. "
    "Move it aside now, keep it until the refusal ships, or keep it for good?"
)
MSG_73942 = {
    "id": 73942,
    "from_node": "lab-ovh",
    "to_node": "commander",
    "kind": "question",
    "content": QUESTION_2076 + "\n\nACCEPT CHECK: " + ACCEPT_2076,
}


def test_2076_live_offer_renders_three_options_not_fragments():
    """The colon is in an earlier sentence. The comma inside the parenthesis
    must not become a tap. The three options are the last sentence."""
    pub = _load()
    row, question = _kept(pub, _row(2076, ACCEPT_2076, card_id=1203), [MSG_73942])
    assert row["source"] == "offer"
    assert row["title"] == QUESTION_2076
    assert [o["text"] for o in question["options"]] == [
        "Move it aside now",
        "keep it until the refusal ships",
        "keep it for good",
    ]
    assert all(o["outcome"] == "pass" for o in question["options"])


def test_tagged_paren_comma_stays_display_only():
    """#1197: a tagged title whose comma sits inside a parenthesis does not
    parse, so it closes nothing. It must not gain pass-closing fragment taps."""
    pub = _load()
    accept = (
        "[commander] Move the file (identity=commander, likely left over from "
        "validate #222) aside now, keep it until the refusal ships, or keep it "
        "for good? | PASS=the commander picks one"
    )
    rows = pub.commander_rows([_row(1, accept)])
    opts = pub.build_board._questions(rows)[0]["options"]
    assert opts[0]["label"] == "Answer on the card"
    assert all(o["outcome"] is None for o in opts)
    assert [o["text"] for o in opts] != ["yes", "no"]


def test_a_decoy_message_whose_accept_differs_does_not_match():
    pub = _load()
    decoy = {
        "id": 999,
        "from_node": "lab-ovh",
        "to_node": "commander",
        "content": (
            "shared token, per-client tokens, or hold?\n\n"
            "ACCEPT CHECK: PASS = a different check | FAIL = no"
        ),
    }
    row, _question = _kept(pub, _row(2103, ACCEPT_2103), [decoy, MSG_74184])
    # The decoy is listed first. It must not win. The real message does.
    assert row["source"] == "offer"
    assert row["title"] == QUESTION_2103

    only_decoy, _q = _kept(pub, _row(2103, ACCEPT_2103), [decoy])
    assert only_decoy["source"] == "untagged_title"
    assert only_decoy["title"] != "shared token, per-client tokens, or hold?"
    assert "different check" not in only_decoy["title"]


# msg 74504 and row #2126. The comma after the final or-part is inside one
# option ("self-minted id with issued, expiring membership..."), not a fourth tap.
ACCEPT_2126 = (
    "PASS = the commander picks one; lab records it on #418 as the Q4 ruling "
    "with its rationale and re-derives rules 3-5 if 'issued' wins | FAIL = no "
    "answer, or lab builds an id scheme before the ruling"
)
QUESTION_2126 = (
    "#418 Q4: fractal ids self-minted (key hash only), issued (an issuer "
    "vouches for the id), or self-minted id with issued, expiring membership "
    "per fractal (lab's lean)?"
)
MSG_74504 = {
    "id": 74504,
    "from_node": "lab-ovh",
    "to_node": "commander",
    "kind": "question",
    "content": QUESTION_2126 + "\n\nACCEPT CHECK: " + ACCEPT_2126,
}


def _assert_display_only(opts):
    assert opts[0]["label"] == "Answer on the card"
    assert all(o["outcome"] is None for o in opts)
    assert [o["text"] for o in opts] != ["yes", "no"]


def test_2126_untagged_offer_is_display_only():
    """The comma after the final or-part must not become a fourth tap."""
    pub = _load()
    row, question = _kept(
        pub, _row(2126, ACCEPT_2126, card_id=418), [MSG_74504]
    )
    assert row["source"] == "offer"
    assert row["title"] == QUESTION_2126
    assert question["title"] == QUESTION_2126
    _assert_display_only(question["options"])


def test_2126_tagged_copy_is_display_only():
    pub = _load()
    accept = "[commander] " + QUESTION_2126 + " | PASS=the commander picks one"
    rows = pub.commander_rows([_row(2126, accept, card_id=418)])
    assert rows[0]["title"] == QUESTION_2126
    assert rows[0]["source"] == "tag"
    _assert_display_only(pub.build_board._questions(rows)[0]["options"])


# Row #2094, card #1011. Short form. No comma after the final or-part.
ACCEPT_2094 = (
    "[commander] trial #733 extended past its first due (first_due 2026-10-13, "
    "now 2026-10-24, 48 days total): extend, flip, or kill? | PASS = the trial "
    "is extended, flipped, or killed | FAIL = it stays extended with no decision"
)


def test_2094_still_renders_extend_flip_kill():
    pub = _load()
    rows = pub.commander_rows([_row(2094, ACCEPT_2094, card_id=1011)])
    opts = pub.build_board._questions(rows)[0]["options"]
    assert [o["text"] for o in opts] == ["extend", "flip", "kill"]
    assert [o["label"] for o in opts] == ["Extend", "Flip", "Kill"]
    assert all(o["outcome"] == "pass" for o in opts)


# Card note 74612, row #2140. Each option is 3-4 words and <= 24 characters.
TITLE_2140 = (
    "Plan-review is the missing step in 23 of 26 blocked moves. "
    "Keep it mandatory, make it optional, or allow a per-card waiver?"
)


def test_2140_medium_phrases_render_three_taps():
    pub = _load()
    accept = "[commander] " + TITLE_2140 + " | PASS=the commander picks one"
    rows = pub.commander_rows([_row(2140, accept)])
    assert rows[0]["title"] == TITLE_2140
    opts = pub.build_board._questions(rows)[0]["options"]
    assert [o["text"] for o in opts] == [
        "Keep it mandatory",
        "make it optional",
        "allow a per-card waiver",
    ]
    assert [o["label"] for o in opts] == [
        "Keep it mandatory",
        "Make it optional",
        "Allow a per-card waiver",
    ]
    assert all(o["outcome"] == "pass" for o in opts)


def test_three_part_stem_stays_display_only():
    """'hold it' and 'kill it' are two words, so this stem is not a choice list."""
    pub = _load()
    accept = "[commander] Should we ship it, hold it, or kill it? | PASS=the commander picks one"
    rows = pub.commander_rows([_row(3, accept)])
    _assert_display_only(pub.build_board._questions(rows)[0]["options"])


# Review #2144. The long branch turns these stems into taps at 0a7bc65.
STEMS_2144 = (
    "Should we ship the release, hold the release, or kill the release?",
    "Should we keep it mandatory, make it optional, or allow a waiver?",
)


def test_2144_stem_titles_stay_display_only():
    pub = _load()
    for title in STEMS_2144:
        accept = "[commander] " + title + " | PASS=the commander picks one"
        rows = pub.commander_rows([_row(2144, accept)])
        assert rows[0]["title"] == title
        opts = pub.build_board._questions(rows)[0]["options"]
        assert opts[0]["label"] == "Answer on the card", title
        assert all(o["outcome"] is None for o in opts), title
        assert [o["text"] for o in opts] != ["yes", "no"]
