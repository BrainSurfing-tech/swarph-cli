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
