"""Idempotent staging of queue-owned work into a peer-owned spool."""
from __future__ import annotations

import json

from swarph_cli.delivery_queue import DeliveryQueue
from swarph_cli.peer_executor import PeerSpool


class PeerSpoolStager:
    """Stage one eligible queue claim without exposing its message body."""

    def __init__(self, queue: DeliveryQueue, spool: PeerSpool):
        self.queue = queue
        self.spool = spool

    def stage(self, dm_id: int, destination_peer: str, *, max_active: int) -> dict | None:
        claim = self.queue.claim_for_service(dm_id, destination_peer, max_active=max_active)
        if claim is None:
            return None
        source_ref = {
            "queue_entry_id": claim["source_dm_id"],
            "source_dm_id": claim["source_dm_id"],
            "queue_claim_fence": claim["fencing_token"],
        }
        envelope = {
            "schema_version": 1,
            "job_id": claim["job_id"],
            "source_dm_id": claim["source_dm_id"],
            "destination_peer": claim["destination_peer"],
            "delivery_ref": json.dumps(source_ref, sort_keys=True, separators=(",", ":")),
        }
        self.spool.enqueue(envelope)
        return envelope
