"""Card #125 / C2 — the channel poll must never be able to kill DM delivery.

_poll_channel_subscriptions' docstring has always PROMISED this ("any failure here
must never affect the DM poll above"). Until now nothing enforced it: the function
handled non-200 HTTP status codes and nothing else, while its caller invokes it on
EVERY tick immediately after _monitor_deliver. So one persistently-malformed channel
record would not skip channels once -- it would kill DM delivery on every subsequent
tick, silently and forever.

These tests are the enforcement. Each one raises a DIFFERENT exception type from a
DIFFERENT layer, because the bug class is "an exception we did not anticipate" and a
test that only injects the anticipated one re-encodes the original mistake.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import mesh


class _State:
    """Minimal MonitorState stand-in: only the attributes the poll touches."""
    def __init__(self, tmp_path):
        self.gateway = "http://gw.invalid:8788"
        self.token = "t"
        self.self_name = "cellA"
        self.log_prefix = "[monitor]"
        self.channel_cursors = {}
        self.pending_channel_posts = []
        self.cursor = {}
        self.cursor_path = tmp_path / "cursor.json"


@pytest.fixture
def state(tmp_path):
    return _State(tmp_path)


def _assert_survives(state, capsys):
    """The contract: returns normally, and says so on stderr."""
    mesh._poll_channel_subscriptions(state)          # must NOT raise
    err = capsys.readouterr().err
    assert "channel poll failed" in err
    assert "DM delivery unaffected" in err
    return err


def test_network_fault_inside_http_get_does_not_propagate(state, monkeypatch, capsys):
    def _boom(*a, **kw):
        raise ConnectionResetError("peer reset")
    monkeypatch.setattr(mesh, "_http_get_json", _boom)
    err = _assert_survives(state, capsys)
    assert "ConnectionResetError" in err


def test_channel_record_missing_name_does_not_propagate(state, monkeypatch, capsys):
    """KeyError from c["name"] — a malformed record from the gateway."""
    monkeypatch.setattr(mesh, "_http_get_json",
                        lambda *a, **kw: (200, {"channels": [{"is_member": True}]}))
    err = _assert_survives(state, capsys)
    assert "KeyError" in err


def test_non_numeric_message_id_does_not_propagate(state, monkeypatch, capsys):
    """ValueError from int(m['id']) — a schema drift the code never anticipated."""
    calls = {"n": 0}

    def _get(url, token, *a, **kw):
        calls["n"] += 1
        if "/channels?" in url:
            return 200, {"channels": [{"name": "ops", "is_member": True}]}
        return 200, {"messages": [{"id": "not-a-number", "from_node": "other"}]}

    monkeypatch.setattr(mesh, "_http_get_json", _get)
    err = _assert_survives(state, capsys)
    assert "ValueError" in err or "TypeError" in err


def test_disk_error_on_persist_does_not_propagate(state, monkeypatch, capsys):
    """OSError from _write_cursor_atomic — full disk, bad perms, read-only mount.

    This one matters most: the failure happens AFTER the poll succeeded, so the
    unguarded version would kill DM delivery on a tick where channels worked fine.
    """
    def _get(url, token, *a, **kw):
        if "/channels?" in url:
            return 200, {"channels": [{"name": "ops", "is_member": True}]}
        return 200, {"messages": [{"id": 5, "from_node": "other"}]}

    def _no_disk(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mesh, "_http_get_json", _get)
    monkeypatch.setattr(mesh, "_write_cursor_atomic", _no_disk)
    err = _assert_survives(state, capsys)
    assert "OSError" in err


def test_healthy_poll_is_silent_and_still_works(state, monkeypatch, capsys):
    """The guard must not become a blanket that hides success or suppresses work.

    A wrapper that swallows everything would pass every test above while doing
    nothing at all -- so assert the happy path still COLLECTS and stays QUIET.
    """
    def _get(url, token, *a, **kw):
        if "/channels?" in url:
            return 200, {"channels": [{"name": "ops", "is_member": True}]}
        return 200, {"messages": [{"id": 7, "from_node": "other", "content": "hi"}]}

    monkeypatch.setattr(mesh, "_http_get_json", _get)
    monkeypatch.setattr(mesh, "_write_cursor_atomic", lambda *a, **kw: None)
    mesh._poll_channel_subscriptions(state)
    assert [m["id"] for m in state.pending_channel_posts] == [7]
    assert state.channel_cursors["ops"] == 7
    assert "channel poll failed" not in capsys.readouterr().err
