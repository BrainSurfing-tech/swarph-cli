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
import uuid
from pathlib import Path


class PeerExecutorError(RuntimeError):
    pass


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
    _canonical_id(job.get("job_id"), "job_id")
    _canonical_id(job.get("destination_peer"), "destination_peer")
    if not isinstance(job.get("source_dm_id"), int) or isinstance(job["source_dm_id"], bool):
        raise PeerExecutorError("source_dm_id must be an integer")
    if not isinstance(job.get("delivery_ref"), str) or not job["delivery_ref"]:
        raise PeerExecutorError("delivery_ref must be non-empty")


class PeerSpool:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.running = self.root / "running"
        self.claims = self.root / "claims"
        self.receipts = self.root / "receipts"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.pending, self.running, self.claims, self.receipts):
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
        claim_path = self.claims / f"{job_id}.json"
        prior = _read_object(claim_path) if claim_path.exists() else {}
        token = int(prior.get("fencing_token", 0)) + 1
        claim = {"job_id": job_id, "destination_peer": peer, "fencing_token": token}
        _write_atomic(claim_path, claim)  # durable before work or the move
        try:
            os.replace(pending, self.running / pending.name)
        except FileNotFoundError as exc:
            raise PeerExecutorError("job is no longer pending") from exc
        return claim

    def accept_receipt(self, receipt: dict) -> None:
        self.initialize()
        for key in ("job_id", "destination_peer"):
            _canonical_id(receipt.get(key), key)
        if not isinstance(receipt.get("fencing_token"), int):
            raise PeerExecutorError("receipt fencing_token must be an integer")
        if not isinstance(receipt.get("output_digest"), str) or not receipt["output_digest"]:
            raise PeerExecutorError("receipt output_digest must be non-empty")
        claim = _read_object(self.claims / f"{receipt['job_id']}.json")
        job = _read_object(self.running / f"{receipt['job_id']}.json")
        if (claim["destination_peer"], claim["fencing_token"]) != (
            receipt["destination_peer"], receipt["fencing_token"]
        ) or job.get("destination_peer") != receipt["destination_peer"]:
            raise PeerExecutorError("stale or wrong-peer receipt")
        path = self.receipts / f"{receipt['job_id']}.json"
        if path.exists():
            raise PeerExecutorError("receipt already accepted")
        _write_atomic(path, receipt)


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
