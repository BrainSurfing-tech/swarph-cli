import json

from swarph_cli.commands.codex_waker import _load, _next_dm, _save


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
