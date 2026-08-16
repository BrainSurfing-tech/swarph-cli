import json

import pytest

from swarph_cli.peer_executor import PeerExecutorError, PeerService, PeerSpool, receipt_digest
from swarph_cli.peer_reply_outbox import PeerReplyOutbox
from swarph_cli.peer_service_host import PeerPayload, PeerServiceHost


class _Authorizer:
    def require_service(self, peer, spool_root):
        assert peer == "gpt-lc"


class _Provider:
    def get_payload(self, request):
        return PeerPayload(
            request.peer,
            request.source_dm_id,
            request.queue_claim_fence,
            "request body",
            request.source_peer,
        )


class _Handler:
    def handle(self, request, payload):
        assert payload == "request body"
        return "service response"


def _job(source_peer="gpt-ops"):
    return {
        "schema_version": 1,
        "job_id": "dm-17",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "delivery_ref": json.dumps({
            "queue_entry_id": 17,
            "source_dm_id": 17,
            "queue_claim_fence": 1,
            "source_peer": source_peer,
        }, sort_keys=True, separators=(",", ":")),
    }


def _completed_spool(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    host = PeerServiceHost(PeerService(spool, "gpt-lc", _Authorizer()), _Provider(), _Handler())
    host.execute("dm-17")
    return spool


def test_stage_binds_content_to_the_accepted_receipt_without_emitting_a_target(tmp_path):
    spool = _completed_spool(tmp_path)
    outbox = PeerReplyOutbox(tmp_path / "outbox")

    envelope = outbox.stage(spool, "dm-17")

    assert envelope["content"] == "service response"
    assert set(envelope) == {"schema_version", "job_id", "receipt_digest", "content"}
    assert envelope["receipt_digest"] == receipt_digest(spool.accepted_receipt("dm-17"))
    assert outbox.stage(spool, "dm-17") == envelope


def test_stage_refuses_unaccepted_or_legacy_unroutable_results(tmp_path):
    spool = PeerSpool(tmp_path / "unaccepted")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="accepted receipt"):
        PeerReplyOutbox(tmp_path / "outbox").stage(spool, "dm-17")

    legacy = PeerSpool(tmp_path / "legacy")
    job = _job()
    job["delivery_ref"] = json.dumps({
        "queue_entry_id": 17, "source_dm_id": 17, "queue_claim_fence": 1,
    }, sort_keys=True, separators=(",", ":"))
    legacy.enqueue(job)
    service = PeerService(legacy, "gpt-lc", _Authorizer())
    claim = service.claim("dm-17")
    service.produce_receipt(
        "dm-17", claim["fencing_token"], "response", json.loads(job["delivery_ref"]),
        "a" * 64, __import__("swarph_cli.peer_executor", fromlist=["envelope_digest"]).envelope_digest(job),
    )
    with pytest.raises(PeerExecutorError, match="routable source peer"):
        PeerReplyOutbox(tmp_path / "outbox").stage(legacy, "dm-17")


def test_stage_rejects_a_conflicting_preexisting_envelope(tmp_path):
    spool = _completed_spool(tmp_path)
    outbox = PeerReplyOutbox(tmp_path / "outbox")
    outbox.root.mkdir()
    (outbox.root / "dm-17.json").write_text('{"forged":true}\n', encoding="utf-8")

    with pytest.raises(PeerExecutorError, match="conflicts"):
        outbox.stage(spool, "dm-17")


def test_stage_refuses_whitespace_only_accepted_output(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    service = PeerService(spool, "gpt-lc", _Authorizer())
    claim = service.claim("dm-17")
    source_ref = json.loads(_job()["delivery_ref"])
    from swarph_cli.peer_executor import envelope_digest, output_digest

    service.produce_receipt(
        "dm-17", claim["fencing_token"], " \t\n", source_ref,
        output_digest("request body"), envelope_digest(_job()),
    )

    with pytest.raises(PeerExecutorError, match="blank"):
        PeerReplyOutbox(tmp_path / "outbox").stage(spool, "dm-17")
