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


def _row(title, accept, state="open", cell="lab-ovh", obligation_id=1, card_id=1006):
    return {
        "title": title,
        "accept": accept,
        "state": state,
        "cell": cell,
        "obligation_id": obligation_id,
        "card_id": card_id,
    }


def _status(name, running=True, supervisor="systemd:swarph-monitor@lab-ovh.service"):
    line = "running pid=1 sinks=tmux" if running else "not running"
    sup = f"  supervised by: {supervisor}\n" if supervisor else ""
    return f"monitor {name}: {line}\n{sup}"


def test_commander_tag_and_closed_or_answered_rows_drop():
    pub = _load()
    rows = [
        _row("Ship the timer [commander]", "do it"),
        _row("Ordinary build", "no tag here"),
        _row("Closed call [commander]", "still tagged", state="closed:pass", obligation_id=2),
        _row("Answered call", "keep [commander]", obligation_id=3),
    ]
    messages = [("lab-ovh", "Re: Answered call\n"), ("commander", "Re: Answered call\n")]
    sent = []
    changed, body = pub.run_once(
        rows=rows,
        messages=messages,
        statuses=[_status("lab-ovh")],
        tmux_sessions={"lab-ovh"},
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
    assert titles == ["Ship the timer [commander]"]
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
    assert any(item.startswith("tmux has-session -t science-claude") for item in flat)
    assert all("ListAgents" not in item for item in flat)
    info = pub.parse_monitor_status(_status("lab-ovh", running=False, supervisor=""))
    assert info["running"] is False
    roster = pub.attach_tmux([info], set())
    assert roster[0]["tmux"] is False


def test_unchanged_board_sends_nothing_across_three_runs():
    pub = _load()
    rows = [_row("Ship the timer [commander]", "do it")]
    kwargs = dict(
        rows=rows,
        messages=[],
        statuses=[_status("lab-ovh")],
        tmux_sessions={"lab-ovh"},
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
        return _Proc(json.dumps([_row("Ship the timer [commander]", "do it")]))

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
    assert all(argv[0] != "swarph" or argv[1] != "mesh" for argv in calls)
    assert "ListAgents" not in json.dumps(calls)
    service = (DEPLOY / "swarph-board-publisher.service").read_text(encoding="utf-8")
    timer = (DEPLOY / "swarph-board-publisher.timer").read_text(encoding="utf-8")
    assert "--token-file ${SWARPH_TOKEN_FILE}" in service
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
