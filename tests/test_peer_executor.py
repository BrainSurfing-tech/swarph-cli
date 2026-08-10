import pytest

from swarph_cli.peer_executor import PeerExecutorError, PeerSpool, output_digest


def _job():
    return {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "delivery_ref": "card:378"}


def test_peer_bound_claim_and_receipt(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": output_digest("done")}
    spool.accept_receipt(receipt)
    assert (tmp_path / "spool" / "receipts" / "job-1.json").exists()


def test_wrong_peer_and_stale_receipt_are_rejected(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="another peer"):
        spool.claim("job-1", "gpt-ops")
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"] - 1, "output_digest": "x"}
    with pytest.raises(PeerExecutorError, match="stale"):
        spool.accept_receipt(receipt)
