import pytest

from swarph_cli.peer_executor import PeerExecutorError, PeerSpool, output_digest
from swarph_cli.commands import mesh


def _job():
    return {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "delivery_ref": "card:378"}


def test_peer_bound_claim_and_receipt(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": output_digest("done")}
    spool.accept_receipt(receipt)
    assert spool.receipt_accepted("job-1")
    assert (tmp_path / "spool" / "receipts" / "job-1.json").exists()


def test_wrong_peer_and_stale_receipt_are_rejected(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    with pytest.raises(PeerExecutorError, match="another peer"):
        spool.claim("job-1", "gpt-ops")
    claim = spool.claim("job-1", "gpt-lc")
    receipt = {"job_id": "job-1", "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"] - 1, "output_digest": "x"}
    with pytest.raises(PeerExecutorError, match="stale"):
        spool.accept_receipt(receipt)


def test_monitor_peer_service_waits_for_receipt(monkeypatch, tmp_path):
    dm = {"id": 17, "from_node": "lab", "kind": "question", "content": "review"}
    monkeypatch.setattr(mesh, "_http_get_json", lambda *a, **k: (200, {"messages": [dm]}))
    sink = mesh.parse_sink(f"peer-service:gpt-lc|{tmp_path / 'spool'}")
    state = mesh.MonitorState(self_name="gpt-lc", state_dir=tmp_path / "state", gateway="http://gw", token="x", sinks=[sink], min_interval_s=0)
    mesh._monitor_iteration(state)
    assert state.ledger(sink.name)["last_delivered_id"] == 0
    job_id = "mesh-gpt-lc-17"
    claim = sink.spool.claim(job_id, "gpt-lc")
    sink.spool.accept_receipt({"job_id": job_id, "source_dm_id": 17, "destination_peer": "gpt-lc", "fencing_token": claim["fencing_token"], "output_digest": "x"})
    monkeypatch.setattr(mesh, "_http_get_json", lambda *a, **k: (200, {"messages": []}))
    mesh._monitor_iteration(state)
    assert state.ledger(sink.name)["last_delivered_id"] == 17
