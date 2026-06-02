"""Tests for ``swarph mesh sidecar`` wake behavior."""

from __future__ import annotations

import json
import time
from pathlib import Path

from swarph_cli.commands import mesh


def _state(tmp_path: Path) -> mesh.MeshSidecarState:
    return mesh.MeshSidecarState(
        self_name="gpt-ops",
        state_dir=tmp_path,
        gateway="http://gateway:8788",
        token="tok",
        tmux_target="gpt-ops-pane",
        poll_s=30,
        wake_min_interval_s=60,
    )


def test_sidecar_wakes_on_new_mail_and_advances_cursor(tmp_path, monkeypatch):
    state = _state(tmp_path)
    captured = {}

    def fake_get(url, token, *, timeout=10.0):
        captured["url"] = url
        captured["token"] = token
        return 200, {
            "messages": [
                {
                    "id": 2,
                    "from_node": "lab-ovh",
                    "to_node": "gpt-ops",
                    "kind": "question",
                    "content": "second",
                    "created_at": "z",
                },
                {
                    "id": 1,
                    "from_node": "lab-ovh",
                    "to_node": "gpt-ops",
                    "kind": "fyi",
                    "content": "first",
                    "created_at": "z",
                },
            ]
        }

    wakes = []
    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: wakes.append(target) or True)
    monkeypatch.setattr(mesh.time, "time", lambda: 1000.0)

    mesh._sidecar_iteration(state)

    assert captured["token"] == "tok"
    assert "to=gpt-ops" in captured["url"]
    assert "to_node=" not in captured["url"]
    assert "unread_only=true" in captured["url"]
    assert wakes == ["gpt-ops-pane"]
    assert state.wakes_sent == 1
    assert state.dms_seen == 2
    assert state.cursor["last_msg_id"] == 2
    assert state.cursor["last_wake_at"] == 1000.0
    disk_cursor = json.loads((tmp_path / "cursor.json").read_text())
    assert disk_cursor["last_msg_id"] == 2
    log_ids = [
        json.loads(line)["id"]
        for line in (tmp_path / "inbox.log").read_text().splitlines()
    ]
    assert log_ids == [1, 2]


def test_sidecar_no_wake_on_empty_inbox(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (200, {"messages": []}),
    )
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: (_ for _ in ()).throw(AssertionError("no wake")))
    mesh._sidecar_iteration(state)
    assert state.consecutive_empty == 1
    assert state.dms_seen == 0
    assert not (tmp_path / "cursor.json").exists()


def test_sidecar_filters_old_and_self_messages(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.cursor["last_msg_id"] = 10
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (
            200,
            {
                "messages": [
                    {"id": 9, "from_node": "lab-ovh", "content": "old"},
                    {"id": 11, "from_node": "gpt-ops", "content": "self"},
                    {"id": 12, "from_node": "lab-ovh", "content": "new"},
                ]
            },
        ),
    )
    wakes = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: wakes.append(target) or True)
    monkeypatch.setattr(mesh.time, "time", lambda: 1000.0)
    mesh._sidecar_iteration(state)
    assert wakes == ["gpt-ops-pane"]
    assert state.cursor["last_msg_id"] == 12
    log_lines = (tmp_path / "inbox.log").read_text().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["id"] == 12


def test_sidecar_idle_guard_suppresses_wake_without_advancing_cursor(
    tmp_path, monkeypatch, capsys
):
    state = _state(tmp_path)
    state.cursor["last_wake_at"] = 995.0
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (
            200,
            {"messages": [{"id": 1, "from_node": "lab-ovh", "content": "new"}]},
        ),
    )
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: (_ for _ in ()).throw(AssertionError("guarded")))
    monkeypatch.setattr(mesh.time, "time", lambda: 1000.0)
    mesh._sidecar_iteration(state)
    assert state.cursor["last_msg_id"] == 0
    assert state.cursor["last_wake_at"] == 995.0
    assert state.wakes_sent == 0
    assert not (tmp_path / "cursor.json").exists()
    assert "wake suppressed" in capsys.readouterr().out


def test_sidecar_wakes_throttled_message_after_guard_window(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.cursor["last_wake_at"] = 995.0
    now = 1000.0

    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (
            200,
            {"messages": [{"id": 1, "from_node": "lab-ovh", "content": "new"}]},
        ),
    )
    wakes = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: wakes.append(target) or True)
    monkeypatch.setattr(mesh.time, "time", lambda: now)

    mesh._sidecar_iteration(state)
    assert wakes == []
    assert state.cursor["last_msg_id"] == 0

    now = 1060.0
    mesh._sidecar_iteration(state)
    assert wakes == ["gpt-ops-pane"]
    assert state.cursor["last_msg_id"] == 1
    assert state.cursor["last_wake_at"] == 1060.0


def test_sidecar_corrupt_cursor_defaults_without_crashing(tmp_path, capsys):
    (tmp_path / "cursor.json").write_text("{", encoding="utf-8")

    state = _state(tmp_path)

    assert state.cursor["last_msg_id"] == 0
    assert state.cursor["last_wake_at"] == 0.0
    assert "ignoring unreadable cursor" in capsys.readouterr().err


def test_sidecar_network_failure_records_disconnect(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (0, {"detail": "unreachable"}),
    )
    monkeypatch.setattr(mesh.time, "time", lambda: 1000.0)
    mesh._sidecar_iteration(state)
    assert state.disconnect_since == 1000.0


def test_sidecar_5xx_records_disconnect_and_4xx_does_not(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (503, {"detail": "down"}),
    )
    monkeypatch.setattr(mesh.time, "time", lambda: 1000.0)
    mesh._sidecar_iteration(state)
    assert state.disconnect_since == 1000.0

    state.disconnect_since = None
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (401, {"detail": "bad token"}),
    )
    mesh._sidecar_iteration(state)
    assert state.disconnect_since is None


def test_sidecar_requires_explicit_tmux_target(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_TMUX_TARGET", raising=False)
    rc = mesh.run_mesh(["sidecar", "--state-dir", str(tmp_path), "--once"])
    assert rc == 2
    assert "tmux-target" in capsys.readouterr().err


def test_sidecar_uses_env_tmux_target(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TMUX_TARGET", "pane")
    captured = {}

    def fake_iter(state):
        captured["self"] = state.self_name
        captured["target"] = state.tmux_target

    monkeypatch.setattr(mesh, "_sidecar_iteration", fake_iter)
    rc = mesh.run_mesh(["sidecar", "--state-dir", str(tmp_path), "--once"])
    assert rc == 0
    assert captured == {"self": "gpt-ops", "target": "pane"}


def test_select_next_poll_seconds_backoff(tmp_path):
    state = _state(tmp_path)
    assert mesh._select_next_poll_seconds(state) == 30
    state.consecutive_empty = mesh._BACKOFF_EMPTY_THRESHOLD
    assert mesh._select_next_poll_seconds(state) == mesh._BACKOFF_EMPTY_SECONDS
    state.consecutive_empty = 0
    state.disconnect_since = time.time() - (mesh._BACKOFF_5XX_THRESHOLD_SECONDS + 1)
    assert mesh._select_next_poll_seconds(state) == mesh._BACKOFF_5XX_SECONDS
