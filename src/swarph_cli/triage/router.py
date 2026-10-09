#!/usr/bin/env python3
"""Organ triage router library (card #1073, seven binding rulings #1413).

(1) A stored item is source_organ, type, confidence, owner, evidence — and
    the stored evidence is IDENTIFIERS ONLY (row id, card id, path, line).
    The sensor may pass a payload into the classifier, but payload text is
    never stored and never logged. The owner map is a DATA file the job
    reads (owners.json + local override), never a code constant. An organ
    with no map entry is split onto lab-ovh's triage row and counted —
    never dropped.
(2) Sharp/split thresholds live in that same data, written by a PROVEN
    calibration. This module contains NO confidence literal: a rule match
    is sharp without a threshold; a model result is sharp only when the
    organ's calibrated number is present and confidence >= it. Until then,
    every model result for the organ is split.
(3) Deliberate-wait is a future card due_at on the NEEDED ROW'S CARD
    (the schedule date card 183 already stores). Never parsed from a card
    link, never a new obligation column. No due_at -> not deliberate-wait.
    A closed needed row is needed-row-closed-race (no DM, no append, footer
    +1). An open needed row with no future due_at stays real-stall when the
    48h rule matches.
(4) One open row per owner ("<owner> triage", at most 10 items, carried
    never duplicated, new items fill free slots only). Security items kept
    first. Overflow is named in the footer count and stays queued
    (re-derived next run, never deleted).
(5) Confident-benign is never omitted and never takes a slot: footer
    count + a weekly PROVEN sample of evidence pointers. Counted items are
    never deleted.
(6) The router never closes, edits, or deletes a row, card, or item. The
    only writes: appends to the open triage row (accept lines only ever
    ADDED, asserted) and footer counts. A sharp real-stall may send the
    existing one-a-day DM (counted); the stuck row is never closed. Race
    counters are per-organ (unavailable to security items); a security
    item is never on an uncounted drop path.
(7) A model classifier sees the payload only after scrub_payload(); if
    the payload is not scrubbable text the model is not called and the
    item is split. Rules read status/due_at metadata and never log raw
    payload.
"""
from __future__ import annotations

import json
import os
import re

OWNER_DEFAULT = "lab-ovh"
ITEM_CAP = 10
ACCEPT_MAX = 2000
ROW_TITLE = "{owner} triage"
RULE_CERTAINTY = 1.0  # a rule match is certain, not a cutoff (ruling 2)


def row_title(owner: str) -> str:
    return ROW_TITLE.format(owner=owner)


def load_owners(default_path: str, local_path: str | None = None) -> dict:
    """Owner map + thresholds + label sets. Local file deep-merges over the
    shipped defaults (this is where a PROVEN calibration writes)."""
    with open(default_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("owner map is not an object")
    if local_path and os.path.isfile(local_path):
        with open(local_path, encoding="utf-8") as f:
            local = json.load(f)
        if not isinstance(local, dict):
            raise ValueError("local owner map is not an object")
        for key in ("owners", "thresholds", "label_sets"):
            if isinstance(local.get(key), dict):
                merged = dict(data.get(key, {}))
                merged.update(local[key])
                data[key] = merged
    owners = data.get("owners")
    if not isinstance(owners, dict):
        raise ValueError("owner map has no owners object")
    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds is not an object")
    for organ, value in thresholds.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"threshold for {organ!r} is not a number")
        if not 0 <= value <= 1:
            raise ValueError(f"threshold for {organ!r} outside [0,1]")
    return data


def organ_owner(owners_map: dict, organ: str):
    """(owner, unmapped): unknown organs route to lab-ovh and are counted."""
    entry = (owners_map.get("owners") or {}).get(organ)
    if not isinstance(entry, dict) or not entry.get("owner"):
        return OWNER_DEFAULT, True
    return entry["owner"], False


# ---- payload scrub (ruling 7) --------------------------------------------------
# Shared module since msg 65982: swarph_cli.scrub is the one source, the
# router imports it (no copy). Secret-shaped TEXT is redacted before any
# model classifier input. Rules never see the payload at all — they read
# status/due_at metadata. A None scrub means unscrubbable, so the model
# is not called for it (caller splits the item).
from swarph_cli.scrub import scrub_text as scrub_payload


# ---- stuck-work rules (ruling 2-3: sharp without a threshold) --------------------
# Inputs are metadata (statuses, due_at, ages) — never payload text.

def classify_stuck_row(need_state: str, need_card_due_at: str | None,
                       now_iso: str) -> tuple[str, float, bool]:
    """(type, confidence, sharp) for one waiter-need pair.

    need_state: 'closed' | 'open' | 'missing'. need_card_due_at: the
    NEEDED ROW'S CARD due_at (ISO), or None. The sensor guarantees the
    48h match before calling.
    """
    if need_state == "closed":
        return ("needed-row-closed-race", RULE_CERTAINTY, True)
    if need_state != "open":
        raise ValueError(f"unevaluable need state {need_state!r}")
    if need_card_due_at is not None and need_card_due_at > now_iso:
        return ("deliberate-wait", RULE_CERTAINTY, True)
    return ("real-stall", RULE_CERTAINTY, True)


# ---- model classifier seam (ruling 2 + 7) ------------------------------------------
# Pluggable, cheapest first. The client protocol is generate(prompt)->str
# (dreaming SLMClient shape); test doubles implement the same method.
# Sharp ONLY with a calibrated threshold present and confidence >= it —
# until PROVEN writes the number, every model result splits. Unparseable
# output, an off-label answer, or an unscrubbable payload all split
# WITHOUT calling further. No confidence literal anywhere in this module.

MODEL_PROMPT = (
    "Classify the item for organ {organ}. Reply with ONLY one JSON object: "
    '{"label": "<one of: LABELS>", "confidence": <0..1>}. '
    "Labels: LABELS. Text: {text}"
)


def classify_model(organ: str, payload, label_set: list[str],
                   thresholds: dict, model_client) -> tuple[str, float | None,
                                                            bool, bool]:
    """(type, confidence, sharp, model_called). Never raises: every
    failure mode returns a split item."""
    scrubbed = scrub_payload(payload)
    if scrubbed is None:
        return ("split", None, False, False)
    prompt = MODEL_PROMPT.replace("{organ}", organ).replace(
        "LABELS", ", ".join(label_set)).replace("{text}", scrubbed)
    try:
        raw = model_client.generate(prompt)
    except Exception:
        return ("split", None, False, True)
    try:
        data = json.loads(_strip_fences(raw))
    except Exception:
        return ("split", None, False, True)
    if not isinstance(data, dict):
        return ("split", None, False, True)
    label, confidence = data.get("label"), data.get("confidence")
    if label not in label_set:
        return ("split", None, False, True)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return ("split", None, False, True)
    threshold = thresholds.get(organ)
    if threshold is None:
        return (label, float(confidence), False, True)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return (label, float(confidence), False, True)
    if float(confidence) >= float(threshold):
        return (label, float(confidence), True, True)
    return (label, float(confidence), False, True)


def _strip_fences(response: str) -> str:
    text = (response or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)


class SlmClassifier:
    """dreaming-SLM adapter for the model seam (OpenAI chat-completions)."""

    def __init__(self, client):
        self.client = client

    def classify(self, organ: str, payload, label_set: list[str],
                 thresholds: dict):
        return classify_model(organ, payload, label_set, thresholds,
                              self.client)
