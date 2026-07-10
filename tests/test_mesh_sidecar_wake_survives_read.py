"""The sidecar's wake must NOT depend on the unread flag.

Now that `swarph mesh inbox` marks messages read (INCIDENT 2026-07-10), a DM read
before the sidecar's next poll would vanish from an `unread_only=true` query and the
cell would never be woken. The sidecar already filters on `id > last_msg_id`, so the
unread filter was redundant — and, after the mark-read fix, actively dangerous.

Run: venv/bin/python -m pytest tests/test_mesh_sidecar_wake_survives_read.py -v
"""
import urllib.parse

from swarph_cli.commands import mesh


def _state(tmp_path, wake_min_interval_s=0):
    return mesh.MeshSidecarState(
        self_name="grok-researcher",
        state_dir=tmp_path,
        gateway="http://gw:8788",
        token="tok",
        tmux_target="grok:0.0",
        poll_s=90,
        wake_min_interval_s=wake_min_interval_s,
    )


def test_sidecar_poll_does_not_filter_on_unread(monkeypatch, tmp_path):
    """A read-but-new message must still reach the sidecar and wake the cell."""
    seen_urls = []

    def fake_get(url, token, **k):
        seen_urls.append(url)
        # ALREADY READ, but newer than the cursor — must still wake.
        return (200, {"messages": [{
            "id": 4444, "from_node": "gridiron", "kind": "question",
            "content": "buzz please", "read_at": "2026-07-10T07:40:00Z",
        }]})

    woke = []
    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: woke.append(target) or True)

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)

    assert seen_urls, "the sidecar polled"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen_urls[0]).query)
    assert "unread_only" not in q, (
        "sidecar must not filter on unread — mark-read would silently kill wakes"
    )
    assert woke == ["grok:0.0"], "an already-read but NEW message still wakes the cell"


def test_sidecar_still_ignores_messages_at_or_below_cursor(monkeypatch, tmp_path):
    """Dropping unread_only must not make the sidecar re-wake on old mail."""
    monkeypatch.setattr(mesh, "_http_get_json", lambda url, tok, **k: (200, {"messages": [
        {"id": 10, "from_node": "gridiron", "kind": "fyi", "content": "old", "read_at": None},
    ]}))
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    state = _state(tmp_path)
    state.cursor["last_msg_id"] = 10
    mesh._sidecar_iteration(state)
    assert woke == [], "id <= last_msg_id must not wake the cell"
