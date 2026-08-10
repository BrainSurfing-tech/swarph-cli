"""Durable, peer-bound work spool for service-owned agent sessions.

This module deliberately has no terminal or App Server integration.  It defines
the ownership boundary those launchers must satisfy: jobs are addressed to one
peer, claims carry a monotonic fencing token, and receipts are accepted only
for the current claim.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class PeerExecutorError(RuntimeError):
    pass


class PeerServiceAuthorizer(Protocol):
    """Platform boundary for the service identity and private spool checks."""

    def require_service(self, peer: str, spool_root: Path) -> None: ...


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeerExecutorError(f"invalid JSON at {path}") from exc
    if not isinstance(value, dict):
        raise PeerExecutorError(f"object required at {path}")
    return value


def _canonical_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != Path(value).name:
        raise PeerExecutorError(f"{field} must be a non-empty filename-safe string")
    return value


def _validate_job(job: dict) -> None:
    allowed = {"schema_version", "job_id", "source_dm_id", "destination_peer", "delivery_ref"}
    unknown = set(job) - allowed
    if unknown:
        raise PeerExecutorError(f"job contains undeclared fields: {sorted(unknown)}")
    if job.get("schema_version") != 1:
        raise PeerExecutorError("unsupported job schema_version")
    _canonical_id(job.get("job_id"), "job_id")
    _canonical_id(job.get("destination_peer"), "destination_peer")
    if not isinstance(job.get("source_dm_id"), int) or isinstance(job["source_dm_id"], bool):
        raise PeerExecutorError("source_dm_id must be an integer")
    if (
        not isinstance(job.get("delivery_ref"), str)
        or not job["delivery_ref"]
        or len(job["delivery_ref"]) > 256
        or "\n" in job["delivery_ref"]
        or "\r" in job["delivery_ref"]
    ):
        raise PeerExecutorError("delivery_ref must be a short, single-line reference")


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise PeerExecutorError("output_digest must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PeerExecutorError("output_digest must be a SHA-256 digest") from exc
    return value


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


class PeerSpool:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.running = self.root / "running"
        self.claims = self.root / "claims"
        self.locks = self.root / "locks"
        self.outputs = self.root / "outputs"
        self.receipts = self.root / "receipts"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.pending, self.running, self.claims, self.locks, self.outputs, self.receipts):
            directory.mkdir(exist_ok=True)
            if directory.stat().st_dev != self.root.stat().st_dev:
                raise PeerExecutorError("spool directories must be on one filesystem")

    def enqueue(self, job: dict) -> Path:
        self.initialize()
        _validate_job(job)
        path = self.pending / f"{job['job_id']}.json"
        if path.exists() or (self.running / path.name).exists():
            raise PeerExecutorError(f"job already exists: {job['job_id']}")
        _write_atomic(path, job)
        return path

    def claim(self, job_id: str, peer: str) -> dict:
        self.initialize()
        job_id, peer = _canonical_id(job_id, "job_id"), _canonical_id(peer, "peer")
        pending = self.pending / f"{job_id}.json"
        job = _read_object(pending)
        _validate_job(job)
        if job["destination_peer"] != peer:
            raise PeerExecutorError("peer cannot claim another peer's job")
        try:
            os.replace(pending, self.running / pending.name)
        except FileNotFoundError as exc:
            raise PeerExecutorError("job is no longer pending") from exc
        # The successful rename is the cross-process ownership boundary.  A
        # process that loses it must not create or advance a claim record.
        claim = self._new_claim(job_id, peer, token=1)
        _write_atomic(self.claims / f"{job_id}.json", claim)
        return claim

    def reclaim(self, job_id: str, peer: str, *, now: float | None = None) -> dict:
        """Take over a crashed worker only after its current lease expires."""
        self.initialize()
        job_id, peer = _canonical_id(job_id, "job_id"), _canonical_id(peer, "peer")
        with _exclusive_file_lock(self.locks / f"{job_id}.lock"):
            if (self.receipts / f"{job_id}.json").exists():
                raise PeerExecutorError("job already has an accepted receipt")
            job = _read_object(self.running / f"{job_id}.json")
            _validate_job(job)
            if job["destination_peer"] != peer:
                raise PeerExecutorError("peer cannot reclaim another peer's job")
            claim_path = self.claims / f"{job_id}.json"
            prior = _read_object(claim_path) if claim_path.exists() else None
            observed_at = time.time() if now is None else now
            if prior is not None:
                if prior.get("destination_peer") != peer:
                    raise PeerExecutorError("peer cannot reclaim another peer's job")
                if observed_at < prior.get("lease_expires_at", float("inf")):
                    raise PeerExecutorError("job claim lease has not expired")
                token = int(prior.get("fencing_token", 0)) + 1
            else:
                # A crash between pending->running and claim persistence leaves
                # an orphaned job with no active lease; recovery starts token 1.
                token = 1
            claim = self._new_claim(job_id, peer, token=token, now=observed_at)
            _write_atomic(claim_path, claim)
        return claim

    def write_output(self, job_id: str, peer: str, fencing_token: int, text: str) -> dict:
        """Persist the service result before a receipt can acknowledge it."""
        self.initialize()
        job_id, peer = _canonical_id(job_id, "job_id"), _canonical_id(peer, "peer")
        if not isinstance(fencing_token, int) or fencing_token < 1:
            raise PeerExecutorError("fencing_token must be a positive integer")
        if not isinstance(text, str):
            raise PeerExecutorError("output text must be a string")
        job = _read_object(self.running / f"{job_id}.json")
        claim = _read_object(self.claims / f"{job_id}.json")
        if (job.get("destination_peer"), claim.get("destination_peer"), claim.get("fencing_token")) != (
            peer,
            peer,
            fencing_token,
        ):
            raise PeerExecutorError("stale or wrong-peer output")
        output_path = self._output_path(job_id, fencing_token)
        if output_path.exists():
            raise PeerExecutorError("output already exists for this claim")
        output = {
            "job_id": job_id,
            "source_dm_id": job["source_dm_id"],
            "destination_peer": peer,
            "fencing_token": fencing_token,
            "text": text,
            "output_digest": output_digest(text),
        }
        _write_atomic(output_path, output)
        return output

    def accept_receipt(self, receipt: dict) -> None:
        self.initialize()
        for key in ("job_id", "destination_peer"):
            _canonical_id(receipt.get(key), key)
        if not isinstance(receipt.get("fencing_token"), int):
            raise PeerExecutorError("receipt fencing_token must be an integer")
        if not isinstance(receipt.get("source_dm_id"), int) or isinstance(receipt["source_dm_id"], bool):
            raise PeerExecutorError("receipt source_dm_id must be an integer")
        _validate_digest(receipt.get("output_digest"))
        claim = _read_object(self.claims / f"{receipt['job_id']}.json")
        job = _read_object(self.running / f"{receipt['job_id']}.json")
        if (claim["destination_peer"], claim["fencing_token"]) != (
            receipt["destination_peer"], receipt["fencing_token"]
        ) or (job.get("destination_peer"), job.get("source_dm_id")) != (
            receipt["destination_peer"], receipt["source_dm_id"]
        ):
            raise PeerExecutorError("stale or wrong-peer receipt")
        output_path = self._output_path(receipt["job_id"], receipt["fencing_token"])
        if not output_path.exists():
            raise PeerExecutorError("durable output is missing")
        output = _read_object(output_path)
        if (
            output.get("source_dm_id"),
            output.get("destination_peer"),
            output.get("fencing_token"),
            output.get("output_digest"),
        ) != (
            receipt["source_dm_id"],
            receipt["destination_peer"],
            receipt["fencing_token"],
            receipt["output_digest"],
        ):
            raise PeerExecutorError("receipt does not match durable output")
        path = self.receipts / f"{receipt['job_id']}.json"
        if path.exists():
            raise PeerExecutorError("receipt already accepted")
        _write_atomic(path, receipt)

    def receipt_accepted(self, job_id: str) -> bool:
        """Whether this job has a validated durable receipt, not merely a file."""
        job_id = _canonical_id(job_id, "job_id")
        path = self.receipts / f"{job_id}.json"
        if not path.exists():
            return False
        try:
            receipt = _read_object(path)
            self._validate_accepted_receipt(receipt)
        except PeerExecutorError:
            return False
        return True

    def _validate_accepted_receipt(self, receipt: dict) -> None:
        self.initialize()
        job_id = _canonical_id(receipt.get("job_id"), "job_id")
        running = _read_object(self.running / f"{job_id}.json")
        claim = _read_object(self.claims / f"{job_id}.json")
        if (receipt.get("destination_peer"), receipt.get("source_dm_id"), receipt.get("fencing_token")) != (
            running.get("destination_peer"), running.get("source_dm_id"), claim.get("fencing_token")
        ):
            raise PeerExecutorError("receipt no longer matches current claim")
        output_path = self._output_path(job_id, receipt["fencing_token"])
        if not output_path.exists():
            raise PeerExecutorError("receipt output is missing")
        output = _read_object(output_path)
        if output.get("output_digest") != receipt.get("output_digest"):
            raise PeerExecutorError("receipt output digest no longer matches")

    @staticmethod
    def _new_claim(job_id: str, peer: str, *, token: int, now: float | None = None) -> dict:
        if token < 1:
            raise PeerExecutorError("fencing_token must be a positive integer")
        claimed_at = time.time() if now is None else now
        return {
            "job_id": job_id,
            "destination_peer": peer,
            "fencing_token": token,
            "lease_expires_at": claimed_at + 300,
        }

    def _output_path(self, job_id: str, fencing_token: int) -> Path:
        if not isinstance(fencing_token, int) or fencing_token < 1:
            raise PeerExecutorError("fencing_token must be a positive integer")
        return self.outputs / f"{job_id}.{fencing_token}.json"


class PeerService:
    """Peer-fixed capability used by the unattended service process only."""

    def __init__(self, spool: PeerSpool, peer: str, authorizer: PeerServiceAuthorizer):
        self.spool = spool
        self.peer = _canonical_id(peer, "peer")
        self.authorizer = authorizer

    def claim(self, job_id: str) -> dict:
        self._authorize()
        return self.spool.claim(job_id, self.peer)

    def reclaim(self, job_id: str, *, now: float | None = None) -> dict:
        self._authorize()
        return self.spool.reclaim(job_id, self.peer, now=now)

    def write_output(self, job_id: str, fencing_token: int, text: str) -> dict:
        self._authorize()
        return self.spool.write_output(job_id, self.peer, fencing_token, text)

    def accept_receipt(self, receipt: dict) -> None:
        self._authorize()
        self.spool.accept_receipt(receipt)

    def _authorize(self) -> None:
        self.authorizer.require_service(self.peer, self.spool.root)


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
