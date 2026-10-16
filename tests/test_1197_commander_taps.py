"""card #1197 — commander taps on the board publisher. A stub, never a live DM.

Measured 2026-10-16: five shadow-due rows asked "...: extend, flip, or kill?"
and rendered a fixed Yes/No, so "yes" closed them PASS with no choice. The
answer went to to_node=commander (the holder), so the commander DMed itself.
Commander-held rows with an untagged accept never reached the app.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "swarph" / "skills" / "lej-session-board" / "scripts" / "publish_board.py"

TRIAL = ("trial #897 extended past its first due (first_due 2026-10-10, "
         "now 2026-10-22, 15 days total): extend, flip, or kill?")


def _load():
    spec = importlib.util.spec_from_file_location("publish_board", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(oid, accept, *, holder="commander", created_by="lab-ovh", step=None, state="open"):
    return {
        "id": oid,
        "holder": holder,
        "created_by": created_by,
        "card_id": 897,
        "accept": accept,
        "state": state,
        "step": step,
    }


def _options(pub, accept, **kw):
    rows = pub.commander_rows([_row(1, accept, **kw)])
    assert len(rows) == 1
    return pub.build_board._questions(rows)[0]["options"]


def test_choice_title_renders_one_option_per_choice():
    pub = _load()
    opts = _options(pub, f"[commander] {TRIAL} | PASS=the commander picks one")
    assert opts == [
        {"label": "Extend", "text": "extend", "rec": False, "outcome": "pass"},
        {"label": "Flip", "text": "flip", "rec": False, "outcome": "pass"},
        {"label": "Kill", "text": "kill", "rec": False, "outcome": "pass"},
    ], "RED: a choice title must not render the fixed Yes/No"
    two = _options(pub, "[commander] Timer: ship or hold? | PASS=x", step="build")
    assert [(o["label"], o["text"], o["outcome"]) for o in two] == [
        ("Ship", "ship", "pass"), ("Hold", "hold", "pass")]
    # Controls: a binary title, and a literal "yes or no?", keep the fixed mapping.
    for accept in ("[commander] Retire the guard? | PASS=x",
                   "[commander] Retire the guard: yes or no? | PASS=x"):
        assert [(o["text"], o["outcome"]) for o in _options(pub, accept)] == [
            ("yes", "pass"), ("no", "fail")]


def test_unparseable_or_title_renders_no_tap():
    pub = _load()
    for accept in ("[commander] Ship it (now or never)? | PASS=x",
                   "[commander] Should we ship or hold the release? | PASS=x",
                   "[commander] Retire or keep the guard | PASS=x"):
        opts = _options(pub, accept)
        assert opts, accept
        assert all(o["outcome"] is None for o in opts), \
            f"RED: an unparseable 'or' title must close nothing: {accept}"
        assert [o["text"] for o in opts] != ["yes", "no"]
        assert opts[0]["label"] == "Answer on the card"


def test_untagged_commander_held_rows_reach_the_board():
    pub = _load()
    rows = pub.commander_rows([
        _row(1181, "Decide the retention window for the shadow log | PASS=the commander names a window"),
        _row(1986, None),
        _row(1989, '{"pass": [{"id": "p1", "text": "the commander signs the plan"}], "fail": []}'),
        _row(7, "PASS=a cell's own row, untagged", holder="science-claude"),
        _row(8, "closed untagged", state="closed:pass"),
    ])
    by_id = {r["obligation_id"]: r for r in rows}
    assert sorted(by_id) == [1181, 1986, 1989], \
        "RED: commander-held untagged rows must be kept, others still dropped"
    assert by_id[1181]["title"] == "Decide the retention window for the shadow log"
    assert by_id[1986]["title"] == "card #897 obligation #1986"
    assert by_id[1989]["title"] == "the commander signs the plan"
    long = pub.commander_rows([_row(2, "x" * 400)])[0]["title"]
    assert len(long) <= 160


# The three open commander-held rows as `swarph board obligations list --as
# lab-ovh --status open --holder commander --json` returned them on
# 2026-10-16 (fields used here only; the #1986 accept is its first 110
# characters as read, which is past the cut).
LIVE = [
    {"id": 1181, "holder": "commander", "created_by": "lab-ovh", "card_id": 1038,
     "step": None, "status": "open",
     "accept": "PASS=the commander taps YES in the app|FAIL=the commander taps NO"},
    {"id": 1986, "holder": "commander", "created_by": "lab-ovh", "card_id": 1192,
     "step": None, "status": "open",
     "accept": "PASS = commander states the rule (sonnet for read-only sweeps, or keep opus) "
               "| FAIL = commander declines to se"},
    {"id": 1989, "holder": "commander", "created_by": "lab-ovh", "card_id": 554,
     "step": None, "status": "open",
     "accept": "PASS = commander states yes or no for A and for B "
               "| FAIL = commander declines both or defers"},
]


def test_the_live_untagged_rows_render_and_answer_lab():
    pub = _load()
    rows = pub.commander_rows([dict(r) for r in LIVE])
    assert [r["obligation_id"] for r in rows] == [1181, 1986, 1989], \
        "RED: the live commander-held rows never reached the board"
    assert [r["title"] for r in rows] == [
        "the commander taps YES in the app",
        "commander states the rule (sonnet for read-only sweeps, or keep opus)",
        "commander states yes or no for A and for B",
    ]
    qs = pub.build_board._questions(rows)
    assert [q["to_node"] for q in qs] == ["lab-ovh"] * 3
    assert [(o["text"], o["outcome"]) for o in qs[0]["options"]] == [("yes", "pass"), ("no", "fail")]
    # Two answers, or a stated rule: no tap may close these.
    for q in qs[1:]:
        assert all(o["outcome"] is None for o in q["options"]), q["title"]


def test_commander_held_answer_goes_to_the_creator_never_commander():
    pub = _load()
    rows = pub.commander_rows([
        _row(1, f"[commander] {TRIAL} | PASS=x", created_by="science-claude"),
        _row(2, "Untagged decision | PASS=x", created_by="drop-on-meta-edge"),
        _row(3, "[commander] Mine? | PASS=x", created_by="commander"),
        _row(4, "[commander] Nobody? | PASS=x", created_by=None),
        _row(5, "[commander] Retire the guard | PASS=x", holder="droplet", created_by="lab-ovh"),
    ])
    cells = {r["obligation_id"]: r["cell"] for r in rows}
    assert cells == {1: "science-claude", 2: "drop-on-meta-edge", 3: "lab-ovh",
                     4: "lab-ovh", 5: "droplet"}, "RED: answer recipient"
    qs = pub.build_board._questions(rows)
    assert all(q["to_node"] != "commander" and q["to"] != "commander" for q in qs)
    assert [r["holder"] for r in rows][:4] == ["commander"] * 4


def test_roster_groups_commander_rows_under_the_creator_cell():
    pub = _load()
    status = "monitor {0}: running pid=1 sinks=tmux\n  supervised by: systemd:swarph-monitor@{0}.service\n"
    sent = []
    _changed, body = pub.run_once(
        rows=[_row(1197, f"[commander] {TRIAL} | PASS=x", created_by="science-claude")],
        messages=[],
        statuses=[status.format("lab-ovh"), status.format("science-claude")],
        tmux_sessions={"lab", "science-claude"},
        previous=None,
        commander="commander",
        sender="board-publisher",
        recipient="commander",
        token_file="/home/ubuntu/.config/swarph/service-board-publisher.token",
        send=lambda b, **_k: sent.append(b),
    )
    data = json.loads(body.split("\n", 1)[1])
    by_name = {s["name"]: s for s in data["sessions"]}
    assert "commander" not in by_name, "RED: no session keyed on the commander"
    sc = by_name["science-claude"]
    assert sc["tone"] == "needs"
    assert [q["obligation"] for q in sc["questions"]] == [1197]
    q = sc["questions"][0]
    assert q["to_node"] == "science-claude"
    assert [o["label"] for o in q["options"]] == ["Extend", "Flip", "Kill"]
    assert by_name["lab-ovh"]["questions"] == []
