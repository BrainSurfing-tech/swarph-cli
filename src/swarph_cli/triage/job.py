#!/usr/bin/env python3
"""Triage job driver (card #1073): route classified records onto owner rows.

A record is {source_organ, type, confidence, owner_hint, evidence,
item_id, sharp, security}. Routing per the seven rulings:

- stuck-work owner is the per-item blocking holder (owner_hint); an
  unresolvable holder routes to lab-ovh and is counted (ruling 1).
- other organs route via the owner map; unmapped organs route to lab-ovh
  and are counted, never dropped (ruling 1).
- needed-row-closed-race: no DM, no append, footer +1 (ruling 3).
- split: footer split count per organ (ruling 2).
- confident-benign: footer count + weekly sample, never a slot (ruling 5).
- sharp real-stall: appended, and the caller-supplied dm_sender may send
  the existing one-a-day DM; sends are counted (ruling 6). The driver
  never DMs any other type.
- closed-row-no-need / missing-need races: counted per organ — except a
  security item, which is appended instead of left uncounted (ruling 6).
"""
from __future__ import annotations

from .router import OWNER_DEFAULT, organ_owner
from .sensor import ORGAN_STUCK

TYPE_CLOSED_RACE = "needed-row-closed-race"
TYPE_REAL_STALL = "real-stall"
TYPE_BENIGN = "confident-benign"


def route_and_store(store, owners_map: dict, records: list[dict],
                    dm_sender=None) -> dict:
    """Route records. dm_sender(record, owner) -> bool (sent?), called only
    for sharp real-stalls. Returns a summary with the footer."""
    summary = {"appended": 0, "duplicate": 0, "overflow": 0, "dm_sent": 0}
    for record in records:
        _route_one(store, owners_map, record, dm_sender, summary)
    summary["footer"] = store.footer()
    return summary


def _route_one(store, owners_map, record, dm_sender, summary):
    organ = record.get("source_organ")
    item_type = record.get("type")
    item_id = record.get("item_id")
    sharp = bool(record.get("sharp"))
    security = bool(record.get("security"))

    if item_type == TYPE_BENIGN:
        store.count_benign(item_id)
        return "benign"

    if item_type == TYPE_CLOSED_RACE:
        store.count_closed_race()
        return "closed-race"

    race = record.get("race")
    if race and not security:
        store.count_race(organ)
        return "race"

    if not sharp:
        store.count_split(organ)
        if record.get("unmapped"):
            store.count_unmapped(organ)
        return "split"

    owner, fell_back = _resolve_owner(owners_map, organ, record)
    if fell_back or record.get("unmapped"):
        # unresolvable holder or unmapped organ: routed to lab-ovh AND
        # counted (ruling 1).
        store.count_unmapped(organ)

    outcome = store.append_item(
        owner, item_id, item_type, security_first=security)
    summary[outcome] = summary.get(outcome, 0) + 1

    if (outcome == "appended" and item_type == TYPE_REAL_STALL
            and dm_sender is not None):
        try:
            sent = dm_sender(record, owner)
        except Exception:
            sent = False
        if sent:
            store.mark_dm_sent()
            summary["dm_sent"] += 1
    return outcome


def _resolve_owner(owners_map: dict, organ: str, record: dict):
    """(owner, fell_back). Stuck-work: the per-item blocking holder;
    unresolvable (None or non-peer) routes to lab-ovh. Other organs:
    the owner map (unmapped -> lab-ovh)."""
    if organ == ORGAN_STUCK:
        hint = record.get("owner_hint")
        is_peer = record.get("holder_is_peer", False)
        if hint and is_peer:
            return hint, False
        return OWNER_DEFAULT, True
    owner, unmapped = organ_owner(owners_map, organ)
    return owner, unmapped
