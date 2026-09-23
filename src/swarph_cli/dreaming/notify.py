"""Notify the owning cell when the dreaming finding set changes (#937).

The finding set is disagree + surface_disagreement, keyed file:line:kind, plus
each organize finding that is not a clean status line. A clean line is not a
finding, and byte, line, and file counts are stripped from the key so a count
change does not send a DM. State lives at <out-parent>/.last-findings.json
and is written only after a successful send. An unchanged set sends nothing.
A missing state file is a first run: send the full set once.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# "memory-index-check: clean — ..." is a status line. "not clean" is not.
_CLEAN = re.compile(r"(?:^|:)\s*clean\b", re.IGNORECASE)
_COUNT = re.compile(
    r"\b\d[\d,]*(?:/\d[\d,]*)?\s*(?:bytes?|files?|lines?)\b"
    r"|\btarget\s+\d[\d,]*\b"
    r"|\b\d[\d,]*\s+more\b",
    re.IGNORECASE,
)


class NotifySendError(Exception):
    """The mesh send failed. Distinct from a findings exit."""


def _organize_key(text: str) -> str | None:
    if _CLEAN.search(text):
        return None
    stable = _COUNT.sub("", text)
    stable = re.sub(r"\s{2,}", " ", stable)
    stable = re.sub(r"\s+([,;])", r"\1", stable).strip(" -—;,")
    if not stable:
        return None
    return f"organize:{stable}"


def finding_keys(verdicts, organized) -> list[str]:
    keys: set[str] = set()
    for v in verdicts or []:
        if v.get("verdict") in ("disagree", "surface_disagreement"):
            keys.add(f"{v.get('file')}:{v.get('line')}:{v.get('kind')}")
    for line in (organized or {}).get("findings") or []:
        key = _organize_key(str(line).strip())
        if key:
            keys.add(key)
    return sorted(keys)


def _load_previous(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("keys") or [])


def _body(keys: list[str], previous: list[str] | None) -> str:
    if previous is None:
        lines = ["dreaming findings first run", f"counts: {len(keys)}"]
        lines.extend(keys)
        return "\n".join(lines)
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
    body = _body(keys, previous)
    try:
        sender(cell, body)
    except NotifySendError:
        raise
    except Exception as exc:
        raise NotifySendError(str(exc)) from exc
    state.write_text(json.dumps({"keys": keys}, indent=2) + "\n", encoding="utf-8")
    return True


def mesh_sender(cell: str, content: str) -> None:
    """Same path as ``swarph mesh send``: one status DM, no second transport."""
    import argparse

    from swarph_cli.commands.mesh import _run_send
    from swarph_cli.gateway_default import env_gateway

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
