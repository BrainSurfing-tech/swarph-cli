"""Receipt-gated local reply envelopes for peer-service output.

This module deliberately does not send to the mesh.  It derives a reply target
only from source-peer provenance that the queue recorded before service work
started; a platform-specific drainer may consume the resulting envelope later.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from swarph_cli.peer_executor import PeerExecutorError, PeerSpool


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class PeerReplyOutbox:
    """Stage one idempotent reply envelope from a validated peer result."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def stage(self, spool: PeerSpool, job_id: str) -> dict:
        result = spool.accepted_result(job_id)
        if result is None:
            raise PeerExecutorError("cannot publish a job without an accepted receipt")
        receipt, output = result["receipt"], result["output"]
        source_ref = receipt["source_ref"]
        source_peer = source_ref.get("source_peer")
        if not isinstance(source_peer, str) or not source_peer:
            raise PeerExecutorError("accepted receipt has no routable source peer")
        if not output["text"]:
            raise PeerExecutorError("accepted output is empty and cannot be published")
        envelope = {
            "schema_version": 1,
            "job_id": receipt["job_id"],
            "source_dm_id": receipt["source_dm_id"],
            "to_node": source_peer,
            "kind": "answer",
            "content": output["text"],
            "fencing_token": receipt["fencing_token"],
            "output_digest": receipt["output_digest"],
            "source_ref": source_ref,
        }
        path = self.root / f"{receipt['job_id']}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PeerExecutorError("existing reply outbox envelope is invalid") from exc
            if existing != envelope:
                raise PeerExecutorError("existing reply outbox envelope conflicts with receipt")
            return existing
        _write_atomic(path, envelope)
        return envelope
