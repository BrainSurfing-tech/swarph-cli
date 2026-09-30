"""card #1006 — CLI session board. The desktop tool names must not appear."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "swarph" / "skills" / "lej-session-board"
FIX = ROOT / "tests" / "fixtures" / "session-board"
SCRIPT = SKILL / "scripts" / "build_board.py"

# Names that exist only on Claude Desktop. Referencing one is a fail.
DESKTOP_TOOLS = (
    "mcp__ccd_session_mgmt__",
    "list_sessions",
    "list_events",
    "archive_session",
    "set_pinned",
    "get_session",
)


def _skill_texts():
    files = [SKILL / "SKILL.md", SKILL / "references" / "coordination-rule.md", SCRIPT]
    return {p.relative_to(SKILL).as_posix(): p.read_text(encoding="utf-8") for p in files}


def desktop_hits(text: str) -> list[str]:
    return [name for name in DESKTOP_TOOLS if name in text]


def test_desktop_tool_name_is_not_referenced():
    for rel, text in _skill_texts().items():
        assert desktop_hits(text) == [], rel


def test_the_scanner_fails_when_a_desktop_tool_name_is_added():
    poisoned = "call mcp__ccd_session_mgmt__list_sessions here"
    assert "mcp__ccd_session_mgmt__" in desktop_hits(poisoned)


def test_license_and_attribution_kept():
    lic = (SKILL / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic
    assert "Jonathan Edwards" in lic
    readme = (SKILL / "README.md").read_text(encoding="utf-8")
    assert "Jonathan Edwards" in readme
    assert "MIT" in readme


def test_coordination_rule_uses_listagents_and_sendmessage_by_name():
    text = (SKILL / "references" / "coordination-rule.md").read_text(encoding="utf-8")
    assert "ListAgents" in text
    assert "SendMessage" in text
    assert "by the name" in text
    assert "approves building, not deploying" in text
    assert desktop_hits(text) == []


def _run_board():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--listing", str(FIX / "listagents-lab.txt"),
         "--rows", str(FIX / "commander-170.json")],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def _validate(board: dict):
    """The fields references/data-schema.md says the page reads."""
    schema = (SKILL / "references" / "data-schema.md").read_text(encoding="utf-8")
    for field in ("updated", "projectFolder", "manager", "sessions", "groups"):
        assert field in schema
        assert field in board
    assert isinstance(board["sessions"], list) and board["sessions"]
    for sess in board["sessions"]:
        for field in ("id", "name", "state", "tone", "where", "questions"):
            assert field in sess
        assert sess["tone"] in ("needs", "done", "")
        assert 2 <= len(sess["where"]) <= 4
        for q in sess["questions"]:
            for field in ("id", "to", "title", "options", "to_node", "in_session"):
                assert field in q
                assert field in schema
            assert "card" in schema and "obligation" in schema
            assert q["to_node"] == q["to"]
            assert isinstance(q["in_session"], bool)
            assert 2 <= len(q["options"]) <= 3
            assert sum(1 for opt in q["options"] if opt.get("rec")) == 1
            for opt in q["options"]:
                assert opt["label"] and opt["text"]
    for group in board["groups"]:
        assert "title" in group and "items" in group


def test_skill_maps_a_name_through_the_sessions_file_and_lists_shell():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    step = text.split("## 1. Take the roster", 1)[1].split("## 2.", 1)[0]
    assert "~/.claude/sessions/<pid>.json" in step
    assert "name" in step and "sessionId" in step and "cwd" in step
    assert "`shell`" in step
    assert "in progress" in step.lower() or "In progress" in step


def test_shell_status_is_in_progress():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    listing = "This session is lab-ovh [1e23f6]\npeer [abc123] · interactive · shell · running a command\n"
    board = build_board.build(listing, [])
    sess = next(s for s in board["sessions"] if s["name"] == "peer")
    assert sess["bucket"] == "in_progress"
    assert sess["tone"] == ""


def test_schema_documents_the_four_new_question_fields():
    schema = (SKILL / "references" / "data-schema.md").read_text(encoding="utf-8")
    for field in ("to_node", "card", "obligation", "in_session"):
        assert f"`{field}`" in schema


def test_skill_builds_schema_valid_json_without_desktop_tools():
    board = _run_board()
    _validate(board)
    assert board["manager"] == "lab-ovh"


def test_lab_roster_sorts_offline_remote_control_into_stale():
    listing = (FIX / "listagents-lab.txt").read_text(encoding="utf-8")
    board = _run_board()
    by_id = {s["id"]: s for s in board["sessions"]}
    # every offline Remote Control row from the real listing is stale
    seen = 0
    for line in listing.splitlines():
        if "Remote Control" in line and "offline" in line:
            sid = line.split("[", 1)[1].split("]", 1)[0]
            assert by_id[sid]["bucket"] == "stale"
            assert by_id[sid]["tone"] == "done"
            seen += 1
    assert seen >= 40


def test_droplet_section_carries_the_commander_row_with_a_recommendation():
    board = _run_board()
    droplet = next(s for s in board["sessions"] if s["name"] == "droplet")
    assert droplet["bucket"] == "needs_followup"
    assert droplet["tone"] == "needs"
    q = droplet["questions"][0]
    assert "687" in q["title"] or "rebalancer" in q["title"]
    rec = [opt for opt in q["options"] if opt["rec"]]
    assert len(rec) == 1
    assert rec[0]["label"] == "Retire"


def test_filling_the_template_does_not_change_the_markup():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    template = (SKILL / "assets" / "board-template.html").read_text(encoding="utf-8")
    board = _run_board()
    # the page does not read the sort bucket
    public = json.loads(json.dumps(board))
    for sess in public["sessions"]:
        sess.pop("bucket", None)
    filled = build_board.fill_template(template, public)
    start = template.find('<script type="application/json" id="board-data">')
    end = template.find("</script>", start)
    assert template[:start] == filled[:start]
    assert template[end:] == filled[filled.find("</script>", start):]
    data = json.loads(filled[filled.find(">", start) + 1:filled.find("</script>", start)])
    _validate(data)


def test_envelope_header_and_new_fields():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    board = _run_board()
    body = build_board.envelope(board)
    header, payload = body.split("\n", 1)
    assert header == "SWARPH-BOARD v1"
    parsed = json.loads(payload)
    _validate(parsed)
    droplet = next(s for s in parsed["sessions"] if s["name"] == "droplet")
    q = droplet["questions"][0]
    assert q["to_node"] == "droplet"
    assert q["card"] == 687
    assert q["obligation"] == 170
    assert q["in_session"] is False
    assert "bucket" not in droplet


def test_unchanged_board_sends_nothing():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    board = _run_board()
    sent = []
    assert build_board.publish_if_changed(board, build_board.envelope(board), sent.append) is False
    assert sent == []
    # a clock-only difference is not a change
    later = json.loads(json.dumps(board))
    later["updated"] = "2099-01-01 00:00 UTC"
    assert build_board.publish_if_changed(later, build_board.envelope(board), sent.append) is False
    assert sent == []


def test_a_changed_board_sends_the_envelope_once():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    board = _run_board()
    changed = json.loads(json.dumps(board))
    droplet = next(s for s in changed["sessions"] if s["name"] == "droplet")
    droplet["questions"][0]["title"] = "A different question"
    sent = []
    assert build_board.publish_if_changed(changed, build_board.envelope(board), sent.append) is True
    assert len(sent) == 1
    assert sent[0].startswith("SWARPH-BOARD v1\n")
    assert "A different question" in sent[0]


def test_deploy_accept_sets_in_session_and_every_question_has_to_node():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    listing = (FIX / "listagents-lab.txt").read_text(encoding="utf-8")
    rows = json.loads((FIX / "commander-170.json").read_text(encoding="utf-8"))
    rows.append({
        "cell": "droplet",
        "card_id": 9001,
        "obligation_id": 9001,
        "title": "Production deploy of the board tab",
        "accept": "The commander types the production deploy go in that session. This row is a hard gate.",
        "options": [
            {"label": "Go", "text": "go", "rec": True},
            {"label": "Hold", "text": "hold", "rec": False},
        ],
    })
    board = build_board.build(listing, rows)
    questions = []
    for sess in board["sessions"]:
        questions.extend(sess["questions"])
    assert questions
    assert all(q.get("to_node") for q in questions)
    deploy = next(q for q in questions if q["title"] == "Production deploy of the board tab")
    retire = next(q for q in questions if q["obligation"] == 170)
    assert deploy["in_session"] is True
    assert deploy["to_node"] == "droplet"
    assert deploy["card"] == 9001
    assert retire["in_session"] is False


def test_an_answer_dm_drops_that_question():
    sys.path.insert(0, str(SCRIPT.parent))
    import build_board
    rows = json.loads((FIX / "commander-170.json").read_text(encoding="utf-8"))
    title = rows[0]["title"]
    kept = build_board.drop_answered(rows, [f"Re: {title}\nRetire.\n"])
    assert kept == []
