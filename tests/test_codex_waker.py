import json
import multiprocessing
import os
import subprocess
import threading
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

import swarph_cli.commands.codex_waker as waker
from swarph_cli.commands.codex_waker import AppServer, AppServerProtocolError, _authorize_outbox_reply, _drain_outbox, _load, _next_dm, _save, _single_flight


def _hold_lock(path: str, ready, release) -> None:
    with _single_flight(Path(path)) as acquired:
        ready.put(acquired)
        if acquired:
            release.wait(10)


def _crash_after_reset_state_save(args) -> None:
    original = waker._append_reset_event

    def interrupt_completion(state_dir, event):
        if event["event"] == "completed":
            os._exit(23)
        original(state_dir, event)

    waker._append_reset_event = interrupt_completion
    waker.run_codex_waker(args)


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


def test_next_dm_responds_only_to_questions(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text(
        "\n".join([
            json.dumps({"id": 12, "from_node": "lab-ovh", "to_node": "gpt-lc", "kind": "answer"}),
            json.dumps({"id": 13, "from_node": "lab-ovh", "to_node": "gpt-lc", "kind": "fyi"}),
            json.dumps({"id": 14, "from_node": "lab-ovh", "to_node": "gpt-lc", "kind": "question"}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert _next_dm(inbox, 10, "gpt-lc")["id"] == 14


@pytest.mark.parametrize("kind", ["answer", "fyi"])
def test_non_question_dm_never_starts_a_turn_or_advances_controller_state(tmp_path, monkeypatch, kind):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(
        json.dumps({"id": 7, "from_node": "lab", "to_node": "gpt-lc", "kind": kind}) + "\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "controller"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    _FakeAppServer.instances = []
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    assert waker.run_codex_waker(args) == 0
    assert _FakeAppServer.instances == []
    assert _load(state_dir / "cursor.json") == {"last_message_id": 0, "thread_id": None}
    assert not list(outbox.glob("*.json"))
    assert not (state_dir / "outbox-authorizations").exists()


def test_non_question_dm_does_not_block_a_later_question(tmp_path, monkeypatch):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(
        "\n".join([
            json.dumps({"id": 7, "from_node": "lab", "to_node": "gpt-lc", "kind": "answer"}),
            json.dumps({"id": 8, "from_node": "lab", "to_node": "gpt-lc", "kind": "fyi"}),
            json.dumps({"id": 9, "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}),
        ]) + "\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "controller"
    outbox = tmp_path / "outbox"
    _write_reply(outbox, 9)
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    _FakeAppServer.instances = []
    _FakeAppServer.fail_first_turn = False
    _FakeAppServer.resume_error = None
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    assert waker.run_codex_waker(args) == 0
    assert len(_FakeAppServer.instances) == 1
    assert _load(state_dir / "cursor.json")["last_message_id"] == 9
    assert not (state_dir / "outbox-authorizations" / "7.json").exists()
    assert not (state_dir / "outbox-authorizations" / "8.json").exists()


def test_state_round_trip_is_atomic_and_defaults_when_missing(tmp_path):
    path = tmp_path / "controller" / "cursor.json"
    assert _load(path) == {"last_message_id": 0, "thread_id": None}
    _save(path, {"last_message_id": 42, "thread_id": "thread-1"})
    assert _load(path) == {"last_message_id": 42, "thread_id": "thread-1"}


def test_next_dm_ignores_other_recipient_and_malformed_id(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text("\n".join([
        json.dumps({"id": "bad", "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": True, "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": 1.5, "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": "\u0667", "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": " 7 ", "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": "7.0", "from_node": "lab", "to_node": "gpt-lc"}),
        json.dumps({"id": 12, "from_node": "lab", "to_node": "another"}),
        json.dumps({"id": "13", "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}),
    ]), encoding="utf-8")
    dm = _next_dm(inbox, 0, "gpt-lc")
    assert dm["id"] == 13
    assert isinstance(dm["id"], int)


def test_single_flight_refuses_overlapping_holder(tmp_path):
    path = tmp_path / "controller.lock"
    with _single_flight(path) as first:
        assert first is True
        with _single_flight(path) as second:
            assert second is False


def test_single_flight_recovers_after_holder_termination(tmp_path):
    """The OS, not a stale PID probe, releases the controller lock on death."""
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    path = tmp_path / "controller.lock"
    holder = context.Process(target=_hold_lock, args=(str(path), ready, release))
    holder.start()
    try:
        assert ready.get(timeout=5) is True
        assert holder.is_alive()
        with _single_flight(path) as contender:
            assert contender is False

        holder.terminate()  # This test owns the holder process.
        holder.join(timeout=5)
        assert not holder.is_alive()
        with _single_flight(path) as later_contender:
            assert later_contender is True
    finally:
        if holder.is_alive():
            holder.terminate()
        holder.join(timeout=5)


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


class _FakeAppServer:
    instances = []
    fail_first_turn = False
    resume_error = None

    def __init__(self, *_args):
        self.requests = []
        type(self).instances.append(self)

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": f"thread-{len(type(self).instances)}"}}
        if method == "thread/resume" and type(self).resume_error:
            raise type(self).resume_error
        if method == "turn/start":
            if type(self).fail_first_turn:
                type(self).fail_first_turn = False
                raise RuntimeError("simulated first-turn failure")
            return {"turn": {"id": "turn-1"}}
        return {}

    def wait_completed(self, *_args):
        return None

    def close(self):
        return None


def _write_reply(outbox, message_id, to_node="lab"):
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{message_id}.json").write_text(json.dumps({
        "message_id": message_id,
        "to_node": to_node,
        "kind": "answer",
        "content": "Synthetic reply",
    }), encoding="utf-8")


def test_failed_first_turn_does_not_persist_or_resume_empty_thread(tmp_path, monkeypatch):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 7, "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}) + "\n", encoding="utf-8")
    state_dir = tmp_path / "controller"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    _FakeAppServer.instances = []
    _FakeAppServer.fail_first_turn = True
    _FakeAppServer.resume_error = None
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    with pytest.raises(RuntimeError, match="simulated first-turn failure"):
        waker.run_codex_waker(args)
    assert _load(state_dir / "cursor.json") == {"last_message_id": 0, "thread_id": None}

    _write_reply(outbox, 7)
    assert waker.run_codex_waker(args) == 0
    retry_methods = [method for method, _params in _FakeAppServer.instances[-1].requests]
    assert "thread/start" in retry_methods
    assert "thread/resume" not in retry_methods
    assert _load(state_dir / "cursor.json") == {"last_message_id": 7, "thread_id": "thread-2"}


def test_unclassified_resume_protocol_error_retains_state(tmp_path, monkeypatch):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 8, "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}) + "\n", encoding="utf-8")
    state_dir = tmp_path / "controller"
    state_path = state_dir / "cursor.json"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)
    _FakeAppServer.instances = []
    _FakeAppServer.fail_first_turn = False
    _FakeAppServer.resume_error = AppServerProtocolError({"code": -32001, "message": "thread not found"})
    _save(state_path, {"last_message_id": 0, "thread_id": "expired-thread"})

    with pytest.raises(AppServerProtocolError, match="thread not found") as exc_info:
        waker.run_codex_waker(args)
    assert exc_info.value.code == -32001
    assert _load(state_path) == {"last_message_id": 0, "thread_id": "expired-thread"}
    assert [method for method, _params in _FakeAppServer.instances[-1].requests] == ["thread/resume"]

    _save(state_path, {"last_message_id": 0, "thread_id": "transient-thread"})
    _FakeAppServer.resume_error = RuntimeError("temporary app-server fault")
    with pytest.raises(RuntimeError, match="temporary app-server fault"):
        waker.run_codex_waker(args)
    assert _load(state_path) == {"last_message_id": 0, "thread_id": "transient-thread"}
    assert [method for method, _params in _FakeAppServer.instances[-1].requests] == ["thread/resume"]


def test_operator_reset_clears_only_thread_id_and_writes_audit_record(tmp_path, monkeypatch):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 9, "from_node": "lab", "to_node": "gpt-lc"}) + "\n", encoding="utf-8")
    state_dir = tmp_path / "controller"
    state_path = state_dir / "cursor.json"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox), "--reset-thread",
        "--acknowledge-thread-reset", "--reset-reason", "App Server reported a stale thread",
    ]
    _save(state_path, {"last_message_id": 8, "thread_id": "expired-thread"})
    _FakeAppServer.instances = []
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    assert waker.run_codex_waker(args) == 0
    assert _FakeAppServer.instances == []
    assert _load(state_path) == {"last_message_id": 8, "thread_id": None}
    audit = [json.loads(line) for line in (state_dir / "thread-reset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in audit] == ["requested", "completed"]
    assert audit[0]["operation_id"] == audit[1]["operation_id"]
    assert audit[0]["previous_thread_id"] == "expired-thread"
    assert audit[0]["reason"] == "App Server reported a stale thread"

    second_args = args[:-1] + ["Operator repeated the reset"]
    assert waker.run_codex_waker(second_args) == 0
    audit = [json.loads(line) for line in (state_dir / "thread-reset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in audit] == ["requested", "completed", "requested", "completed"]
    assert audit[2]["operation_id"] == audit[3]["operation_id"]
    assert audit[2]["operation_id"] != audit[0]["operation_id"]

    with pytest.raises(SystemExit):
        waker.run_codex_waker(args[:-3])


def test_reset_crash_after_state_save_leaves_an_incomplete_audit_request(tmp_path):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text("", encoding="utf-8")
    state_dir = tmp_path / "controller"
    state_path = state_dir / "cursor.json"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox), "--reset-thread",
        "--acknowledge-thread-reset", "--reset-reason", "Controlled crash test",
    ]
    _save(state_path, {"last_message_id": 8, "thread_id": "expired-thread"})
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crash_after_reset_state_save, args=(args,))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 23
    assert _load(state_path) == {"last_message_id": 8, "thread_id": None}
    audit = [json.loads(line) for line in (state_dir / "thread-reset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in audit] == ["requested"]


def test_completed_turn_requires_a_valid_outbox_reply_before_acknowledging_dm(tmp_path, monkeypatch):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 10, "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}) + "\n", encoding="utf-8")
    state_dir = tmp_path / "controller"
    state_path = state_dir / "cursor.json"
    outbox = tmp_path / "outbox"
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    _FakeAppServer.instances = []
    _FakeAppServer.fail_first_turn = False
    _FakeAppServer.resume_error = None
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    with pytest.raises(RuntimeError, match="missing outbox reply"):
        waker.run_codex_waker(args)
    assert _load(state_path) == {"last_message_id": 0, "thread_id": None}

    _write_reply(outbox, 10, to_node="wrong-recipient")
    with pytest.raises(RuntimeError, match="destination does not match"):
        waker.run_codex_waker(args)
    assert _load(state_path) == {"last_message_id": 0, "thread_id": None}


@pytest.mark.parametrize(("payload", "error"), [
    ("{", "invalid outbox JSON"),
    ({"message_id": 10, "to_node": "lab", "kind": "answer", "content": "stale reply"}, "message_id does not match"),
    ({"message_id": 99, "to_node": "lab", "kind": "answer", "content": "reply"}, "message_id does not match"),
    ({"message_id": 11, "to_node": "lab", "kind": "decline", "content": "reply"}, "kind .* must be answer"),
    ({"message_id": 11, "to_node": "lab", "kind": "answer", "content": ""}, "content .* non-empty"),
    ({"message_id": 11, "to_node": "lab", "kind": "answer", "content": 42}, "content .* non-empty"),
])
def test_invalid_outbox_envelope_never_advances_state(tmp_path, monkeypatch, payload, error):
    inbox = tmp_path / "monitor" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 11, "from_node": "lab", "to_node": "gpt-lc", "kind": "question"}) + "\n", encoding="utf-8")
    state_dir = tmp_path / "controller"
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    content = payload if isinstance(payload, str) else json.dumps(payload)
    (outbox / "11.json").write_text(content, encoding="utf-8")
    args = [
        "--inbox-log", str(inbox), "--state-dir", str(state_dir), "--self", "gpt-lc",
        "--cwd", str(tmp_path / "workspace"), "--outbox-dir", str(outbox),
    ]
    _FakeAppServer.instances = []
    _FakeAppServer.fail_first_turn = False
    _FakeAppServer.resume_error = None
    monkeypatch.setattr(waker, "AppServer", _FakeAppServer)

    with pytest.raises(RuntimeError, match=error):
        waker.run_codex_waker(args)
    assert _load(state_dir / "cursor.json") == {"last_message_id": 0, "thread_id": None}


def test_wait_completed_ignores_a_completion_for_another_turn():
    app = AppServer.__new__(AppServer)
    app.timeout = 1
    app.proc = SimpleNamespace(stdout=True)
    app.events = Queue()
    app.events.put(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "other", "status": "completed"}}}))
    app.events.put(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "wanted", "status": "completed"}}}))
    app.wait_completed("thread-1", "wanted")


def test_outbox_deletes_only_after_mesh_send_succeeds(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 21, to_node="peer")
    _save(state_dir / "cursor.json", {"last_message_id": 21, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 21, "from_node": "peer"})
    calls = []
    monkeypatch.setattr(
        "swarph_cli.commands.codex_waker.subprocess.run",
        lambda args, check, timeout: calls.append((args, timeout)),
    )
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert calls and not (outbox / "21.json").exists()
    assert not (state_dir / "outbox-authorizations" / "21.json").exists()
    assert calls[0][0][calls[0][0].index("--content") + 1] == "Synthetic reply"
    assert calls[0][1] == waker._OUTBOX_SEND_TIMEOUT_S


def test_outbox_retains_reply_when_mesh_send_fails(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 22, to_node="peer")
    entry = outbox / "22.json"
    _save(state_dir / "cursor.json", {"last_message_id": 22, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 22, "from_node": "peer"})
    monkeypatch.setattr(
        "swarph_cli.commands.codex_waker.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "swarph")),
    )
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert entry.exists()


def test_timed_out_send_retains_its_entry_and_continues_to_later_reply(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 23, to_node="peer")
    _write_reply(outbox, 24, to_node="peer")
    _save(state_dir / "cursor.json", {"last_message_id": 24, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 23, "from_node": "peer"})
    _authorize_outbox_reply(state_dir, {"id": 24, "from_node": "peer"})
    calls = []

    def send(args, check, timeout):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr("swarph_cli.commands.codex_waker.subprocess.run", send)
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert (outbox / "23.json").exists()
    assert not (outbox / "24.json").exists()
    assert len(calls) == 2


def test_overlapping_drainers_do_not_double_send(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 25, to_node="peer")
    _save(state_dir / "cursor.json", {"last_message_id": 25, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 25, "from_node": "peer"})
    started = threading.Event()
    release = threading.Event()
    calls = []

    def send(args, check, timeout):
        calls.append(args)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr("swarph_cli.commands.codex_waker.subprocess.run", send)
    first = threading.Thread(
        target=_drain_outbox,
        args=(outbox, state_dir, "self", "http://gateway", "/token", "swarph"),
    )
    first.start()
    assert started.wait(5)
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert len(calls) == 1
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert not (outbox / "25.json").exists()


def test_outbox_rejects_noncanonical_message_filename(tmp_path):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 23, to_node="peer")
    (outbox / "23.json").rename(outbox / "023.json")
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert (outbox / "invalid" / "023.json").exists()


def test_outbox_rejects_agent_asserted_destination(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 24, to_node="other-peer")
    _save(state_dir / "cursor.json", {"last_message_id": 24, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 24, "from_node": "peer"})
    calls = []
    monkeypatch.setattr(
        "swarph_cli.commands.codex_waker.subprocess.run",
        lambda args, check, timeout: calls.append(args),
    )
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert calls == []
    assert (outbox / "invalid" / "24.json").exists()


def test_invalid_outbox_entry_does_not_block_later_authorized_reply(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    state_dir = tmp_path / "state"
    _write_reply(outbox, 25, to_node="peer")
    (outbox / "25.json").rename(outbox / "001.json")
    _write_reply(outbox, 26, to_node="peer")
    _save(state_dir / "cursor.json", {"last_message_id": 26, "thread_id": "thread-1"})
    _authorize_outbox_reply(state_dir, {"id": 26, "from_node": "peer"})
    calls = []
    monkeypatch.setattr(
        "swarph_cli.commands.codex_waker.subprocess.run",
        lambda args, check, timeout: calls.append(args),
    )
    _drain_outbox(outbox, state_dir, "self", "http://gateway", "/token", "swarph")
    assert (outbox / "invalid" / "001.json").exists()
    assert not (outbox / "26.json").exists()
    assert len(calls) == 1


def test_drain_mode_does_not_require_wake_mode_arguments(tmp_path):
    assert waker.run_codex_waker([
        "--drain-outbox", "--self", "gpt-lc", "--state-dir", str(tmp_path / "state"),
        "--cwd", str(tmp_path / "cwd"), "--outbox-dir", str(tmp_path / "outbox"), "--gateway", "http://gateway",
        "--token-file", str(tmp_path / "token"),
    ]) == 0


def test_drain_mode_rejects_state_dir_inside_agent_cwd(tmp_path):
    cwd = tmp_path / "workspace"
    with pytest.raises(SystemExit):
        waker.run_codex_waker([
            "--drain-outbox", "--self", "gpt-lc", "--state-dir", str(cwd / "state"),
            "--cwd", str(cwd), "--outbox-dir", str(tmp_path / "outbox"),
            "--gateway", "http://gateway", "--token-file", str(tmp_path / "token"),
        ])


def test_normal_mode_rejects_nested_state_and_outbox_paths(tmp_path):
    state_dir = tmp_path / "state"
    with pytest.raises(SystemExit):
        waker.run_codex_waker([
            "--self", "gpt-lc", "--state-dir", str(state_dir), "--cwd", str(tmp_path / "cwd"),
            "--inbox-log", str(tmp_path / "monitor" / "inbox.log"),
            "--outbox-dir", str(state_dir / "outbox"),
        ])
