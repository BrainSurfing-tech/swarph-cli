"""End-to-end gate for the queue-owned and peer-owned delivery handoff."""

import json

from swarph_cli.delivery_queue import DeliveryQueue
from swarph_cli.peer_executor import PeerService, PeerSpool
from swarph_cli.peer_reconciliation import PeerReceiptReconciler
from swarph_cli.peer_service_host import PeerServiceHost
from swarph_cli.peer_service_payload import InboxLogPeerPayloadProvider
from swarph_cli.peer_staging import PeerSpoolStager


class _Authorizer:
    def require_service(self, peer, spool_root):
        assert peer == "gpt-lc"


class _Handler:
    def __init__(self):
        self.calls = []

    def handle(self, request, payload):
        self.calls.append((request, payload))
        return "handled by the peer-owned service"


def test_queue_to_host_receipt_reconciles_after_restart_without_spooling_payload(tmp_path):
    queue_path = tmp_path / "queue.json"
    queue = DeliveryQueue(queue_path)
    queue.enqueue({"id": 17, "from_node": "gpt-ops", "kind": "question", "content": "secret request body"})
    queue.record_eligibility(17, "eligible", "service policy permits it")

    spool_root = tmp_path / "private-spool"
    spool = PeerSpool(spool_root)
    envelope = PeerSpoolStager(queue, spool).stage(17, "gpt-lc", max_active=1)
    assert "content" not in envelope

    inbox_log = tmp_path / "monitor" / "inbox.log"
    inbox_log.parent.mkdir()
    inbox_log.write_text(json.dumps({
        "id": 17,
        "to_node": "gpt-lc",
        "from_node": "gpt-ops",
        "kind": "question",
        "content": "secret request body",
    }) + "\n", encoding="utf-8")
    handler = _Handler()
    host = PeerServiceHost(
        PeerService(spool, "gpt-lc", _Authorizer()),
        InboxLogPeerPayloadProvider(inbox_log, "gpt-lc"),
        handler,
    )

    receipt = host.execute(envelope["job_id"])

    assert handler.calls[0][0].source_dm_id == 17
    assert handler.calls[0][1] == "secret request body"
    assert receipt["source_ref"] == {
        "queue_entry_id": 17,
        "source_dm_id": 17,
        "queue_claim_fence": 1,
    }
    assert "secret request body" not in "\n".join(
        path.read_text(encoding="utf-8") for path in spool_root.rglob("*.json")
    )

    # The durable peer receipt exists before queue acknowledgement.  A restart
    # must reconcile it exactly once without rerunning the handler.
    restarted_queue = DeliveryQueue(queue_path)
    restarted_spool = PeerSpool(spool_root)
    reconciler = PeerReceiptReconciler(restarted_queue, restarted_spool)
    assert reconciler.reconcile(envelope["job_id"]) is True
    assert reconciler.reconcile(envelope["job_id"]) is False
    assert restarted_queue.status()["owed"] == 0
