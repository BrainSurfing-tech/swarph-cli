"""card #729: one DM per outage, and none while a watcher is alive."""
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
