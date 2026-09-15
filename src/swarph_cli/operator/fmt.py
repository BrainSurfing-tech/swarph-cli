"""Human-readable formatting for operator CLI responses."""

from __future__ import annotations

import json
from typing import Any


def _rows(d: Any, *keys: str) -> list:
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for k in keys:
        if isinstance(d.get(k), list):
            return d[k]
    return []


def format_response(mode: str, d: Any) -> str:
    lines: list[str] = []

    if isinstance(d, dict) and d.get("detail") and mode != "channels":
        detail = d["detail"]
        lines.append(f"  {detail if isinstance(detail, str) else json.dumps(detail)[:200]}")
        return "\n".join(lines)

    if mode == "channels":
        for c in _rows(d, "channels"):
            desc = (c.get("description") or "")[:64]
            lines.append(f"  {c.get('name', ''):<14} {c.get('kind', ''):<9} {desc}")
    elif mode == "who":
        for x in _rows(d, "members"):
            lines.append(f"  {x.get('peer', ''):<20} wake={x.get('wake_policy')}")
    elif mode == "join":
        lines.append(
            f"  joined #{d.get('channel')} as {d.get('peer')} "
            f"(wake={d.get('wake_policy')})"
        )
    elif mode == "posted":
        lines.append(f"  posted #{d.get('id')}")
    elif mode == "read":
        m = _rows(d, "messages")
        seen = [x for x in m if x.get("to_node") == "__channel__"] or m
        lines.append(f"  {len(seen)} message(s)")
        for x in reversed(seen):
            body = (x.get("content") or "").replace("\n", " ")[:100]
            lines.append(
                f"  {x.get('created_at', '')[:16]}  {x.get('from_node', '')}: {body}"
            )
    elif mode == "unread":
        m = _rows(d, "messages")
        lines.append(f"  {len(m)} unread")
        for x in m:
            body = (x.get("content") or "").replace("\n", " ")[:78]
            lines.append(
                f"  #{x.get('id')} {x.get('created_at', '')[:16]} "
                f"{x.get('from_node')}: {body}"
            )
    elif mode in ("cards", "cards-ready"):
        rows = _rows(d, "cards")
        if mode == "cards-ready":
            rows = [c for c in rows if c.get("move_ready")]
        lines.append(f"  {len(rows)} card(s)")
        for c in rows[:40]:
            ready = " READY" if c.get("move_ready") else ""
            pri = c.get("priority")
            p = f"p{pri}" if pri not in (None, 0) else "p0"
            title = (c.get("title") or "").replace("\n", " ")[:56]
            lines.append(
                f"  #{c.get('id')} {p:<4} [{c.get('stage', '?'):<9}] "
                f"{(c.get('assignee') or '-'):<18}{ready}  {title}"
            )
        if len(rows) > 40:
            lines.append(f"  … {len(rows) - 40} more (pass a stage: swarph-me cards build)")
    elif mode == "card":
        if not isinstance(d, dict) or not d.get("id"):
            return json.dumps(d, indent=2)
        ready = " move_ready" if d.get("move_ready") else ""
        lines.append(
            f"  #{d.get('id')} [{d.get('stage')}] p{d.get('priority') or 0}  "
            f"{d.get('assignee') or '-'}{ready}"
        )
        lines.append(f"  {d.get('title')}")
        body = (d.get("body") or "").strip()
        if body:
            for line in body.splitlines()[:8]:
                lines.append(f"    {line[:90]}")
            extra = max(0, len(body.splitlines()) - 8)
            if extra:
                lines.append(f"    … {extra} more lines")
        obl = d.get("open_obligations") or []
        if obl:
            lines.append(
                "  "
                + f"{len(obl)} open obligation(s): "
                + ", ".join(f"#{o.get('id')} {o.get('holder')}" for o in obl[:6])
            )
    elif mode == "thread":
        m = _rows(d, "messages")
        lines.append(f"  {len(m)} on thread")
        for x in m[-15:]:
            body = (x.get("content") or "").replace("\n", " ")[:90]
            lines.append(
                f"  {x.get('created_at', '')[:16]}  {x.get('from_node', '')}: {body}"
            )
    elif mode == "moved":
        lines.append(f"  #{d.get('id')} -> {d.get('stage')}")
    elif mode == "assigned":
        lines.append(f"  #{d.get('id')} assignee -> {d.get('assignee')}")
    elif mode == "ready":
        lines.append(f"  #{d.get('id')} move_ready -> {d.get('move_ready')}")
    elif mode == "sched":
        ev = _rows(d, "events") if isinstance(d, dict) else []
        lines.append(
            f"  {'NAME':<32} {'CRON':<14} {'TARGET':<16} {'FIRES':>6}  LAST"
        )
        for e in sorted(ev, key=lambda x: x.get("name") or ""):
            last = (e.get("last_fired_at") or "never")[:16]
            flag = "" if e.get("enabled") else "  [DISABLED]"
            lines.append(
                f"  {(e.get('name') or '')[:32]:<32} "
                f"{(e.get('cron') or '-'):<14} "
                f"{(e.get('target_cell') or '-'):<16} "
                f"{e.get('fire_count'):>6}  {last}{flag}"
            )
        n = d.get("n", len(ev)) if isinstance(d, dict) else len(ev)
        lines.append(f"\n  {n} events.")
    else:
        return json.dumps(d, indent=2)
    return "\n".join(lines)
