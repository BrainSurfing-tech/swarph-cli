import threading

import pytest

import swarph_cli.peer_executor as peer_executor
from swarph_cli.peer_executor import PeerExecutorError, PeerService, PeerSpool, output_digest


def _job():
    return {"schema_version": 1, "job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "delivery_ref": "card:378"}


def test_peer_bound_claim_and_receipt(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")
    receipt = {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": output["output_digest"]}
    spool.accept_receipt(receipt)
    assert spool.receipt_accepted("job-1")
    assert (tmp_path / "spool" / "receipts" / "job-1.json").exists()


def test_wrong_peer_and_stale_receipt_are_rejected(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="another peer"):
        spool.claim("job-1", "gpt-ops")
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"] - 1, "output_digest": output_digest("x")}
    with pytest.raises(PeerExecutorError, match="stale"):
        spool.accept_receipt(receipt)


def test_receipt_requires_matching_durable_output(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": output_digest("done")}
    with pytest.raises(PeerExecutorError, match="output is missing"):
        spool.accept_receipt(receipt)

    spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "different")
    with pytest.raises(PeerExecutorError, match="durable output"):
        spool.accept_receipt(receipt)


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
    fresh = spool.write_output("job-1", "gpt-lc", reclaimed["fencing_token"], "new worker")
    assert fresh["fencing_token"] == reclaimed["fencing_token"]


def test_accepted_job_cannot_be_reclaimed(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    output = spool.write_output("job-1", "gpt-lc", claim["fencing_token"], "done")
    spool.accept_receipt({"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": output["output_digest"]})
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


def test_peer_service_fails_closed_when_platform_rejects_identity(tmp_path):
    class Deny:
        def require_service(self, peer, spool_root):
            raise PeerExecutorError("service identity does not match manifest")

    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="does not match"):
        PeerService(spool, "gpt-lc", Deny()).claim("job-1")
