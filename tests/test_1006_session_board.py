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
            for field in ("id", "to", "title", "options"):
                assert field in q
            assert 2 <= len(q["options"]) <= 3
            assert sum(1 for opt in q["options"] if opt.get("rec")) == 1
            for opt in q["options"]:
                assert opt["label"] and opt["text"]
    for group in board["groups"]:
        assert "title" in group and "items" in group


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
