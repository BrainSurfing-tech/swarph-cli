"""Durable, receipt-gated ledger for drained mesh DMs.

The queue is the single source of truth for work owed to a peer. A source DM
is retained until a receipt binds one claimed service job, its source DM,
destination peer, fencing token, and output digest. It deliberately contains
no terminal or App Server integration.
"""
from __future__ import annotations

import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import List

from swarph_cli.console_safe import print_safe


class DeliveryQueueError(RuntimeError):
    """A receipt or state transition does not match queued work."""


_ELIGIBILITY = {"eligible", "ineligible", "cannot_evaluate"}
_SERVICE_STATES = {"unassigned", "claimed", "capacity_refused"}
_OBLIGATION_STATES = {
    "owed",
    "ineligible",
    "cannot_evaluate",
    "capacity_refused",
    "expired",
}


def wake_for(kind: str, thread_id) -> bool:
    """Whether a DM is actionable enough to wake a future service."""
    if kind in ("question", "unblock"):
        return True
    return kind == "answer" and thread_id is not None


class DeliveryQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending: List[dict] = []
        self._receipts: List[dict] = []
        self.deferred_ticks = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("queue file is not a JSON object")
            pending = data.get("pending", [])
            receipts = data.get("receipts", [])
            if not isinstance(pending, list) or not isinstance(receipts, list):
                raise ValueError("queue pending and receipts must be lists")
            self._pending = [self._migrate_entry(entry) for entry in pending]
            self._receipts = [dict(receipt) for receipt in receipts]
            self.deferred_ticks = int(data.get("deferred_ticks", 0))
        except FileNotFoundError:
            self._pending = []
            self._receipts = []
            self.deferred_ticks = 0
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            print_safe(
                f"[swarph-daemon] delivery queue unreadable at {self.path} "
                f"({type(exc).__name__}: {exc}); starting empty - any queued "
                "DMs survive only in inbox.log",
                file=sys.stderr,
                flush=True,
            )
            self._pending = []
            self._receipts = []
            self.deferred_ticks = 0

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(
                {
                    "pending": self._pending,
                    "receipts": self._receipts,
                    "deferred_ticks": self.deferred_ticks,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def enqueue(self, dm: dict) -> None:
        mid = dm["id"]
        if any(entry["id"] == mid for entry in self._pending):
            return
        kind = dm.get("kind", "")
        thread_id = dm.get("thread_id")
        self._pending.append(
            {
                "id": mid,
                "from": dm.get("from_node"),
                "kind": kind,
                "thread_id": thread_id,
                "content": dm.get("content", ""),
                "wake": wake_for(kind, thread_id),
                "queued_at": time.time(),
                "eligibility": "cannot_evaluate",
                "eligibility_reason": "awaiting policy evaluation",
                "obligation_state": "cannot_evaluate",
                "service_state": "unassigned",
                "job": None,
                "source_read_state": "unread",
            }
        )
        self._persist()

    def pending(self) -> List[dict]:
        return deepcopy(self._pending)

    def any_wake(self) -> bool:
        return any(entry.get("wake") for entry in self._pending)

    def record_eligibility(self, dm_id: int, state: str, reason: str) -> None:
        """Record a policy decision without dropping the owed source DM."""
        if state not in _ELIGIBILITY:
            raise DeliveryQueueError(f"unsupported eligibility state: {state}")
        if not isinstance(reason, str) or not reason:
            raise DeliveryQueueError("eligibility reason is required")
        entry = self._entry(dm_id)
        entry["eligibility"] = state
        entry["eligibility_reason"] = reason
        entry["obligation_state"] = "owed" if state == "eligible" else state
        self._persist()

    def record_expired(self, dm_id: int, reason: str) -> None:
        """Record expiry explicitly; expiry never silently becomes completion."""
        if not isinstance(reason, str) or not reason:
            raise DeliveryQueueError("expiry reason is required")
        entry = self._entry(dm_id)
        if entry["service_state"] == "claimed":
            raise DeliveryQueueError("an active service claim cannot be expired")
        entry["obligation_state"] = "expired"
        entry["expiry_reason"] = reason
        self._persist()

    def claim_for_service(
        self, dm_id: int, destination_peer: str, *, max_active: int
    ) -> dict | None:
        """Reserve eligible work for a bounded service without exposing content."""
        if not isinstance(max_active, int) or isinstance(max_active, bool) or max_active < 1:
            raise DeliveryQueueError("max_active must be a positive integer")
        if not isinstance(destination_peer, str) or not destination_peer:
            raise DeliveryQueueError("destination_peer is required")
        entry = self._entry(dm_id)
        if entry["eligibility"] != "eligible" or entry["obligation_state"] not in {
            "owed",
            "capacity_refused",
        }:
            raise DeliveryQueueError("only eligible work may be claimed")
        if entry["service_state"] == "claimed":
            job = entry.get("job")
            if isinstance(job, dict) and job.get("destination_peer") == destination_peer:
                return deepcopy(job)
            raise DeliveryQueueError("source DM already has an active service claim")

        active = sum(entry.get("service_state") == "claimed" for entry in self._pending)
        if active >= max_active:
            entry["service_state"] = "capacity_refused"
            entry["obligation_state"] = "capacity_refused"
            entry["capacity_refusal"] = (
                f"service capacity exhausted: {active}/{max_active} active"
            )
            self._persist()
            return None

        prior = entry.get("job") or {}
        token = int(prior.get("fencing_token", 0)) + 1
        job = {
            "job_id": f"dm-{entry['id']}",
            "source_dm_id": entry["id"],
            "destination_peer": destination_peer,
            "fencing_token": token,
            "reply_provenance": {
                "peer": destination_peer,
                "actor": "service",
                "actor_id": f"{destination_peer}/service",
            },
        }
        entry["job"] = job
        entry["service_state"] = "claimed"
        entry["obligation_state"] = "owed"
        entry.pop("capacity_refusal", None)
        self._persist()
        return deepcopy(job)

    def remove_on_receipt(self, receipt: dict) -> None:
        """Remove one owed DM only after its exact service receipt is recorded."""
        required = {
            "job_id",
            "source_dm_id",
            "destination_peer",
            "fencing_token",
            "output_digest",
            "reply_provenance",
        }
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise DeliveryQueueError("receipt must contain the complete receipt contract")
        if not isinstance(receipt["source_dm_id"], int) or isinstance(receipt["source_dm_id"], bool):
            raise DeliveryQueueError("receipt source_dm_id must be an integer")
        if not isinstance(receipt["fencing_token"], int) or receipt["fencing_token"] < 1:
            raise DeliveryQueueError("receipt fencing_token must be a positive integer")
        if not isinstance(receipt["output_digest"], str) or len(receipt["output_digest"]) != 64:
            raise DeliveryQueueError("receipt output_digest must be a SHA-256 digest")
        try:
            int(receipt["output_digest"], 16)
        except ValueError as exc:
            raise DeliveryQueueError("receipt output_digest must be a SHA-256 digest") from exc

        entry = self._entry(receipt["source_dm_id"])
        job = entry.get("job")
        if entry.get("service_state") != "claimed" or not isinstance(job, dict):
            raise DeliveryQueueError("source DM has no active service claim")
        for field in (
            "job_id",
            "source_dm_id",
            "destination_peer",
            "fencing_token",
            "reply_provenance",
        ):
            if receipt[field] != job.get(field):
                raise DeliveryQueueError("receipt does not match the active service claim")

        self._receipts.append(dict(receipt))
        self._pending.remove(entry)
        self._persist()

    def reconcile_spool_receipt(self, receipt: dict) -> bool:
        """Acknowledge a validated spool handoff without trusting a bare ID.

        The caller must obtain ``receipt`` through ``PeerSpool.accepted_receipt``.
        ``source_ref`` binds that durable receipt to this queue claim; removing
        the queue item is then a retryable acknowledgement of the handoff.
        """
        required = {
            "job_id", "source_dm_id", "destination_peer", "fencing_token",
            "output_digest", "source_ref",
        }
        if not isinstance(receipt, dict) or not required.issubset(receipt):
            raise DeliveryQueueError("spool receipt lacks the reconciliation contract")
        source_ref = receipt["source_ref"]
        if not isinstance(source_ref, dict) or set(source_ref) != {
            "queue_entry_id", "source_dm_id", "queue_claim_fence",
        }:
            raise DeliveryQueueError("spool receipt source_ref is invalid")
        for key in ("queue_entry_id", "source_dm_id", "queue_claim_fence"):
            if not isinstance(source_ref[key], int) or isinstance(source_ref[key], bool):
                raise DeliveryQueueError("spool receipt source_ref must use integer identifiers")
        if source_ref["queue_entry_id"] != source_ref["source_dm_id"] or (
            receipt["source_dm_id"] != source_ref["source_dm_id"]
        ):
            raise DeliveryQueueError("spool receipt source_ref does not identify its source DM")
        if not isinstance(receipt["fencing_token"], int) or receipt["fencing_token"] < 1:
            raise DeliveryQueueError("spool receipt fencing_token must be a positive integer")
        if not isinstance(receipt["output_digest"], str) or len(receipt["output_digest"]) != 64:
            raise DeliveryQueueError("spool receipt output_digest must be a SHA-256 digest")
        try:
            int(receipt["output_digest"], 16)
        except ValueError as exc:
            raise DeliveryQueueError("spool receipt output_digest must be a SHA-256 digest") from exc

        try:
            entry = self._entry(source_ref["queue_entry_id"])
        except DeliveryQueueError:
            if any(
                accepted.get("source_ref") == source_ref
                and accepted.get("output_digest") == receipt["output_digest"]
                for accepted in self._receipts
            ):
                return False
            raise
        job = entry.get("job")
        if entry.get("service_state") != "claimed" or not isinstance(job, dict):
            raise DeliveryQueueError("source DM has no active service claim")
        if (
            job.get("job_id"), job.get("source_dm_id"), job.get("destination_peer"),
            job.get("fencing_token"),
        ) != (
            receipt["job_id"], receipt["source_dm_id"], receipt["destination_peer"],
            source_ref["queue_claim_fence"],
        ):
            raise DeliveryQueueError("spool receipt does not match the active queue claim")
        self._receipts.append(dict(receipt))
        self._pending.remove(entry)
        self._persist()
        return True

    def status(self, *, now: float | None = None) -> dict:
        """Return operator-ready owed-work counts and oldest age."""
        observed_at = time.time() if now is None else now
        ages = [
            max(0.0, observed_at - float(entry.get("queued_at", observed_at)))
            for entry in self._pending
        ]
        return {
            "owed": len(self._pending),
            "oldest_age_seconds": max(ages, default=0.0),
            "eligibility": {
                state: sum(entry.get("eligibility") == state for entry in self._pending)
                for state in sorted(_ELIGIBILITY)
            },
            "service_state": {
                state: sum(entry.get("service_state") == state for entry in self._pending)
                for state in sorted(_SERVICE_STATES)
            },
            "obligation_state": {
                state: sum(entry.get("obligation_state") == state for entry in self._pending)
                for state in sorted(_OBLIGATION_STATES)
            },
        }

    def accepted_receipts(self) -> List[dict]:
        return deepcopy(self._receipts)

    def remove(self, ids: set) -> None:
        """Refuse the legacy ID-only removal API."""
        raise DeliveryQueueError("ID-only removal is forbidden; use remove_on_receipt")

    def bump_deferred(self) -> int:
        self.deferred_ticks += 1
        self._persist()
        return self.deferred_ticks

    def reset_deferred(self) -> None:
        if self.deferred_ticks != 0:
            self.deferred_ticks = 0
            self._persist()

    def _entry(self, dm_id: int) -> dict:
        for entry in self._pending:
            if entry.get("id") == dm_id:
                return entry
        raise DeliveryQueueError(f"unknown queued source DM: {dm_id}")

    @staticmethod
    def _migrate_entry(entry: object) -> dict:
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError("queue entry must be an object with an id")
        migrated = dict(entry)
        migrated.setdefault("queued_at", time.time())
        migrated.setdefault("eligibility", "cannot_evaluate")
        migrated.setdefault("eligibility_reason", "awaiting policy evaluation")
        migrated.setdefault(
            "obligation_state",
            "owed" if migrated["eligibility"] == "eligible" else migrated["eligibility"],
        )
        migrated.setdefault("service_state", "unassigned")
        migrated.setdefault("job", None)
        migrated.setdefault("source_read_state", "unread")
        if migrated["eligibility"] not in _ELIGIBILITY:
            raise ValueError("queue entry has an unsupported eligibility state")
        if migrated["service_state"] not in _SERVICE_STATES:
            raise ValueError("queue entry has an unsupported service state")
        if migrated["obligation_state"] not in _OBLIGATION_STATES:
            raise ValueError("queue entry has an unsupported obligation state")
        return migrated
