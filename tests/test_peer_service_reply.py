import hashlib
import json

from swarph_cli.peer_executor import PeerSpool, envelope_digest, output_digest
from swarph_cli.peer_service_reply import ReceiptGatedReplyOutbox


def _source_ref():
    return {
        "queue_entry_id": 17,
        "source_dm_id": 17,
        "queue_claim_fence": 1,
        "source_peer": "gpt-ops",
    }


def _accepted_receipt(tmp_path, text="reply body"):
    spool = PeerSpool(tmp_path / "spool")
    job = {
        "schema_version": 1,
        "job_id": "dm-17",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "delivery_ref": json.dumps(_source_ref(), sort_keys=True, separators=(",", ":")),
    }
    spool.enqueue(job)
    claim = spool.claim("dm-17", "gpt-lc")
    receipt = spool.produce_receipt(
        "dm-17",
        "gpt-lc",
        claim["fencing_token"],
        text,
        _source_ref(),
        output_digest("request body"),
        envelope_digest(job),
    )
    return spool, receipt


def _write_envelope(outbox, receipt, content, **extra):
    envelope = {
        "schema_version": 1,
        "job_id": receipt["job_id"],
        "receipt_digest": hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "content": content,
    } | extra
    outbox.pending.mkdir(parents=True, exist_ok=True)
    (outbox.pending / f"{receipt['job_id']}.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )
    return envelope


class _Transport:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.delivered = {}

    def send(self, destination, content, *, idempotency_key):
        self.calls.append((destination, content, idempotency_key))
        if self.fail:
            raise OSError("gateway unavailable")
        self.delivered.setdefault(idempotency_key, (destination, content))


def test_consumer_uses_accepted_receipt_source_peer_not_model_destination(tmp_path):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "reply body")
    transport = _Transport()

    assert outbox.drain(spool, transport)[0].state == "sent"
    assert transport.calls[0][:2] == ("gpt-ops", "reply body")
    assert not list(outbox.pending.glob("*.json"))
    assert list(outbox.sent.glob("*.json"))


def test_consumer_rejects_model_asserted_destination_and_keeps_entry(tmp_path):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "reply body", to_node="other-peer")
    transport = _Transport()

    assert outbox.drain(spool, transport)[0].state == "retained"
    assert transport.calls == []
    assert (outbox.pending / "dm-17.json").exists()


def test_consumer_retains_on_transport_failure(tmp_path):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "reply body")

    assert outbox.drain(spool, _Transport(fail=True))[0].state == "retained"
    assert (outbox.pending / "dm-17.json").exists()
    assert not list(outbox.sent.glob("*.json"))


def test_consumer_rejects_wrong_receipt_or_output_binding(tmp_path):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "different reply")
    transport = _Transport()

    assert outbox.drain(spool, transport)[0].state == "retained"
    assert transport.calls == []
    assert (outbox.pending / "dm-17.json").exists()


def test_consumer_recovers_after_send_before_local_evidence(tmp_path, monkeypatch):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "reply body")
    transport = _Transport()

    import swarph_cli.peer_service_reply as reply

    original_write = reply._write_atomic
    failed = False

    def crash_before_evidence(path, value):
        nonlocal failed
        if path.parent == outbox.sent and not failed:
            failed = True
            raise OSError("simulated crash before local commit")
        original_write(path, value)

    monkeypatch.setattr(reply, "_write_atomic", crash_before_evidence)
    assert outbox.drain(spool, transport)[0].state == "retained"
    assert (outbox.pending / "dm-17.json").exists()

    monkeypatch.setattr(reply, "_write_atomic", original_write)
    assert outbox.drain(spool, transport)[0].state == "sent"
    assert len(transport.calls) == 2
    assert len(transport.delivered) == 1


def test_consumer_does_not_repeat_a_committed_send_after_restart(tmp_path):
    spool, receipt = _accepted_receipt(tmp_path)
    outbox = ReceiptGatedReplyOutbox(tmp_path / "outbox")
    _write_envelope(outbox, receipt, "reply body")
    transport = _Transport()

    assert outbox.drain(spool, transport)[0].state == "sent"
    _write_envelope(outbox, receipt, "reply body")
    restarted = ReceiptGatedReplyOutbox(outbox.root)
    assert restarted.drain(spool, transport)[0].state == "already-sent"
    assert len(transport.calls) == 1
