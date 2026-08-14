import json
from swarph_cli.commands import mesh


class _FakeResponses:
    def __init__(self, by_url):
        self.by_url = by_url

    def get(self, url):
        for prefix, resp in self.by_url.items():
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected URL: {url}")


def test_channel_subscriptions_discovered_and_polled(monkeypatch, tmp_path):
    fake = _FakeResponses({
        "http://gw/channels?peer=lab-ovh": (200, {
            "channels": [
                {"name": "releases", "is_member": True},
                {"name": "random", "is_member": False},
            ], "n": 2,
        }),
        "http://gw/messages?channel=releases": (200, {
            "messages": [
                {"id": 501, "channel": "releases", "from_node": "droplet",
                 "content": "v2.0.0 shipped", "kind": "fyi",
                 "created_at": "2026-08-14T00:00:00Z"},
            ],
        }),
        "http://gw/messages?to=lab-ovh": (200, {"messages": []}),
    })
    monkeypatch.setattr(mesh, "_http_get_json",
                         lambda url, token: fake.get(url))

    state = mesh.MonitorState(
        gateway="http://gw", token="tok", self_name="lab-ovh",
        state_dir=tmp_path,
        sinks=[mesh.PullSink()],
    )
    mesh._monitor_iteration(state)

    assert state.channel_cursors.get("releases") == 501
    assert "random" not in state.channel_cursors  # not a member
    assert any(p.get("channel") == "releases" for p in state.pending_channel_posts)


def test_channel_cursor_persists_across_ticks(monkeypatch, tmp_path):
    # second poll of the same channel with the SAME message must not re-surface it
    fake = _FakeResponses({
        "http://gw/channels?peer=lab-ovh": (200, {
            "channels": [{"name": "releases", "is_member": True}], "n": 1}),
        "http://gw/messages?channel=releases": (200, {"messages": [
            {"id": 501, "channel": "releases", "from_node": "droplet",
             "content": "v2.0.0 shipped", "kind": "fyi",
             "created_at": "2026-08-14T00:00:00Z"}]}),
        "http://gw/messages?to=lab-ovh": (200, {"messages": []}),
    })
    monkeypatch.setattr(mesh, "_http_get_json",
                         lambda url, token: fake.get(url))

    state = mesh.MonitorState(
        gateway="http://gw", token="tok", self_name="lab-ovh",
        state_dir=tmp_path,
        sinks=[mesh.PullSink()],
    )
    mesh._monitor_iteration(state)
    mesh._monitor_iteration(state)
    # message 501 observed once in the first tick; second tick must not re-add it
    assert len([p for p in state.pending_channel_posts if p["id"] == 501]) == 1


def test_status_surfaces_pending_channel_posts(monkeypatch, tmp_path, capsys):
    """Unit test: _print_status correctly formats pending_channel_posts when provided."""
    info = {
        "self": "lab-ovh",
        "state_dir": str(tmp_path),
        "running": True,
        "pidfile_status": "live_ours",
        "pid": 1234,
        "configured_sinks": ["pull"],
        "observation_cursor": 0,
        "unread_reportable": True,
        "sinks": [],
        "pending_channel_posts": [
            {"id": 501, "channel": "releases", "from_node": "droplet",
             "content": "v2.0.0 shipped"},
        ],
    }
    from swarph_cli.commands import monitor
    monitor._print_status(info, pending=0)
    out = capsys.readouterr().out
    assert "releases" in out
    assert "1" in out


def test_status_integration_reads_pending_channel_posts_from_cursor(monkeypatch, tmp_path, capsys):
    """Integration test: real monitor status pipeline reads and displays pending_channel_posts."""
    import argparse
    from swarph_cli.commands import monitor

    # Write cursor file with pending channel posts persisted
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text(json.dumps({
        "last_msg_id": 0,
        "last_wake_at": 0.0,
        "channel_cursors": {"releases": 501},
        "pending_channel_posts": [
            {"id": 501, "channel": "releases", "from_node": "droplet",
             "content": "v2.0.0 shipped", "kind": "fyi"},
        ],
    }))

    # Create pidfile so the monitor appears "running"
    pidfile = tmp_path / "monitor.pid"
    pidfile.write_text(json.dumps({
        "pid": 1234,
        "self": "lab-ovh",
        "sinks": ["pull"],
        "poll_s": 30,
        "cmdline": "swarph monitor start",
    }))

    # Mock pidfile_status to return live_ours
    monkeypatch.setattr(mesh, "pidfile_status",
                       lambda path: ("live_ours", {"pid": 1234, "sinks": ["pull"]}))

    # Create args for monitor status
    args = argparse.Namespace(
        self_name="lab-ovh",
        state_dir=str(tmp_path),
        gateway="http://localhost:8788",
        token_file=None,
        json=False,
        brief=False,
    )

    # Call the real _collect function
    info = monitor._collect(args)

    # Verify pending_channel_posts are in the info dict
    assert "pending_channel_posts" in info
    assert len(info["pending_channel_posts"]) == 1
    assert info["pending_channel_posts"][0]["channel"] == "releases"

    # Verify the monitor appears to be running
    assert info["running"] is True

    # Call _print_status with real data from _collect
    monitor._print_status(info, pending=0)
    out = capsys.readouterr().out

    # Verify the output includes the channel posts
    assert "releases" in out
    assert "1" in out
    assert "unread channel post" in out
