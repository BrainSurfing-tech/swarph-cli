#!/usr/bin/env python3
"""Triage sensors (card #1073, first consumer: the #1064 stuck-work sweep).

A sensor emits {source_organ, payload, evidence}: evidence is IDENTIFIERS
ONLY (row id, card id, path, line) — payload text is never stored and
never logged (ruling 1), and rules read metadata, never the payload
(ruling 7).
"""
from __future__ import annotations

from .router import classify_stuck_row

ORGAN_STUCK = "stuck-work"
ORGAN_SECURITY = "security"


def now_iso_utc(now) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def stuck_evidence(waiter_row_id: int, need_row_id: int | None) -> dict:
    """Identifiers only (ruling 1)."""
    return {"waiter_row": waiter_row_id, "need_row": need_row_id}


def map_stuck_finding(finding: dict, need_state: str,
                      need_card_due_at: str | None,
                      now_iso: str) -> dict:
    """One sweep finding -> classified triage record.

    finding: a sweep _row_findings entry (waiter id, blocker, need id).
    need_state: 'closed' | 'open' | 'missing' for the need row.
    need_card_due_at: the NEEDED ROW'S CARD due_at, or None (ruling 3).
    Returns {source_organ, type, confidence, owner_hint, evidence, sharp}.
    """
    item_type, confidence, sharp = classify_stuck_row(
        need_state, need_card_due_at, now_iso)
    return {
        "source_organ": ORGAN_STUCK,
        "type": item_type,
        "confidence": confidence,
        "owner_hint": finding.get("blocker"),
        "evidence": stuck_evidence(finding.get("id"), finding.get("need")),
        "sharp": sharp,
    }


def security_evidence(path: str, line: int | None = None) -> dict:
    """Identifiers only (ruling 1)."""
    return {"path": path, "line": line}
