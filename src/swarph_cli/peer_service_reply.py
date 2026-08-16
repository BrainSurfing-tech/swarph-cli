"""Receipt-gated platform delivery for peer-service replies.

This consumer deliberately has no queue or model authority.  A host-side
producer writes a compact reply envelope, while this module proves it against
the accepted spool receipt before delivering to the receipt-derived source
peer.  The transport must deduplicate its deterministic idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from swarph_cli.peer_executor import PeerExecutorError, PeerSpool, output_digest


class PeerReplyError(RuntimeError):
    """A reply envelope or its durable delivery evidence is invalid."""


class IdempotentReplyTransport(Protocol):
    """Platform transport that must deduplicate equal idempotency keys."""

    def send(self, destination: str, content: str, *, idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class ReplyDrainResult:
    job_id: str
    state: str


_ENVELOPE_FIELDS = {"schema_version", "job_id", "receipt_digest", "content"}


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeerReplyError(f"invalid JSON at {path}") from exc
    if not isinstance(value, dict):
        raise PeerReplyError(f"object required at {path}")
    return value


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(_canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _exclusive_file_lock(path: Path):
    """Take a short-lived, crash-released lock on Windows or POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()


def _validate_envelope(envelope: object) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise PeerReplyError("reply envelope must contain the complete host contract")
    if envelope.get("schema_version") != 1:
        raise PeerReplyError("unsupported reply envelope schema_version")
    for field in ("job_id", "receipt_digest"):
        value = envelope.get(field)
        if not isinstance(value, str) or not value:
            raise PeerReplyError(f"reply envelope {field} must be non-empty text")
    if len(envelope["receipt_digest"]) != 64:
        raise PeerReplyError("reply envelope receipt_digest must be a SHA-256 digest")
    try:
        int(envelope["receipt_digest"], 16)
    except ValueError as exc:
        raise PeerReplyError("reply envelope receipt_digest must be a SHA-256 digest") from exc
    if not isinstance(envelope.get("content"), str) or not envelope["content"].strip():
        raise PeerReplyError("reply envelope content must be non-empty text")
    return envelope


class ReceiptGatedReplyOutbox:
    """Deliver only receipt-bound replies and retain unsent envelopes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.sent = self.root / "sent"
        self.locks = self.root / "locks"

    def drain(
        self, spool: PeerSpool, transport: IdempotentReplyTransport
    ) -> list[ReplyDrainResult]:
        self.pending.mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(self.pending.glob("*.json")):
            try:
                results.append(self._drain_one(path, spool, transport))
            except (PeerReplyError, PeerExecutorError, OSError):
                results.append(ReplyDrainResult(path.stem, "retained"))
        return results

    def _drain_one(
        self, path: Path, spool: PeerSpool, transport: IdempotentReplyTransport
    ) -> ReplyDrainResult:
        envelope = _validate_envelope(_read_object(path))
        if path.stem != envelope["job_id"]:
            raise PeerReplyError("reply envelope filename must match job_id")
        envelope_digest = _digest(envelope)
        with _exclusive_file_lock(self.locks / f"{envelope_digest}.lock"):
            if not path.exists():
                return ReplyDrainResult(envelope["job_id"], "already-drained")
            evidence_path = self.sent / f"{envelope_digest}.json"
            if evidence_path.exists():
                evidence = _read_object(evidence_path)
                if evidence != self._evidence(envelope, envelope_digest):
                    raise PeerReplyError("existing delivery evidence does not match reply")
                path.unlink()
                return ReplyDrainResult(envelope["job_id"], "already-sent")

            receipt = spool.accepted_receipt(envelope["job_id"])
            if receipt is None or _digest(receipt) != envelope["receipt_digest"]:
                raise PeerReplyError("reply envelope is not bound to an accepted receipt")
            source_ref = receipt.get("source_ref")
            if not isinstance(source_ref, dict):
                raise PeerReplyError("accepted receipt has no source provenance")
            destination = source_ref.get("source_peer")
            if not isinstance(destination, str) or not destination:
                raise PeerReplyError("accepted receipt has no routable source peer")
            if receipt.get("output_digest") != output_digest(envelope["content"]):
                raise PeerReplyError("reply content does not match accepted receipt output")

            # A crash after send and before this atomic write retries the same
            # idempotency key. The platform transport is the final exactly-once
            # authority for that unavoidable distributed-systems boundary.
            transport.send(destination, envelope["content"], idempotency_key=envelope_digest)
            _write_atomic(evidence_path, self._evidence(envelope, envelope_digest))
            path.unlink()
            return ReplyDrainResult(envelope["job_id"], "sent")

    @staticmethod
    def _evidence(envelope: dict, envelope_digest: str) -> dict:
        return {
            "schema_version": 1,
            "job_id": envelope["job_id"],
            "receipt_digest": envelope["receipt_digest"],
            "delivery_key": envelope_digest,
        }
