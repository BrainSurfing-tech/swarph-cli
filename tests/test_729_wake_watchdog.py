"""card #729: one DM per outage, and none while a watcher is alive."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from swarph_cli.scripts.wake_watchdog import scan

INBOX = "/home/ubuntu/swarph_state/cursor-lin/mesh-sidecar/inbox.log"


def test_one_dm_per_outage_and_none_while_watched():
    cells = {"cursor-lin": INBOX}
    state = {}
    assert scan(cells, [], state, now=0) == []
    assert scan(cells, [], state, now=30) == []
    assert scan(cells, [], state, now=61) == ["cursor-lin"]
    assert state["cursor-lin"].get("outage") is not True
    state["cursor-lin"]["outage"] = True
    assert scan(cells, [], state, now=200) == []
    alive = [f"tail -n 0 -F {INBOX} | python -m swarph_cli.scripts.dm_notify_filter"]
    assert scan(cells, alive, state, now=210) == []
    assert state["cursor-lin"]["outage"] is False
    assert scan(cells, [], state, now=220) == []
    assert scan(cells, [], state, now=281) == ["cursor-lin"]


def test_a_missing_listing_binary_is_an_empty_listing(monkeypatch):
    import swarph_cli.scripts.wake_watchdog as w

    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(w.subprocess, "run", boom)
    assert w._crontab() == ""
    assert w._timer_lines() == []
    assert w._listing(["ps", "-eo", "args"]) == ""


def test_cron_poll_cell_is_not_flagged():
    cells = {"drop-on-meta-edge": "/tmp/drop-inbox"}
    state = {}
    cron = ("*/2 * * * * /usr/bin/python3 "
            "/home/ubuntu/drop-on-meta-edge/.claude/hooks/dm_wake.py\n")
    assert scan(cells, [], state, now=0, crontab=cron) == []
    assert scan(cells, [], state, now=120, crontab=cron) == []
    commented = "# */2 * * * * python3 /home/ubuntu/drop-on-meta-edge/.claude/hooks/dm_wake.py\n"
    again = {}
    assert scan(cells, [], again, now=0, crontab=commented) == []
    assert scan(cells, [], again, now=120, crontab=commented) == ["drop-on-meta-edge"]


def test_an_unrelated_process_naming_the_inbox_is_not_a_watcher():
    cells = {"cursor-lin": INBOX}
    state = {}
    noise = [f"claude --append-system-prompt read {INBOX} and summarize"]
    assert scan(cells, noise, state, now=0) == []
    assert scan(cells, noise, state, now=120) == ["cursor-lin"]


def test_codex_waker_timer_counts_as_watched():
    cells = {"gpt-ops": "/tmp/gpt-ops-inbox"}
    state = {}
    timers = ["swarph-codex-waker@gpt-ops.timer"]
    assert scan(cells, [], state, now=0, timers=timers) == []
    assert scan(cells, [], state, now=120, timers=timers) == []
    assert state["gpt-ops"].get("outage") is not True


def test_main_guard_is_required():
    src = Path("src/swarph_cli/scripts/wake_watchdog.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    assert "cursor-lin" not in src
    unit = Path("deploy/wake-watchdog.service").read_text(encoding="utf-8")
    assert "PYTHONPATH" not in unit
    assert "--as lab-ovh" in unit
    assert "Environment=MESH_GATEWAY_URL=http://100.64.189.91:8788" in unit


def _cell(root: Path, name: str) -> None:
    inbox = root / name / "mesh-sidecar"
    inbox.mkdir(parents=True)
    (inbox / "inbox.log").write_text("")


def _stub(tmp_path: Path):
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        exit "$STUB_RC"
    """))
    stub.chmod(0o755)
    return stub


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0; the escalation assertions run on Linux",
)
def test_escalation_is_one_dm_to_the_peer_and_not_to_itself(tmp_path):
    root = tmp_path / "state"
    _cell(root, "nobody")
    _cell(root, "lab-ovh")
    _cell(root, "watcher-peer")
    state_path = tmp_path / "wake.json"
    log = tmp_path / "stub.log"
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(_stub(tmp_path)),
        "STUB_LOG": str(log),
        "STUB_RC": "0",
    }
    env.pop("SWARPH_SELF", None)
    subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, check=True)
    saved = json.loads(state_path.read_text())
    saved["nobody"]["missing_since"] = 0
    state_path.write_text(json.dumps(saved))
    subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, check=True)
    text = log.read_text()
    assert "mesh send nobody" in text
    assert "cards say 729" in text
    assert text.count("mesh send drop-on-meta-edge") == 1
    assert json.loads(state_path.read_text())["nobody"]["outage"] is True
    log.write_text("")
    subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, check=True)
    assert log.read_text().strip() == ""
    state_path.write_text(json.dumps({"watcher-peer": {"missing_since": 0}}))
    log.write_text("")
    subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "watcher-peer"],
        env=env, check=True)
    own = log.read_text()
    assert own.count("mesh send watcher-peer") == 1


def test_sender_equal_to_escalation_peer_saves_nothing(tmp_path):
    root = tmp_path / "state"
    _cell(root, "nobody")
    state_path = tmp_path / "wake.json"
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
    }
    env.pop("SWARPH_SELF", None)
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "lab-ovh"],
        env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "escalation peer equals the sender" in proc.stderr
    assert not state_path.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0; the failed-send assertions run on Linux",
)
def test_failed_send_is_not_saved_as_alerted_and_the_next_run_retries(tmp_path):
    root = tmp_path / "state"
    _cell(root, "nobody")
    state_path = tmp_path / "wake.json"
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        exit "$STUB_RC"
    """))
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(stub),
        "STUB_LOG": str(log),
        "STUB_RC": "1",
    }
    env.pop("SWARPH_SELF", None)
    # first pass only records missing_since
    subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "lab-ovh"],
        env=env, check=True)
    saved = json.loads(state_path.read_text())
    saved["nobody"]["missing_since"] = 0
    state_path.write_text(json.dumps(saved))
    failed = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "lab-ovh"],
        env=env)
    assert failed.returncode != 0
    after = json.loads(state_path.read_text())
    assert after["nobody"].get("outage") is not True
    assert "--as lab-ovh" in log.read_text()
    env["STUB_RC"] = "0"
    retried = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "lab-ovh"],
        env=env, check=True)
    assert retried.returncode == 0
    done = json.loads(state_path.read_text())
    assert done["nobody"]["outage"] is True
