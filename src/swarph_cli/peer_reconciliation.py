"""Receipt-gated handoff from a peer spool back to the owed-work queue."""
from __future__ import annotations

from swarph_cli.delivery_queue import DeliveryQueue
from swarph_cli.peer_executor import PeerSpool


class PeerReceiptReconciler:
    """A queue-owned acknowledgement of a validated peer-spool receipt."""

    def __init__(self, queue: DeliveryQueue, spool: PeerSpool):
        self.queue = queue
        self.spool = spool

    def reconcile(self, job_id: str) -> bool:
        receipt = self.spool.accepted_receipt(job_id)
        if receipt is None:
            return False
        return self.queue.reconcile_spool_receipt(receipt)
