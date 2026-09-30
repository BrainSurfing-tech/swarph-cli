"""Muse session-message wake sink (card #961, obligation #842).

The swarph monitor wakes a live muse session with
`muse session-message send --target <session>` instead of typing:
edge-triggered on the drain like TmuxSink (no second send while
wake_outstanding stands), with every refusal NAMED (ingress unavailable,
unverified receipt, non-zero exit, missing binary) — never silent success.

Run: venv/bin/python -m pytest tests/test_mesh_sidecar_muse_sink.py -v
"""
import pytest

from swarph_cli.commands import mesh


@pytest.fixture()
def fake_state():
    """Minimal MonitorState stand-in: ledger dict + gateway identity."""
    ledgers = {}

    class FakeState:
        gateway = "http://gw:8788"
        self_name = "meta-muse"
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


def test_parse_sink_muse_needs_session():
    with pytest.raises(mesh.MonitorSinkError):
        mesh.parse_sink("muse:")


def test_parse_sink_muse_names_session():
    sink = mesh.parse_sink("muse:sac-961-ingress-2")
    assert sink.name == "muse:sac-961-ingress-2"
    assert sink.is_push is True


def test_deliver_sends_once_with_full_body(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_muse_send",
        lambda session, text: calls.append((session, text)) or (True, ""),
    )
    sink = mesh.MuseSink("sac-961-ingress-2")
    assert sink.deliver(fake_state, [_dm(101, "full body here")], 101) is True
    assert len(calls) == 1
    session, text = calls[0]
    assert session == "sac-961-ingress-2"
    assert "full body here" in text
    assert "101" in text


def test_no_second_send_while_outstanding(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_muse_send",
        lambda session, text: calls.append((session, text)) or (True, ""),
    )
    from swarph_cli.commands import watchdog
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 3)
    sink = mesh.MuseSink("sac-961-ingress-2")
    assert sink.deliver(fake_state, [_dm(101)], 101) is True
    assert len(calls) == 1
    # Standing wake: second delivery reports delivered with no new send.
    assert sink.deliver(fake_state, [_dm(102)], 102) is True
    assert len(calls) == 1


def test_send_again_after_drain(monkeypatch, fake_state):
    calls = []
    monkeypatch.setattr(
        mesh, "_muse_send",
        lambda session, text: calls.append((session, text)) or (True, ""),
    )
    from swarph_cli.commands import watchdog
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 3)
    sink = mesh.MuseSink("sac-961-ingress-2")
    assert sink.deliver(fake_state, [_dm(101)], 101) is True
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *a: 0)
    assert sink.deliver(fake_state, [_dm(102)], 102) is True
    assert len(calls) == 2


def test_named_failure_sender_ingress_unavailable(monkeypatch, fake_state, capsys):
    monkeypatch.setattr(
        mesh, "_muse_send",
        lambda session, text: (False, "sender ingress unavailable: sending "
                                      "process lacks the gate"),
    )
    sink = mesh.MuseSink("sac-961-ingress-2")
    assert sink.deliver(fake_state, [_dm(101)], 101) is False
    out = capsys.readouterr().out
    assert "sender" in out


def test_muse_send_maps_target_closed_to_target_reason():
    class Proc:
        returncode = 1
        stdout = "session-message send failed: external_agent_ingress_closed\n"
        stderr = ""

    ok, reason = mesh._muse_send(
        "s", "t",
        _run=lambda *a, **k: Proc(),
        _which=lambda name: "/bin/muse",
    )
    assert ok is False
    assert "target" in reason
    assert "sender" not in reason


def test_named_failure_unverified_receipt(monkeypatch, fake_state, capsys):
    monkeypatch.setattr(
        mesh, "_muse_send",
        lambda session, text: (False, "send refused (unverified_target_receipt)"),
    )
    sink = mesh.MuseSink("sac-961-ingress-2")
    assert sink.deliver(fake_state, [_dm(101)], 101) is False
    assert "unverified_target_receipt" in capsys.readouterr().out


def test_muse_send_maps_ingress_text_to_named_failure(tmp_path):
    class Proc:
        returncode = 0
        stdout = "external agent ingress is unavailable\n"
        stderr = ""

    ok, reason = mesh._muse_send(
        "s", "t",
        _run=lambda *a, **k: Proc(),
        _which=lambda name: "/bin/muse",
    )
    assert ok is False
    assert "sender" in reason


def test_muse_send_maps_nonzero_exit_to_named_failure():
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    ok, reason = mesh._muse_send(
        "s", "t",
        _run=lambda *a, **k: Proc(),
        _which=lambda name: "/bin/muse",
    )
    assert ok is False
    assert "1" in reason and "boom" in reason


def test_muse_send_maps_missing_binary_to_named_failure():
    ok, reason = mesh._muse_send("s", "t", _which=lambda name: None)
    assert ok is False
    assert "PATH" in reason


def test_muse_send_success_shape():
    class Proc:
        returncode = 0
        stdout = "sent id=1\n"
        stderr = ""

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return Proc()

    ok, reason = mesh._muse_send(
        "sac-9", "WAKE",
        _run=fake_run,
        _which=lambda name: "/bin/muse",
    )
    assert (ok, reason) == (True, "")
    assert seen["argv"] == ["/bin/muse", "session-message", "send",
                            "--target", "sac-9"]
    assert seen["input"] == "WAKE"
