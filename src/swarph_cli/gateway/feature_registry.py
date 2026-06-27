"""Pure-function helpers for the swarph feature registry.

Constants are spec-locked (see docs/superpowers/specs/2026-05-28-swarph-feature-registry-design.md
§3.2). Keep this module dependency-free (stdlib only) so it's trivially unit-testable
without spinning up FastAPI or SQLite.
"""
from __future__ import annotations

# Spec §3.2(a) — RATIFICATION GATE.
# Hardcoded allowlist; adding a publisher = code change (a deliberate gate).
# Add your own trusted cell names here. Upgrade to a draft/ratify state machine
# when expanding beyond a small set of trusted cells.
PUBLISHER_ALLOWLIST: frozenset[str] = frozenset({"node-a"})

# Spec §3.2(b) — per-cell entry cap + per-field length caps (catalog-flood defense).
ENTRY_CAP_PER_CELL: int = 20

FIELD_LEN_CAPS: dict[str, int] = {
    "name": 100,
    "description": 500,
    "what_it_does": 500,
    "how_to_request": 500,
}

TAG_CAPS: dict[str, int] = {"count": 10, "length": 32}

import logging
import re

log = logging.getLogger(__name__)


_REQUIRED_FIELDS: tuple[str, ...] = ("id", "name", "description", "what_it_does", "how_to_request", "tags")


def _entry_is_well_formed(e: object) -> bool:
    """Cheap structural check before we touch fields."""
    if not isinstance(e, dict):
        return False
    for f in _REQUIRED_FIELDS:
        if f not in e:
            return False
    if not isinstance(e["tags"], list):
        return False
    return True


def aggregate_features(peers: list[dict]) -> list[dict]:
    """Flatten cell-published features across peers, gateway-stamping `cell`.

    `peers` is a list of dicts {name, capabilities} where `capabilities` is
    the already-parsed JSON blob from claude_peers. (server.py already parses
    it for /peers — same shape.)

    Behavior (spec §3.2(c)):
    - Peers without `published_features` contribute nothing.
    - Malformed `published_features` on one peer doesn't break others (skip+warn).
    - Any self-declared `cell` is overridden by the gateway-stamped peer name.
    - Entries failing structural check are dropped+warned individually.

    NOTE: Allowlist + caps filtering are applied by callers (the endpoint) via
    apply_allowlist() and apply_caps() — keep aggregate_features() pure so
    tests can target each behavior independently.
    """
    out: list[dict] = []
    for peer in peers:
        cell = peer.get("name")
        caps = peer.get("capabilities") or {}
        pub = caps.get("published_features")
        if not pub:
            continue
        if not isinstance(pub, list):
            log.warning("peer %s: published_features is not a list — skipping", cell)
            continue
        for e in pub:
            if not _entry_is_well_formed(e):
                log.warning("peer %s: malformed entry %r — dropped", cell, e)
                continue
            stamped = {k: e[k] for k in _REQUIRED_FIELDS}
            stamped["cell"] = cell
            out.append(stamped)
    return out


def _entry_within_field_caps(e: dict) -> bool:
    for field, cap in FIELD_LEN_CAPS.items():
        v = e.get(field, "")
        if not isinstance(v, str) or len(v) > cap:
            log.warning("entry cell=%s id=%s field=%s exceeds cap (%d) — dropped",
                        e.get("cell"), e.get("id"), field, cap)
            return False
    tags = e.get("tags", [])
    if len(tags) > TAG_CAPS["count"]:
        log.warning("entry cell=%s id=%s too many tags (%d) — dropped",
                    e.get("cell"), e.get("id"), len(tags))
        return False
    for t in tags:
        if not isinstance(t, str) or len(t) > TAG_CAPS["length"]:
            log.warning("entry cell=%s id=%s tag too long — dropped", e.get("cell"), e.get("id"))
            return False
    return True


def apply_caps(entries: list[dict]) -> list[dict]:
    """Enforce per-field length caps + per-cell entry-count cap.

    Spec §3.2(b). Cap-busting entries are skipped + WARN-logged.
    NEVER 500 the whole /features response on cap violations.

    Order: field-cap filter first, then per-cell count truncation — so a cell
    that publishes 30 entries half of which bust field caps lands at min(15, 20)
    rather than getting truncated to "the first 20 raw, then 15 surviving".
    """
    field_ok = [e for e in entries if _entry_within_field_caps(e)]
    seen: dict[str, int] = {}
    kept: list[dict] = []
    for e in field_ok:
        cell = e.get("cell")
        n = seen.get(cell, 0)
        if n >= ENTRY_CAP_PER_CELL:
            log.warning("entry cell=%s id=%s exceeds per-cell cap (%d) — dropped",
                        cell, e.get("id"), ENTRY_CAP_PER_CELL)
            continue
        seen[cell] = n + 1
        kept.append(e)
    return kept


def apply_allowlist(entries: list[dict]) -> list[dict]:
    """Drop entries whose stamped `cell` is not in PUBLISHER_ALLOWLIST.

    Spec §3.2(a) ratification gate. Skipped entries are WARNING-logged so the
    operator notices an unexpected publisher (e.g. a new cell came online with
    published_features and forgot to coordinate the allowlist update).
    """
    kept: list[dict] = []
    for e in entries:
        cell = e.get("cell")
        if cell in PUBLISHER_ALLOWLIST:
            kept.append(e)
        else:
            log.warning("entry from cell=%s not in PUBLISHER_ALLOWLIST — skipped", cell)
    return kept


# Spec §3.4 — LLM-control patterns to neutralize at refresh-job-time.
# Conservative substitution (escape, don't delete) — destroying content is its
# own bug class (legitimate uses get mangled). We turn the marker into a
# visibly-escaped form so a downstream ranker treats it as plain text.
_LLM_CONTROL_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"IGNORE PREVIOUS INSTRUCTIONS", re.IGNORECASE), "[IGNORE PREVIOUS INSTRUCTIONS]"),
    (re.compile(r"\bSYSTEM:", re.IGNORECASE), "[SYSTEM:]"),
    (re.compile(r"\bASSISTANT:", re.IGNORECASE), "[ASSISTANT:]"),
    (re.compile(r"\bUSER:", re.IGNORECASE), "[USER:]"),
    (re.compile(r"<\|im_start\|>"), "[im_start]"),
    (re.compile(r"<\|im_end\|>"), "[im_end]"),
    (re.compile(r"###\s*Instruction", re.IGNORECASE), "[### Instruction]"),
)


def _neutralize_text(s: str) -> str:
    """Escape LLM-control sequences in cell-supplied free text.

    belt+suspenders with an external ranker's delimiter wrap.
    A field surviving caps may still contain a prompt-injection payload;
    rewriting common markers to a visibly-escaped form prevents the most
    common injections without destroying legitimate uses (the escaping is
    visible, so a curator can spot it).
    """
    for pat, repl in _LLM_CONTROL_PATTERNS:
        s = pat.sub(repl, s)
    return s


def neutralize_entry(e: dict) -> dict:
    """Apply _neutralize_text to every free-text field of an entry."""
    out = dict(e)
    for field in ("name", "description", "what_it_does", "how_to_request"):
        if field in out and isinstance(out[field], str):
            out[field] = _neutralize_text(out[field])
    return out
