import json

from swarph_cli.commands import peer_reply_drain as command
from swarph_cli.peer_service_reply import ReplyDrainResult


def test_peer_reply_drain_is_a_dedicated_cli_verb():
    from swarph_cli.main import _VERB_HANDLERS

    assert _VERB_HANDLERS["peer-reply-drain"] == (
        "swarph_cli.commands.peer_reply_drain.run_peer_reply_drain"
    )


def test_reply_drain_constructs_only_the_receipt_gated_platform_path(monkeypatch, tmp_path, capsys):
    captured = {}

    monkeypatch.setattr(command, "resolve_self_name", lambda value: value or "service-peer")
    monkeypatch.setattr(command, "resolve_token", lambda peer, path: "token")

    class Spool:
        def __init__(self, root):
            captured["spool"] = root

    class Transport:
        def __init__(self, gateway, token, self_name):
            captured["transport"] = (gateway, token, self_name)

    class Outbox:
        def __init__(self, root):
            captured["outbox"] = root

        def drain(self, spool, transport):
            captured["drain"] = (spool, transport)
            return [ReplyDrainResult("job-1", "sent")]

    monkeypatch.setattr(command, "PeerSpool", Spool)
    monkeypatch.setattr(command, "MeshGatewayReplyTransport", Transport)
    monkeypatch.setattr(command, "ReceiptGatedReplyOutbox", Outbox)
    assert command.run_peer_reply_drain([
        "--spool-dir", str(tmp_path / "spool"), "--outbox-dir", str(tmp_path / "outbox"),
        "--as", "service-peer", "--gateway", "http://gw", "--token-file", str(tmp_path / "token"),
    ]) == 0
    assert captured["transport"] == ("http://gw", "token", "service-peer")
    assert json.loads(capsys.readouterr().out) == [{"job_id": "job-1", "state": "sent"}]


def test_reply_drain_fails_when_an_envelope_is_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(command, "resolve_self_name", lambda value: "service-peer")
    monkeypatch.setattr(command, "resolve_token", lambda peer, path: "token")
    monkeypatch.setattr(command, "PeerSpool", lambda root: object())
    monkeypatch.setattr(command, "MeshGatewayReplyTransport", lambda *args: object())

    class Outbox:
        def __init__(self, root):
            pass

        def drain(self, spool, transport):
            return [ReplyDrainResult("job-1", "retained")]

    monkeypatch.setattr(command, "ReceiptGatedReplyOutbox", Outbox)
    assert command.run_peer_reply_drain([
        "--spool-dir", str(tmp_path / "spool"), "--outbox-dir", str(tmp_path / "outbox"),
        "--gateway", "http://gw", "--token-file", str(tmp_path / "token"),
    ]) == 1
