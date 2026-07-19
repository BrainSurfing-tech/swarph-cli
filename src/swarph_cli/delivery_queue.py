"""DeliveryQueue — drained-but-undelivered mesh DMs, persisted beside the
daemon cursor (write-and-rename atomic) so it survives a restart. A DM is
never lost: it stays queued until injected into the session. Fail-safe: a
corrupt/unreadable file is treated as empty (never raises)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List


def wake_for(kind: str, thread_id) -> bool:
    """Actionable (wake on next idle) = question / unblock, or a threaded
    answer (targeted reply). Broadcast answers / fyi / status ride along."""
    if kind in ("question", "unblock"):
        return True
    return kind == "answer" and thread_id is not None


class DeliveryQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending: List[dict] = []
        self.deferred_ticks = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._pending = list(data.get("pending", []))
            self.deferred_ticks = int(data.get("deferred_ticks", 0))
        except (FileNotFoundError, ValueError, OSError, TypeError):
            self._pending = []
            self.deferred_ticks = 0

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"pending": self._pending,
                        "deferred_ticks": self.deferred_ticks},
                       indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # atomic

    def enqueue(self, dm: dict) -> None:
        mid = dm["id"]
        if any(e["id"] == mid for e in self._pending):
            return
        kind = dm.get("kind", "")
        thread_id = dm.get("thread_id")
        self._pending.append({
            "id": mid,
            "from": dm.get("from_node"),
            "kind": kind,
            "thread_id": thread_id,
            "content": dm.get("content", ""),
            "wake": wake_for(kind, thread_id),
        })
        self._persist()

    def pending(self) -> List[dict]:
        return list(self._pending)

    def any_wake(self) -> bool:
        return any(e.get("wake") for e in self._pending)

    def remove(self, ids: set) -> None:
        self._pending = [e for e in self._pending if e["id"] not in ids]
        self._persist()

    def bump_deferred(self) -> int:
        self.deferred_ticks += 1
        self._persist()
        return self.deferred_ticks

    def reset_deferred(self) -> None:
        if self.deferred_ticks != 0:
            self.deferred_ticks = 0
            self._persist()
