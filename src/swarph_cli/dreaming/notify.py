"""Notify the owning cell when the dreaming finding set changes (#937).

The finding set is disagree + surface_disagreement, keyed file:line:kind, plus
each organize finding keyed by its category token and, where the line names
one, the file. A clean status line is not a finding. Organize keys contain
no digits: counts stay in the DM body, not in the key. State lives at
<out-parent>/.last-findings.json and is written only after a successful send.
An unchanged set sends nothing. A missing state file is a first run: send
the full set once.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# "memory-index-check: clean — ..." is a status line. "not clean" is not.
_CLEAN = re.compile(r"(?:^|:)\s*clean\b", re.IGNORECASE)
# Longest first so "MISSING INDEX" wins over a shorter prefix.
_CATEGORY = (
    "MISSING INDEX",
    "DUP_TARGET",
    "LINES",
    "CUT",
    "DANGLING",
    "ORPHANS",
    "TRUNCATED",
    "BUDGET",
    "WARN",
)
_FILE = re.compile(r"[\w.-]+\.md\b")


class NotifySendError(Exception):
    """The mesh send failed. Distinct from a findings exit."""


def _category(text: str) -> str | None:
    for name in _CATEGORY:
        if text.startswith(name + ":") or text.startswith(name + " "):
            return name
    return None


def organize_keys(lines) -> list[str]:
    """One key per category header. Member lines are not keys.

    A header that names a file keys ``organize:CATEGORY:file``. Indented
    children (CUT's lost pointers, ORPHANS' name window, a DUP_TARGET block)
    stay out of the key, so they cannot attach to the previous category and
    a count or a member list cannot change the set. Digits that are part of
    a filename stay. Counts do not.
    """
    keys: set[str] = set()
    for raw in lines or []:
        original = str(raw)
        text = original.strip()
        if not text or _CLEAN.search(text) or text.startswith("memory-index-check:"):
            continue
        # A child line is not a finding of its own, and it must not inherit
        # the category above it.
        if original.startswith((" ", "\t")) or _FILE.fullmatch(text):
            continue
        cat = _category(text)
        if not cat:
            continue
        files = _FILE.findall(text)
        if files:
            keys.add(f"organize:{cat}:{files[0]}")
        else:
            keys.add(f"organize:{cat}")
    return sorted(keys)


def finding_keys(verdicts, organized) -> list[str]:
    keys: set[str] = set()
    for v in verdicts or []:
        if v.get("verdict") in ("disagree", "surface_disagreement"):
            keys.add(f"{v.get('file')}:{v.get('line')}:{v.get('kind')}")
    keys.update(organize_keys((organized or {}).get("findings") or []))
    return sorted(keys)


def _load_previous(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("keys") or [])


def _details(organized) -> list[str]:
    """Original organize lines, counts included. Clean status is not a finding."""
    out = []
    for raw in (organized or {}).get("findings") or []:
        text = str(raw).strip()
        if text and not _CLEAN.search(text) and not text.startswith("memory-index-check:"):
            out.append(text)
    return out


def _body(keys: list[str], previous: list[str] | None, details: list[str]) -> str:
    if previous is None:
        lines = ["dreaming findings first run", f"counts: {len(keys)}"]
        lines.extend(keys)
    else:
        prev = set(previous)
        cur = set(keys)
        new = sorted(cur - prev)
        cleared = sorted(prev - cur)
        lines = [
            "dreaming findings changed",
            "new: " + (", ".join(new) if new else "(none)"),
            "cleared: " + (", ".join(cleared) if cleared else "(none)"),
            f"counts: {len(keys)} was {len(previous)}",
        ]
    if details:
        lines.append("details:")
        lines.extend(details)
    return "\n".join(lines)


def apply(out: Path, verdicts, organized, cell: str, sender) -> bool:
    """Send one DM when the set changes. Return True if a DM was sent.

    ``sender(cell, body)`` raises on failure. State is not written then.
    """
    out = Path(out)
    keys = finding_keys(verdicts, organized)
    state = out.parent / ".last-findings.json"
    previous = _load_previous(state)
    if previous is not None and sorted(previous) == keys:
        return False
    body = _body(keys, previous, _details(organized))
    try:
        sender(cell, body)
    except NotifySendError:
        raise
    except Exception as exc:
        raise NotifySendError(str(exc)) from exc
    state.write_text(json.dumps({"keys": keys}, indent=2) + "\n", encoding="utf-8")
    return True


def _post_to_card(card: str, content: str) -> None:
    """Put the finding on a card. A DM to the sender is refused (#185 / #409).

    Card #864: a finding goes on a card. Card #1278: the dreaming unit is
    lab-ovh notifying lab-ovh, so the send never arrives. Default card is 656,
    whose rows are closed; a holder who posts to a card thread closes their
    oldest open row on it, so do not point this at a card the sender holds.
    """
    import os

    from swarph_cli.commands.board import (
        _card_say_payload,
        _http_get_json,
        _post_json,
        _project_owner_orchestrator,
        _thread_recipient,
    )
    from swarph_cli.commands.mesh import _resolve_self_name, _resolve_token
    from swarph_cli.gateway_default import env_gateway

    gw = env_gateway()
    self_name = os.environ.get("SWARPH_SELF") or _resolve_self_name(None)
    token = _resolve_token(self_name, None)
    status, card_row = _http_get_json(f"{gw}/board/cards/{card}", token)
    if status != 200 or not isinstance(card_row, dict):
        raise NotifySendError(f"cannot read card #{card}: {card_row}")
    thread_uuid = card_row.get("thread_uuid")
    if not thread_uuid:
        raise NotifySendError(f"card #{card} has no bound thread")
    owner = None
    if not card_row.get("assignee"):
        owner = _project_owner_orchestrator(gw, token, card_row.get("project_id"))
    try:
        to_node, _reason = _thread_recipient(card_row, None, owner_orchestrator=owner)
    except RuntimeError as exc:
        raise NotifySendError(str(exc)) from exc
    status, payload = _post_json(
        f"{gw}/messages",
        _card_say_payload(self_name, to_node, "status", content, thread_uuid),
        token,
    )
    if status < 200 or status >= 300:
        raise NotifySendError(f"card #{card} post failed: {status} {payload}")


def mesh_sender(cell: str, content: str) -> None:
    """Same path as ``swarph mesh send``, unless that send would be a self-wake.

    SWARPH_SELF equal to the notify cell is the dreaming unit. Mesh send
    refuses it and the run exits 4. Post the same body to the dreaming card.
    """
    import argparse
    import os

    from swarph_cli.commands.mesh import _run_send
    from swarph_cli.gateway_default import env_gateway

    self_name = os.environ.get("SWARPH_SELF")
    if self_name and cell == self_name:
        _post_to_card(os.environ.get("SWARPH_DREAMING_CARD", "656"), content)
        return
    ns = argparse.Namespace(
        to=cell,
        kind="status",
        content=content,
        content_file=None,
        self_name=None,
        token_file=None,
        gateway=env_gateway(),
    )
    rc = _run_send(ns)
    if rc != 0:
        raise NotifySendError(f"mesh send exited {rc}")
