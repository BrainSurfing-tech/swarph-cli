import threading

import pytest

from swarph_cli import peer_executor
from swarph_cli.peer_executor import (
    PeerExecutorError,
    PeerService,
    PeerSpool,
    envelope_digest,
    output_digest,
)


def _job():
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "delivery_ref": "card:378",
    }


def _source_ref(fence=1):
    return {"queue_entry_id": 17, "source_dm_id": 17, "queue_claim_fence": fence}


def _receipt(claim, digest):
    return {
        "job_id": "job-1",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "fencing_token": claim["fencing_token"],
        "output_digest": digest,
        "payload_digest": output_digest("payload"),
        "envelope_digest": envelope_digest(_job()),
        "source_ref": _source_ref(),
    }


def test_peer_bound_claim_and_receipt(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")
    receipt = _receipt(claim, output["output_digest"])
    spool.accept_receipt(receipt)
    assert spool.receipt_accepted("job-1")
    assert spool.accepted_receipt("job-1") == receipt
    assert (tmp_path / "spool" / "receipts" / "job-1.json").exists()


def test_enqueue_waits_for_competing_stage_before_claim(tmp_path, monkeypatch):
    spool = PeerSpool(tmp_path / "spool")
    job = _job()
    original_write = peer_executor._write_atomic
    first_write_started = threading.Event()
    allow_first_write = threading.Event()
    second_done = threading.Event()
    write_count = 0

    def pause_first_pending_write(path, value):
        nonlocal write_count
        if path.parent == spool.pending:
            write_count += 1
            if write_count == 1:
                first_write_started.set()
                assert allow_first_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(peer_executor, "_write_atomic", pause_first_pending_write)
    first = threading.Thread(target=spool.enqueue, args=(job,))
    second = threading.Thread(target=lambda: (spool.enqueue(job), second_done.set()))
    first.start()
    assert first_write_started.wait(timeout=2)
    second.start()
    assert not second_done.wait(timeout=0.1)
    allow_first_write.set()
    first.join(timeout=2)
    second.join(timeout=2)

    claim = spool.claim("job-1", "gpt-lc")
    assert claim["fencing_token"] == 1
    assert second_done.is_set()
    assert not (spool.pending / "job-1.json").exists()
    assert (spool.running / "job-1.json").exists()


def test_wrong_peer_and_stale_receipt_are_rejected(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="another peer"):
        spool.claim("job-1", "gpt-ops")
    claim = spool.claim("job-1", "gpt-lc")
    receipt = _receipt(claim, output_digest("x")) | {
        "fencing_token": claim["fencing_token"] + 1
    }
    with pytest.raises(PeerExecutorError, match="stale"):
        spool.accept_receipt(receipt)


def test_receipt_requires_matching_durable_output(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    receipt = _receipt(claim, output_digest("done"))
    with pytest.raises(PeerExecutorError, match="output is missing"):
        spool.accept_receipt(receipt)

    spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "different")
    with pytest.raises(PeerExecutorError, match="durable output"):
        spool.accept_receipt(receipt)


def test_receipt_requires_matching_durable_envelope(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")

    with pytest.raises(PeerExecutorError, match="durable envelope"):
        spool.accept_receipt(
            _receipt(claim, output["output_digest"])
            | {"envelope_digest": output_digest("forged envelope")}
        )


def test_receipt_requires_queue_provenance(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")

    with pytest.raises(PeerExecutorError, match="source_ref"):
        spool.accept_receipt(
            _receipt(claim, output["output_digest"]) | {"source_ref": None}
        )


def test_reclaim_waits_for_receipt_acceptance(tmp_path, monkeypatch):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")
    receipt = {
        "job_id": "job-1",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "fencing_token": claim["fencing_token"],
        "output_digest": output["output_digest"],
        "payload_digest": output_digest("payload"),
        "envelope_digest": envelope_digest(_job()),
        "source_ref": _source_ref(),
    }
    original_write = peer_executor._write_atomic
    receipt_write_started = threading.Event()
    allow_receipt_write = threading.Event()
    reclaim_done = threading.Event()
    reclaim_error = []

    def pause_receipt_write(path, value):
        if path.parent == spool.receipts:
            receipt_write_started.set()
            assert allow_receipt_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(peer_executor, "_write_atomic", pause_receipt_write)
    accept_thread = threading.Thread(target=spool.accept_receipt, args=(receipt,))

    def reclaim():
        try:
            spool.reclaim("job-1", "gpt-lc", now=claim["lease_expires_at"])
        except PeerExecutorError as exc:
            reclaim_error.append(exc)
        finally:
            reclaim_done.set()

    accept_thread.start()
    assert receipt_write_started.wait(timeout=2)
    reclaim_thread = threading.Thread(target=reclaim)
    reclaim_thread.start()
    assert not reclaim_done.wait(timeout=0.1)
    allow_receipt_write.set()
    accept_thread.join(timeout=2)
    reclaim_thread.join(timeout=2)

    assert reclaim_done.is_set()
    assert reclaim_error and "accepted receipt" in str(reclaim_error[0])
    assert spool.receipt_accepted("job-1")


def test_reclaim_waits_for_output_persistence(tmp_path, monkeypatch):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    original_write = peer_executor._write_atomic
    output_write_started = threading.Event()
    allow_output_write = threading.Event()
    reclaim_done = threading.Event()
    reclaim_result = []

    def pause_output_write(path, value):
        if path.parent == spool.outputs:
            output_write_started.set()
            assert allow_output_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(peer_executor, "_write_atomic", pause_output_write)
    output_thread = threading.Thread(
        target=spool.write_output,
        args=("job-1", "gpt-lc", claim["fencing_token"], "done"),
    )

    def reclaim():
        reclaim_result.append(
            spool.reclaim("job-1", "gpt-lc", now=claim["lease_expires_at"])
        )
        reclaim_done.set()

    output_thread.start()
    assert output_write_started.wait(timeout=2)
    reclaim_thread = threading.Thread(target=reclaim)
    reclaim_thread.start()
    assert not reclaim_done.wait(timeout=0.1)
    allow_output_write.set()
    output_thread.join(timeout=2)
    reclaim_thread.join(timeout=2)

    assert reclaim_done.is_set()
    assert reclaim_result[0]["fencing_token"] == claim["fencing_token"] + 1
    assert (spool.outputs / "job-1.1.json").exists()


def test_expired_claim_can_be_reclaimed_with_a_higher_fencing_token(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    first = spool.claim("job-1", "gpt-lc")
    with pytest.raises(PeerExecutorError, match="not expired"):
        spool.reclaim("job-1", "gpt-lc", now=first["lease_expires_at"] - 1)
    reclaimed = spool.reclaim("job-1", "gpt-lc", now=first["lease_expires_at"])
    assert reclaimed["fencing_token"] == first["fencing_token"] + 1
    with pytest.raises(PeerExecutorError, match="stale or wrong-peer output"):
        spool.write_output("job-1", "gpt-lc", first["fencing_token"], "old worker")
    fresh = spool.write_output(
        "job-1", "gpt-lc", reclaimed["fencing_token"], "new worker"
    )
    assert fresh["fencing_token"] == reclaimed["fencing_token"]


def test_accepted_job_cannot_be_reclaimed(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")
    spool.accept_receipt(_receipt(claim, output["output_digest"]))
    with pytest.raises(PeerExecutorError, match="accepted receipt"):
        spool.reclaim("job-1", "gpt-lc", now=claim["lease_expires_at"])


def test_job_envelope_rejects_raw_dm_content_and_unknown_schema(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    job = _job() | {"content": "raw DM text must never reach the spool"}
    with pytest.raises(PeerExecutorError, match="undeclared"):
        spool.enqueue(job)
    with pytest.raises(PeerExecutorError, match="schema_version"):
        spool.enqueue(_job() | {"schema_version": 2})


def test_peer_service_authorizes_each_service_operation(tmp_path):
    class Authorizer:
        def __init__(self):
            self.calls = []

        def require_service(self, peer, spool_root):
            self.calls.append((peer, spool_root))

    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    authorizer = Authorizer()
    service = PeerService(spool, "gpt-lc", authorizer)
    claim = service.claim("job-1")
    service.write_output("job-1", claim["fencing_token"], "done")
    assert [peer for peer, _ in authorizer.calls] == ["gpt-lc", "gpt-lc"]


def test_peer_service_produces_a_queue_bound_receipt_idempotently(tmp_path):
    class Authorizer:
        def __init__(self):
            self.calls = []

        def require_service(self, peer, spool_root):
            self.calls.append((peer, spool_root))

    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    authorizer = Authorizer()
    service = PeerService(spool, "gpt-lc", authorizer)
    claim = service.claim("job-1")
    source_ref = _source_ref(fence=9)

    receipt = service.produce_receipt(
        "job-1",
        claim["fencing_token"],
        "done",
        source_ref,
        output_digest("payload"),
        envelope_digest(_job()),
    )

    assert receipt == _receipt(claim, output_digest("done")) | {
        "source_ref": source_ref
    }
    assert (
        service.produce_receipt(
            "job-1",
            claim["fencing_token"],
            "done",
            source_ref,
            output_digest("payload"),
            envelope_digest(_job()),
        )
        == receipt
    )
    assert spool.accepted_receipt("job-1") == receipt
    assert [peer for peer, _ in authorizer.calls] == ["gpt-lc", "gpt-lc", "gpt-lc"]


def test_receipt_producer_rejects_forged_or_wrong_source_ref(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")

    with pytest.raises(PeerExecutorError, match="source DM"):
        spool.produce_receipt(
            "job-1",
            "gpt-lc",
            claim["fencing_token"],
            "done",
            {"queue_entry_id": 18, "source_dm_id": 18, "queue_claim_fence": 1},
            output_digest("payload"),
            envelope_digest(_job()),
        )
    assert not (spool.outputs / "job-1.1.json").exists()
    assert not (spool.receipts / "job-1.json").exists()


def test_receipt_producer_recovers_after_output_before_receipt(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")

    receipt = spool.produce_receipt(
        "job-1",
        "gpt-lc",
        claim["fencing_token"],
        "done",
        _source_ref(fence=4),
        output_digest("payload"),
        envelope_digest(_job()),
    )

    assert receipt["output_digest"] == output["output_digest"]
    assert receipt["source_ref"]["queue_claim_fence"] == 4
    assert spool.accepted_receipt("job-1") == receipt


def test_peer_service_fails_closed_when_platform_rejects_identity(tmp_path):
    class Deny:
        def require_service(self, peer, spool_root):
            raise PeerExecutorError("service identity does not match manifest")

    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="does not match"):
        PeerService(spool, "gpt-lc", Deny()).claim("job-1")
