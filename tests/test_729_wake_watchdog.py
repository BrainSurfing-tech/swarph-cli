"""card #729: one DM per outage, and none while a watcher is alive."""
from pathlib import Path

from swarph_cli.scripts.wake_watchdog import scan

INBOX = "/home/ubuntu/swarph_state/cursor-lin/mesh-sidecar/inbox.log"


def test_one_dm_per_outage_and_none_while_watched():
    cells = {"cursor-lin": INBOX}
    state = {}
    assert scan(cells, [], state, now=0) == []
    assert scan(cells, [], state, now=30) == []
    assert scan(cells, [], state, now=61) == ["cursor-lin"]
    assert scan(cells, [], state, now=200) == []
    alive = [f"tail -n 0 -F {INBOX} | python -m swarph_cli.scripts.dm_notify_filter"]
    assert scan(cells, alive, state, now=210) == []
    assert state["cursor-lin"]["outage"] is False
    assert scan(cells, [], state, now=220) == []
    assert scan(cells, [], state, now=281) == ["cursor-lin"]


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
    assert "SWARPH_SELF" in src
