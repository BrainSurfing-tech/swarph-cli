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
