"""Local, fail-closed payload authority for peer-service jobs.

The durable service envelope intentionally contains provenance but never a DM
body.  This provider resolves that body from the recipient's append-only
monitor archive.  It has no queue or gateway dependency: the host has already
claimed the job, and the archive lookup must either return the exact recipient
record or refuse to execute it.
"""

from __future__ import annotations

import json
from pathlib import Path

from swarph_cli.peer_executor import PeerExecutorError
from swarph_cli.peer_service_host import PeerPayload, PeerPayloadRequest


class InboxLogPeerPayloadProvider:
    """Resolve one claimed payload from a private monitor ``inbox.log``.

    Repeated identical archive records are tolerated because a monitor can
    replay an observed DM after a crash.  Any conflicting record for the same
    message ID is an ambiguity, not a reason to select the latest content.
    """

    def __init__(self, inbox_log: Path, peer: str, *, max_log_bytes: int = 16_000_000):
        if not isinstance(peer, str) or not peer:
            raise PeerExecutorError("payload provider peer must be non-empty text")
        if not isinstance(max_log_bytes, int) or isinstance(max_log_bytes, bool) or max_log_bytes < 1:
            raise PeerExecutorError("max_log_bytes must be a positive integer")
        self.inbox_log = Path(inbox_log)
        self.peer = peer
        self.max_log_bytes = max_log_bytes

    def get_payload(self, request: PeerPayloadRequest) -> PeerPayload:
        if not isinstance(request, PeerPayloadRequest):
            raise PeerExecutorError("payload provider requires a claimed payload request")
        if request.peer != self.peer:
            raise PeerExecutorError("claimed peer does not match payload provider identity")

        try:
            before = self.inbox_log.stat()
        except FileNotFoundError as exc:
            raise PeerExecutorError("monitor inbox log is unavailable") from exc
        if not self.inbox_log.is_file():
            raise PeerExecutorError("monitor inbox log is not a regular file")
        if before.st_size > self.max_log_bytes:
            raise PeerExecutorError("monitor inbox log exceeds provider read bound")

        matches: set[tuple[str, str, str]] = set()
        try:
            with self.inbox_log.open("r", encoding="utf-8") as archive:
                for raw in archive:
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("id") != request.source_dm_id:
                        continue
                    message_id = record.get("id")
                    to_node = record.get("to_node")
                    from_node = record.get("from_node")
                    content = record.get("content")
                    if (
                        isinstance(message_id, bool)
                        or not isinstance(to_node, str)
                        or not isinstance(from_node, str)
                        or not isinstance(content, str)
                    ):
                        raise PeerExecutorError("source DM archive record is invalid")
                    if to_node != self.peer:
                        raise PeerExecutorError("source DM archive record has the wrong recipient")
                    if request.source_peer is not None and from_node != request.source_peer:
                        raise PeerExecutorError("source DM archive record has the wrong sender")
                    matches.add((to_node, from_node, content))
        except OSError as exc:
            raise PeerExecutorError("monitor inbox log cannot be read") from exc

        try:
            after = self.inbox_log.stat()
        except FileNotFoundError as exc:
            raise PeerExecutorError("monitor inbox log disappeared while reading") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PeerExecutorError("monitor inbox log changed while resolving payload")
        if not matches:
            raise PeerExecutorError("source DM is absent from the monitor inbox log")
        if len(matches) != 1:
            raise PeerExecutorError("source DM has conflicting monitor archive records")
        _, _, content = matches.pop()
        return PeerPayload(
            peer=request.peer,
            source_dm_id=request.source_dm_id,
            queue_claim_fence=request.queue_claim_fence,
            text=content,
            source_peer=request.source_peer,
        )
