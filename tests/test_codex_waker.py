import json

from swarph_cli.commands.codex_waker import AppServer, _load, _next_dm, _save, _single_flight


def test_next_dm_skips_self_and_old_messages(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text(
        "\n".join([
            json.dumps({"id": 10, "from_node": "gpt-lc"}),
            json.dumps({"id": 11, "from_node": "lab-ovh", "to_node": "gpt-lc", "kind": "question"}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert _next_dm(inbox, 10, "gpt-lc")["id"] == 11
    assert _next_dm(inbox, 11, "gpt-lc") is None


def test_state_round_trip_is_atomic_and_defaults_when_missing(tmp_path):
    path = tmp_path / "controller" / "cursor.json"
    assert _load(path) == {"last_message_id": 0, "thread_id": None}
    _save(path, {"last_message_id": 42, "thread_id": "thread-1"})
    assert _load(path) == {"last_message_id": 42, "thread_id": "thread-1"}


def test_next_dm_ignores_other_recipient_and_malformed_id(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text("\n".join([
        json.dumps({"id": "bad", "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": 12, "from_node": "lab", "to_node": "another"}),
        json.dumps({"id": 13, "from_node": "lab", "to_node": "gpt-lc"}),
    ]), encoding="utf-8")
    assert _next_dm(inbox, 0, "gpt-lc")["id"] == 13


def test_single_flight_refuses_overlapping_holder(tmp_path):
    path = tmp_path / "controller.lock"
    with _single_flight(path) as first:
        assert first is True
        with _single_flight(path) as second:
            assert second is False


class _FakeProcess:
    def __init__(self, pid, exited=False):
        self.pid = pid
        self.exited = exited
        self.killed = False

    def poll(self):
        return 0 if self.exited else None

    def kill(self):
        self.killed = True


def test_timeout_kills_only_the_owned_app_server_child():
    app = AppServer.__new__(AppServer)
    app.proc = _FakeProcess(123)
    app.child_pid = 123
    app._kill_owned_child()
    assert app.proc.killed is True

    app.proc = _FakeProcess(456)
    app._kill_owned_child()
    assert app.proc.killed is False
