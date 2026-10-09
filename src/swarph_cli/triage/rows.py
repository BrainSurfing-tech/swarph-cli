#!/usr/bin/env python3
"""Triage rows: one open row per owner, carried never duplicated (ruling 4-6).

The job layer supplies a backend with two methods — find_open_row(title)
-> row-or-None and create_row(title, owner) -> row — plus dict-like posts
via append(), so unit tests run against an in-memory double while
production uses board rows/cards. This module never closes, edits, or
deletes anything (ruling 6): accepts are only ever ADDED, asserted after
each append, and the row is never reopened for removal.
"""
from __future__ import annotations

from .router import ACCEPT_MAX, ITEM_CAP, row_title


def take_accept(item_id: str, kind: str) -> str:
    """Accept-line payload, capped at 2000 chars. Identifiers only —
    the caller passes evidence pointers, never raw payload text."""
    return f"ACCEPT {item_id} {kind}"[:ACCEPT_MAX]


def _item_ids(posts: list[dict]) -> set[str]:
    ids: set[str] = set()
    for post in posts:
        body = (post.get("body") or "")
        if body.startswith("ACCEPT "):
            parts = body.split()
            if len(parts) >= 2:
                ids.add(parts[1])
    return ids


def _is_security(post: dict) -> bool:
    return (post.get("kind") or "") == "security"


class TriageStore:
    """One open row per owner ("<owner> triage", <=10 items)."""

    def __init__(self, backend, benign_sample_every: int = 7):
        self.backend = backend
        self.split_counts: dict[str, int] = {}
        self.unmapped_counts: dict[str, int] = {}
        self.race_counts: dict[str, int] = {}
        self.closed_race_count = 0
        self.overflow_count = 0
        self.benign_count = 0
        self.benign_sample_every = benign_sample_every
        self.benign_sample: list[str] = []
        self.sent_dm_count = 0

    def owner_row(self, owner: str) -> dict:
        title = row_title(owner)
        row = self.backend.find_open_row(title)
        if row is None:
            row = self.backend.create_row(title, owner)
        return row

    def append_item(self, owner: str, item_id: str, kind: str,
                    security_first: bool = False) -> str:
        """Append one item. Returns 'appended' | 'duplicate' | 'overflow'.

        Carried never duplicated: an item id already on the row is left
        alone. Security items are recorded with the security kind so they
        sort first. Full rows overflow (named in the footer, re-derived
        next run — never deleted)."""
        row = self.owner_row(owner)
        posts = self.backend.list_posts(row)
        if item_id in _item_ids(posts):
            return "duplicate"
        if len(_item_ids(posts)) >= ITEM_CAP:
            self.overflow_count += 1
            return "overflow"
        post = self.backend.append(
            row, body=take_accept(item_id, kind),
            kind=("security" if security_first else kind))
        self._assert_accept_added(row, post)
        return "appended"

    def _assert_accept_added(self, row: dict, post: dict) -> None:
        """Accept lines are only ever ADDED: the new post must be present
        afterwards (ruling 6: asserted)."""
        bodies = [p.get("body") for p in self.backend.list_posts(row)]
        assert post.get("body") in bodies, "accept line not added"

    def security_items(self, owner: str) -> list[dict]:
        return [p for p in self.backend.list_posts(self.owner_row(owner))
                if _is_security(p)]

    # -- footer counters (ruling 4-6) ------------------------------------------
    def count_split(self, organ: str) -> None:
        self.split_counts[organ] = self.split_counts.get(organ, 0) + 1

    def count_unmapped(self, organ: str) -> None:
        self.unmapped_counts[organ] = self.unmapped_counts.get(organ, 0) + 1

    def count_race(self, organ: str | None) -> None:
        """closed-row-no-need / missing-need race. Counted per organ —
        except security items, which are never on an uncounted drop path
        (ruling 6: organ=None appends to the row instead)."""
        if organ is None:
            return
        self.race_counts[organ] = self.race_counts.get(organ, 0) + 1

    def count_closed_race(self) -> None:
        self.closed_race_count += 1

    def count_benign(self, evidence_id: str) -> None:
        """Confident-benign: footer count + weekly PROVEN sample of
        evidence pointers (ruling 5)."""
        self.benign_count += 1
        if self.benign_count % self.benign_sample_every == 1:
            self.benign_sample.append(evidence_id)

    def mark_dm_sent(self) -> None:
        self.sent_dm_count += 1

    def footer(self) -> dict:
        return {
            "split": dict(self.split_counts),
            "unmapped": dict(self.unmapped_counts),
            "race": dict(self.race_counts),
            "closed_race": self.closed_race_count,
            "overflow": self.overflow_count,
            "confident_benign": self.benign_count,
            "benign_sample": list(self.benign_sample),
            "dm_sent": self.sent_dm_count,
        }
