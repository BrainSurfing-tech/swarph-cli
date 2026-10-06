"""card #1006 — auto-publish. A stub runner, never a live DM."""
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "swarph" / "skills" / "lej-session-board"
SCRIPT = SKILL / "scripts" / "publish_board.py"
DEPLOY = SKILL / "deploy"


def _load():
    spec = importlib.util.spec_from_file_location("publish_board", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(title, rest="do it", state="open", cell="lab-ovh", obligation_id=1, card_id=1006, *, tagged=True):
    accept = f"[commander] {title} | {rest}" if tagged else rest
    return {
        "id": obligation_id,
        "holder": cell,
        "card_id": card_id,
        "accept": accept,
        "state": state,
    }


# Copied verbatim from GET /board/obligations on 2026-09-30. No title, no cell.
REAL_OBLIGATION = {
    "id": 1,
    "holder": "droplet",
    "card_id": 307,
    "accept": None,
    "state": "closed:unknown",
}


def _status(name, running=True, supervisor="systemd:swarph-monitor@lab-ovh.service"):
    line = "running pid=1 sinks=tmux" if running else "not running"
    sup = f"  supervised by: {supervisor}\n" if supervisor else ""
    return f"monitor {name}: {line}\n{sup}"


def test_commander_tag_and_closed_or_answered_rows_drop():
    pub = _load()
    rows = [
        _row("Ship the timer", "do it"),
        _row("Ordinary build", "a prose mention of [commander] is not a tag", tagged=False),
        _row("Closed call", "still tagged", state="closed:pass", obligation_id=2),
        _row("Answered call", "keep it", obligation_id=3),
    ]
    messages = [("lab-ovh", "Re: Answered call\n"), ("commander", "Re: Answered call\n")]
    sent = []
    changed, body = pub.run_once(
        rows=rows,
        messages=messages,
        statuses=[_status("lab-ovh")],
        tmux_sessions={"lab"},
        previous=None,
        commander="commander",
        sender="board-publisher",
        recipient="commander",
        token_file="/home/ubuntu/.config/swarph/service-board-publisher.token",
        send=lambda body, **_k: sent.append(body),
    )
    assert changed is True
    data = json.loads(body.split("\n", 1)[1])
    titles = [q["title"] for sess in data["sessions"] for q in sess["questions"]]
    assert titles == ["Ship the timer"]
    assert "Ordinary build" not in body
    assert "Closed call" not in body
    assert "Answered call" not in body


def test_roster_is_monitor_status_and_tmux_without_a_listing_tool():
    pub = _load()
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ListAgents" not in source
    argv = pub.read_argv(cells=["lab-ovh", "science-claude"], read_token_file="/etc/swarph/read.token")
    flat = [" ".join(item) for item in argv]
    assert any(item.startswith("swarph monitor status --as lab-ovh") for item in flat)
    assert any(item.startswith("tmux has-session -t lab ") or item == "tmux has-session -t lab" for item in flat)
    assert any(item.startswith("tmux has-session -t science-claude") for item in flat)
    assert any("--as lab-ovh" in item and "--status open" in item for item in flat)
    assert all("inbox" not in item and "/messages" not in item for item in flat)
    assert all("ListAgents" not in item for item in flat)
    assert pub._supervised("swarph-monitor.service", "lab-ovh")
    assert pub._supervised("swarph-monitor@lab-ovh.service", "lab-ovh")
    info = pub.parse_monitor_status(_status("lab-ovh", running=False, supervisor=""))
    assert info["running"] is False
    roster = pub.attach_tmux([info], set())
    assert roster[0]["tmux"] is False


def test_unchanged_board_sends_nothing_across_three_runs():
    pub = _load()
    rows = [_row("Ship the timer", "do it")]
    kwargs = dict(
        rows=rows,
        messages=[],
        statuses=[_status("lab-ovh")],
        tmux_sessions={"lab"},
        commander="commander",
        sender="board-publisher",
        recipient="commander",
        token_file="/home/ubuntu/.config/swarph/service-board-publisher.token",
    )
    sent = []
    _changed, body = pub.run_once(previous=None, send=lambda body, **_k: sent.append(body), **kwargs)
    assert len(sent) == 1
    sent.clear()
    for _ in range(3):
        changed, _body = pub.run_once(previous=body, send=lambda body, **_k: sent.append(body), **kwargs)
        assert changed is False
    assert sent == []


def test_token_file_is_a_path_and_a_self_send_is_refused(tmp_path):
    pub = _load()
    secret = "super-secret-token-value"
    path = tmp_path / "service-board-publisher.token"
    path.write_text(secret, encoding="utf-8")
    argv = pub.send_argv(
        token_file=str(path),
        sender="board-publisher",
        recipient="commander",
        content_file=str(tmp_path / "body.txt"),
    )
    assert str(path) in argv
    windows = pub.send_argv(
        token_file=r"C:\swarph\service-board-publisher.token",
        sender="board-publisher",
        recipient="commander",
        content_file=r"C:\swarph\body.txt",
    )
    assert r"C:\swarph\service-board-publisher.token" in windows
    assert secret not in argv
    assert secret not in " ".join(argv)
    try:
        pub.send_argv(token_file=secret, sender="board-publisher", recipient="commander", content_file="body.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("a token value must not be accepted as --token-file")
    try:
        pub.run_once(
            rows=[],
            messages=[],
            statuses=[],
            tmux_sessions=set(),
            previous=None,
            commander="commander",
            sender="board-publisher",
            recipient="board-publisher",
            token_file=str(path),
            send=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sent")),
        )
    except pub.SelfSend:
        pass
    else:
        raise AssertionError("a self-send must be refused")


def test_stub_runner_never_dms_and_the_unit_is_not_installed():
    pub = _load()
    calls = []

    class _Proc:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def runner(argv):
        calls.append(list(argv))
        if argv[:3] == ["swarph", "monitor", "status"]:
            return _Proc(_status(argv[-1]))
        if argv[:2] == ["tmux", "has-session"]:
            return _Proc("", 0)
        return _Proc(json.dumps([_row("Ship the timer", "do it")]))

    sent = []
    pub.live_once(
        cells=["lab-ovh"],
        read_token_file="/etc/swarph/read.token",
        sender="board-publisher",
        recipient="commander",
        token_file="/etc/swarph/service-board-publisher.token",
        commander="commander",
        previous=None,
        messages=[],
        runner=runner,
        send=lambda body, **_k: sent.append(body),
    )
    assert sent
    assert all("inbox" not in argv and "/messages" not in " ".join(argv) for argv in calls)
    assert all(argv[:3] != ["swarph", "mesh", "send"] for argv in calls)
    assert "ListAgents" not in json.dumps(calls)
    service = (DEPLOY / "swarph-board-publisher.service").read_text(encoding="utf-8")
    timer = (DEPLOY / "swarph-board-publisher.timer").read_text(encoding="utf-8")
    assert "--token-file ${SWARPH_TOKEN_FILE}" in service
    assert "%h/.local/bin" in service
    path_line = next(line for line in service.splitlines() if line.startswith("Environment=PATH="))
    assert "~" not in path_line
    assert "OnUnitActiveSec=5min" in timer
    assert "super-secret" not in service
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-enabled", "swarph-board-publisher.timer"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        proc = None
    if proc is not None:
        assert proc.returncode != 0


def test_real_obligation_shape_and_a_prose_mention_does_not_count():
    pub = _load()
    prose = {
        "id": 839,
        "holder": "cursor-lin",
        "card_id": 1006,
        "state": "open",
        "accept": "PASS=a prose mention of [commander] inside the sentence does not count",
    }
    tagged = {
        "id": 1,
        "holder": "droplet",
        "card_id": 307,
        "accept": "[commander] Retire the guard | PASS=the droplet removes it",
        "state": "open",
    }
    kept = pub.commander_rows([REAL_OBLIGATION, prose, tagged])
    assert [row["title"] for row in kept] == ["Retire the guard"]
    assert kept[0]["cell"] == "droplet"
    assert kept[0]["obligation_id"] == 1
    assert "title" not in REAL_OBLIGATION
    assert "cell" not in REAL_OBLIGATION


def test_live_entry_drops_a_closed_row_and_a_failed_read_sends_nothing(tmp_path, monkeypatch):
    pub = _load()
    calls = []
    rows = [
        _row("Route the queue", "send it", cell="science-claude", obligation_id=746),
        _row("Already closed", "done", state="closed:pass", obligation_id=2),
    ]

    class _Proc:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def runner(argv):
        calls.append(list(argv))
        assert "inbox" not in argv
        assert "/messages" not in " ".join(argv)
        if argv[:3] == ["swarph", "mesh", "send"]:
            return _Proc("sent", 0)
        if argv[:3] == ["swarph", "monitor", "status"]:
            return _Proc(_status(argv[-1], supervisor="swarph-monitor.service"))
        if argv[:2] == ["tmux", "has-session"]:
            return _Proc("", 0)
        if argv[:4] == ["swarph", "board", "obligations", "list"]:
            assert "--as" in argv and argv[argv.index("--as") + 1] == "lab-ovh"
            assert "--status" in argv and argv[argv.index("--status") + 1] == "open"
            return _Proc(json.dumps(rows))
        return _Proc("", 1)

    monkeypatch.setattr(pub, "run_command", runner)
    state = tmp_path / "board.last"
    base = [
        "--live",
        "--token-file", str(tmp_path / "service.token"),
        "--read-token-file", str(tmp_path / "read.token"),
        "--as", "board-publisher",
        "--to", "commander",
        "--cells", "lab-ovh",
        "--state-file", str(state),
    ]
    assert pub.main(base) == 0
    first = (tmp_path / "board.last.body").read_text(encoding="utf-8")
    assert "Route the queue" in first
    assert "Already closed" not in first
    rows[0] = _row("Route the queue", "send it", cell="science-claude", obligation_id=746, state="closed:pass")
    state.write_text(first, encoding="utf-8")
    assert pub.main(base) == 0
    second = (tmp_path / "board.last.body").read_text(encoding="utf-8")
    assert "Route the queue" not in second
    assert all("inbox" not in argv for argv in calls)

    calls.clear()

    def failing(argv):
        calls.append(list(argv))
        if argv[:4] == ["swarph", "board", "obligations", "list"]:
            return _Proc("nope", 500)
        if argv[:3] == ["swarph", "monitor", "status"]:
            return _Proc(_status("lab-ovh"))
        if argv[:2] == ["tmux", "has-session"]:
            return _Proc("", 0)
        return _Proc("", 0)

    monkeypatch.setattr(pub, "run_command", failing)
    assert pub.main(base) == 1
    assert all(argv[:3] != ["swarph", "mesh", "send"] for argv in calls)
    assert all("inbox" not in argv for argv in calls)
    assert not hasattr(pub, "commander_replies")


def test_in_session_marker_sets_the_flag_and_strips_the_title():
    """An accept starting [commander][in-session] Re-login... builds
    in_session=true and a title without the marker. A plain [commander]
    row with no deploy or hard-gate words stays false. On main the title
    keeps [in-session] and the flag stays false.
    """
    pub = _load()
    listing = (ROOT / "tests" / "fixtures" / "session-board" / "listagents-lab.txt").read_text(encoding="utf-8")
    rows = pub.commander_rows([
        {
            "id": 5,
            "holder": "droplet",
            "card_id": 1006,
            "state": "open",
            "accept": "[commander][in-session] Re-login to the board | PASS=the commander re-logs",
        },
        {
            "id": 6,
            "holder": "droplet",
            "card_id": 1006,
            "state": "open",
            "accept": "[commander] Note the weather today | PASS=nothing",
        },
    ])
    board = pub.build_board.build(listing, rows)
    questions = [
        q for sess in board["sessions"] if sess["name"] == "droplet" for q in sess["questions"]
    ]
    marked = next(q for q in questions if q["obligation"] == 5)
    plain = next(q for q in questions if q["obligation"] == 6)
    assert marked["in_session"] is True
    assert marked["title"] == "Re-login to the board"
    assert "[in-session]" not in marked["title"]
    assert "[commander]" not in marked["title"]
    assert plain["in_session"] is False


def _commander_obligation(oid, holder="commander", step="build", accept="PASS=x | FAIL=y", title="Do it"):
    return {
        "id": oid,
        "holder": holder,
        "card_id": 291,
        "state": "open",
        "step": step,
        "accept": f"[commander] {title} | {accept}",
    }


def test_tap_closable_commander_rows_render_yes_no():
    """card #291 ruling_1157 (1): a tap-closable commander row (build or
    step-less) renders as yes/no with the fixed mapping yes=pass, no=fail —
    even when the row carries its own prose options."""
    pub = _load()
    rows = pub.commander_rows([
        _commander_obligation(1, step="build"),
        _commander_obligation(2, step=None),
    ])
    assert [r["step"] for r in rows] == ["build", None]
    by_cell = {}
    for row in rows:
        row["title"] = "Do it"
        row["options"] = [{"label": "Prose", "text": "discuss first", "rec": True}]
        by_cell.setdefault(row["cell"], []).append(row)
    questions = pub.build_board._questions(by_cell["commander"])
    assert len(questions) == 2
    for q in questions:
        assert [(o["label"], o["text"], o["outcome"]) for o in q["options"]] == [
            ("Yes", "yes", "pass"),
            ("No", "no", "fail"),
        ], "RED: tap-closable rows render yes/no, never row prose"


def test_validate_plan_review_rows_keep_display_only_options():
    """card #291 ruling_1157 (1): validate/plan-review commander rows are
    NOT taps — their options stay prose and every option carries
    outcome None (display-only, closes nothing)."""
    pub = _load()
    rows = pub.commander_rows([
        _commander_obligation(3, step="validate"),
        _commander_obligation(4, step="plan-review"),
    ])
    by_cell = {}
    for row in rows:
        row["title"] = "Check it"
        row["options"] = [{"label": "Prose", "text": "discuss first", "rec": True}]
        by_cell.setdefault(row["cell"], []).append(row)
    questions = pub.build_board._questions(by_cell["commander"])
    assert len(questions) == 2
    for q in questions:
        assert all(o.get("outcome") is None for o in q["options"]), \
            "RED: no prose option closes PASS by default"
        assert [o["label"] for o in q["options"]] == ["Prose"]


def test_default_options_are_display_only():
    """The three fallback prose options carry outcome None on every row."""
    pub = _load()
    rows = pub.commander_rows([_commander_obligation(5, step="build")])
    row = dict(rows[0])
    row["title"] = "Do it"
    row.pop("options", None)
    other = dict(row, cell="drop-on-meta-edge", holder="drop-on-meta-edge")
    for q in pub.build_board._questions([row, other]):
        if q["to_node"] == "commander":
            assert [o["outcome"] for o in q["options"]] == ["pass", "fail"]
        else:
            assert [o["label"] for o in q["options"]] == [
                "Answer on the card", "Ask for a narrower question", "Park it"]
            assert all(o["outcome"] is None for o in q["options"])
