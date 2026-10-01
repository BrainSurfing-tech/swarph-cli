"""Codex queue wake sink (card #484, obligation #908).

The swarph monitor wakes the live gpt-ops session with
`codex queue --thread <thread> --message <text>` instead of typing:
edge-triggered on the drain like MuseSink (no second send while
wake_outstanding stands), with every refusal NAMED (non-zero exit, missing
binary, launch/timeout failure) — never silent success.

Run: python -m pytest tests/test_484_codex_queue_sink.py -v
"""
import pytest

from swarph_cli.commands import mesh


@pytest.fixture()
def fake_state():
    """Minimal MonitorState stand-in: ledger dict + gateway identity."""
    ledgers = {}

    class FakeState:
        gateway = "http://gw:8788"
        self_name = "gpt-ops"
        token = "tok"

        def ledger(self, name):
            return ledgers.setdefault(name, {
                "last_delivered_id": 0,
                "last_delivery_at": 0.0,
                "consecutive_failures": 0,
            })

    return FakeState()


def _dm(i, content="wake up please"):
    return {"id": i, "from_node": "lab-ovh", "kind": "question",
            "content": content, "read_at": None}


def test_parse_sink_codex_needs_thread():
    with pytest.raises(mesh.MonitorSinkError):
        mesh.parse_sink("codex:")


def test_parse_sink_codex_names_thread():
    sink = mesh.parse_sink("codex:0197abcd")
    assert sink.name == "codex:0197abcd"
    assert sink.is_push is True


def test_deliver_sends_once_with_full_body(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_codex_send",
        lambda thread, text: calls.append((thread, text)) or (True, ""),
    )
    sink = mesh.CodexSink("0197abcd")
    assert sink.deliver(fake_state, [_dm(101, "full body here")], 101) is True
    assert len(calls) == 1
    thread, text = calls[0]
    assert thread == "0197abcd"
    assert "full body here" in text
    assert "101" in text


def test_no_second_send_while_outstanding(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_codex_send",
        lambda thread, text: calls.append((thread, text)) or (True, ""),
    )
    from swarph_cli.commands import watchdog
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 3)
    sink = mesh.CodexSink("0197abcd")
    assert sink.deliver(fake_state, [_dm(101)], 101) is True
    assert len(calls) == 1
    assert sink.deliver(fake_state, [_dm(102)], 102) is True
    assert len(calls) == 1


def test_send_again_after_drain(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_codex_send",
        lambda thread, text: calls.append((thread, text)) or (True, ""),
    )
    from swarph_cli.commands import watchdog
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 3)
    sink = mesh.CodexSink("0197abcd")
    assert sink.deliver(fake_state, [_dm(101)], 101) is True
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 0)
    assert sink.deliver(fake_state, [_dm(102)], 102) is True
    assert len(calls) == 2


def test_named_failure_nonzero_exit(monkeypatch, fake_state, capsys):
    monkeypatch.setattr(
        mesh, "_codex_send",
        lambda thread, text: (False, "codex queue exited 1: no such thread"),
    )
    sink = mesh.CodexSink("0197abcd")
    assert sink.deliver(fake_state, [_dm(101)], 101) is False
    assert "codex queue" in capsys.readouterr().out


def test_codex_send_argv_shape():
    class Proc:
        returncode = 0
        stdout = "queued\n"
        stderr = ""

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["message"] = kwargs.get("input")
        return Proc()

    ok, reason = mesh._codex_send(
        "0197abcd", "WAKE",
        _run=fake_run,
        _which=lambda name: "/bin/codex",
    )
    assert (ok, reason) == (True, "")
    assert seen["argv"] == ["/bin/codex", "queue",
                            "--thread", "0197abcd",
                            "--message", "WAKE"]


def test_codex_send_maps_nonzero_exit():
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "no such thread"

    ok, reason = mesh._codex_send(
        "t", "m",
        _run=lambda *a, **k: Proc(),
        _which=lambda name: "/bin/codex",
    )
    assert ok is False
    assert "1" in reason and "no such thread" in reason


def test_codex_send_maps_missing_binary():
    ok, reason = mesh._codex_send("t", "m", _which=lambda name: None)
    assert ok is False
    assert "PATH" in reason
