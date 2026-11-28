"""Card #1445 — ``swarph usage timeline``: read-only board-obligation history.

Two mutually exclusive modes over the SAME reads the board verbs use
(GET /board/obligations, GET /board/cards/{id}). This module never writes:
it performs no POST/PATCH/PUT/DELETE and imports no write helper — the
no-write regression test pins that.

DEFINITIONS (repeated in --help and the PR body):

- order: rows sort by (created_at, id). A row without a parseable
  created_at sorts AFTER every dated row, ordered by id among itself.
- identity / rework ordinal: the grouping key is (step, task) — the
  strongest work identity the rows carry (two live rows of one step need
  different task ids, card #1075; a row with no task groups under
  task '-'). The ordinal is the 1-based position in creation order
  within that key. Ordinal 1 is the original work; 2, 3, ... are rework
  instances. Grouping by step alone would merge distinct parallel tasks;
  grouping finer (e.g. by accept text) would split one logical rework
  chain every time the check was reworded.
- held(row): closed_at - taken_at, only when BOTH parse. A taken but
  still-open row contributes held-open = as_of - taken_at instead, and
  that figure is reported SEPARATELY (marked *, as-of-dependent) — never
  folded into the closed held total.
- queue(row): taken_at - created_at, only when BOTH parse. A row never
  taken contributes queue-open = as_of - created_at (marked *).
- open age: as_of - created_at for rows with no closed_at (marked *).
- commander-held: holder == 'commander' EXACTLY. Rows merely created
  (asked) by commander under another holder do not count.
- peer-day inclusion: taken_at in [day 00:00Z, next 00:00Z) OR closed_at
  in that interval. A row both taken and closed that day lists ONCE with
  role took+closed. Sort is by earliest in-day event, then id.
- missing/unparseable timestamps render '-' and exclude that duration
  from totals (counted as unmeasured). Nothing here crashes on them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_ts(value) -> datetime | None:
    """Tolerant ISO parse -> aware UTC datetime, or None. Never raises."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_ts(value) -> str:
    """Normalized UTC ``YYYY-MM-DDTHH:MM:SSZ``, or '-' when missing."""
    parsed = parse_ts(value)
    if parsed is None:
        return "-"
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_day(value: str) -> datetime:
    """``YYYY-MM-DD`` -> that UTC midnight. Raises ValueError when bad."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        raise ValueError(
            f"bad --day {value!r} (use YYYY-MM-DD, a UTC calendar day)")


def fmt_dur(seconds) -> str:
    """Compact duration (1d02h, 3h20m, 45m, 30s); '-' when None."""
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        if minutes:
            return f"{days}d{hours:02d}h{minutes:02d}m"
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"


def row_identity(row: dict) -> tuple:
    """Strongest work identity a row carries: (step, task)."""
    step = row.get("step") or "-"
    task = row.get("task") or "-"
    return (str(step), str(task))


def sort_rows(rows: list) -> list:
    """Deterministic chronological order: (created_at, id); undated last."""

    def key(row):
        created = parse_ts(row.get("created_at"))
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            rid = 0
        if created is None:
            return (1, datetime.max.replace(tzinfo=timezone.utc), rid)
        return (0, created, rid)

    return sorted(rows, key=key)


def assign_rework(ordered: list) -> dict:
    """Row id -> 1-based ordinal within its (step, task) creation order."""
    seen: dict = {}
    ordinal: dict = {}
    for row in ordered:
        key = row_identity(row)
        seen[key] = seen.get(key, 0) + 1
        ordinal[row.get("id")] = seen[key]
    return ordinal


def held_seconds(row: dict):
    """taken->closed in seconds, else None (open or half-stamped rows)."""
    taken = parse_ts(row.get("taken_at"))
    closed = parse_ts(row.get("closed_at"))
    if taken is None or closed is None:
        return None
    return max(0, (closed - taken).total_seconds())


def held_open_seconds(row: dict, as_of: datetime):
    """taken->as_of for a taken but unclosed row, else None."""
    if parse_ts(row.get("closed_at")) is not None:
        return None
    taken = parse_ts(row.get("taken_at"))
    if taken is None:
        return None
    return max(0, (as_of - taken).total_seconds())


def queue_seconds(row: dict):
    """created->taken in seconds, else None (never taken or half-stamped)."""
    created = parse_ts(row.get("created_at"))
    taken = parse_ts(row.get("taken_at"))
    if created is None or taken is None:
        return None
    return max(0, (taken - created).total_seconds())


def open_age_seconds(row: dict, as_of: datetime):
    """created->as_of for a row with no closed_at, else None."""
    if parse_ts(row.get("closed_at")) is not None:
        return None
    created = parse_ts(row.get("created_at"))
    if created is None:
        return None
    return max(0, (as_of - created).total_seconds())


def row_outcome(row: dict) -> str:
    """Outcome display: '-' while open, stored outcome, else none-recorded.

    A NULL outcome on a closed row is a pre-#562 legacy close — named
    rather than printed blank (the board obligations readout convention).
    """
    outcome = row.get("close_outcome")
    if outcome:
        return str(outcome)
    if parse_ts(row.get("closed_at")) is None and row.get("status") != "closed":
        return "-"
    return "none-recorded"


def _new_bucket():
    return {"rows": 0, "closed": 0, "held_s": 0.0, "held_n": 0,
            "held_unmeasured": 0, "open_held_s": 0.0, "open_held_n": 0,
            "queue_s": 0.0, "queue_n": 0, "open_age_s": 0.0, "open_n": 0}


def _accumulate(bucket: dict, row: dict, as_of: datetime) -> None:
    bucket["rows"] += 1
    if parse_ts(row.get("closed_at")) is not None or row.get("status") == "closed":
        bucket["closed"] += 1
    held = held_seconds(row)
    if held is not None:
        bucket["held_s"] += held
        bucket["held_n"] += 1
    else:
        open_held = held_open_seconds(row, as_of)
        if open_held is not None:
            bucket["open_held_s"] += open_held
            bucket["open_held_n"] += 1
        elif row.get("taken_at") or row.get("closed_at"):
            # Half-stamped: a start or an end exists but no duration can be
            # built from it (a close with no take, or garbage text). Taken
            # but still-open rows are covered by open-held above, never here.
            bucket["held_unmeasured"] += 1
    queue = queue_seconds(row)
    if queue is not None:
        bucket["queue_s"] += queue
        bucket["queue_n"] += 1
    age = open_age_seconds(row, as_of)
    if age is not None:
        bucket["open_age_s"] += age
        bucket["open_n"] += 1


def summarize(rows: list, as_of: datetime) -> dict:
    """Per-step, per-holder buckets plus the commander-held bucket."""
    by_step: dict = {}
    by_holder: dict = {}
    commander = _new_bucket()
    for row in rows:
        step = str(row.get("step") or "-")
        holder = str(row.get("holder") or "-")
        by_step.setdefault(step, _new_bucket())
        by_holder.setdefault(holder, _new_bucket())
        _accumulate(by_step[step], row, as_of)
        _accumulate(by_holder[holder], row, as_of)
        if row.get("holder") == "commander":
            _accumulate(commander, row, as_of)
    return {"by_step": by_step, "by_holder": by_holder, "commander": commander,
            "rows": len(rows)}


def _bucket_line(name: str, bucket: dict) -> str:
    parts = [f"{bucket['rows']} row(s)", f"closed {bucket['closed']}"]
    parts.append(f"held {fmt_dur(bucket['held_s'])}"
                 f" over {bucket['held_n']} measured"
                 + (f", {bucket['held_unmeasured']} unmeasured"
                    if bucket["held_unmeasured"] else ""))
    if bucket["open_held_n"]:
        parts.append(f"open-held* {fmt_dur(bucket['open_held_s'])}"
                     f" over {bucket['open_held_n']}")
    if bucket["queue_n"]:
        parts.append(f"queue {fmt_dur(bucket['queue_s'])}"
                     f" over {bucket['queue_n']}")
    if bucket["open_n"]:
        parts.append(f"open-age* {fmt_dur(bucket['open_age_s'])}"
                     f" over {bucket['open_n']}")
    return f"  {name}: " + ", ".join(parts)


def render_card(card_id: int, card: dict | None, rows: list,
                as_of: datetime, summary: dict) -> str:
    """Plain-text card history: one line per row, then totals."""
    ordered = sort_rows(rows)
    ordinal = assign_rework(ordered)
    as_of_txt = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    head = f"card #{card_id}"
    if isinstance(card, dict):
        title = str(card.get("title") or "").replace("\n", " ")
        if len(title) > 70:
            title = title[:67] + "..."
        head += f" [{card.get('stage') or '-'}] {title}".rstrip()
    lines = [head + f"  {len(ordered)} obligation row(s), as_of {as_of_txt}"]
    if not ordered:
        lines.append("  (no obligation rows on this card)")
    for row in ordered:
        rk = ordinal.get(row.get("id"), 1)
        held = held_seconds(row)
        if held is None:
            open_held = held_open_seconds(row, as_of)
            held_txt = (f"{fmt_dur(open_held)}*" if open_held is not None
                        else "-")
        else:
            held_txt = fmt_dur(held)
        lines.append(
            f"  #{row.get('id')} rk#{rk}"
            f" step={row.get('step') or '-'}"
            f" task={row.get('task') or '-'}"
            f" holder={row.get('holder') or '-'}"
            f" status={row.get('status') or '-'}"
            f" created={fmt_ts(row.get('created_at'))}"
            f" taken={fmt_ts(row.get('taken_at'))}"
            f" closed={fmt_ts(row.get('closed_at'))}"
            f" outcome={row_outcome(row)}"
            f" held={held_txt}"
            f" queue={fmt_dur(queue_seconds(row))}")
    lines.append("totals by step:")
    if summary["by_step"]:
        for step in sorted(summary["by_step"]):
            lines.append(_bucket_line(step, summary["by_step"][step]))
    else:
        lines.append("  (none)")
    lines.append("totals by holder:")
    if summary["by_holder"]:
        for holder in sorted(summary["by_holder"]):
            lines.append(_bucket_line(holder, summary["by_holder"][holder]))
    else:
        lines.append("  (none)")
    lines.append("commander-held (holder == 'commander'):")
    lines.append(_bucket_line("commander", summary["commander"])
                 if summary["commander"]["rows"]
                 else "  (no commander-held rows)")
    if any(b["open_held_n"] or b["open_n"]
           for b in list(summary["by_step"].values())
           + list(summary["by_holder"].values())):
        lines.append(f"* open-held/open-age measured to as_of {as_of_txt}")
    return "\n".join(lines)


def card_to_json(card_id: int, rows: list, as_of: datetime,
                 summary: dict) -> dict:
    """The same card history as JSON."""
    ordered = sort_rows(rows)
    ordinal = assign_rework(ordered)
    out_rows = []
    for row in ordered:
        out_rows.append({
            "id": row.get("id"), "card_id": row.get("card_id"),
            "step": row.get("step"), "task": row.get("task"),
            "holder": row.get("holder"), "status": row.get("status"),
            "rework": ordinal.get(row.get("id"), 1),
            "created_at": row.get("created_at"),
            "taken_at": row.get("taken_at"),
            "closed_at": row.get("closed_at"),
            "outcome": row_outcome(row),
            "held_s": held_seconds(row),
            "held_open_s": held_open_seconds(row, as_of),
            "queue_s": queue_seconds(row),
            "open_age_s": open_age_seconds(row, as_of),
        })
    return {"card_id": card_id, "as_of": as_of.isoformat(),
            "rows": out_rows,
            "totals": {"by_step": summary["by_step"],
                       "by_holder": summary["by_holder"],
                       "commander": summary["commander"]}}


def peer_day_entries(rows: list, day: datetime) -> list:
    """Rows the peer TOOK or CLOSED that UTC day, one entry each.

    Inclusion is taken_at in [day, day+24h) OR closed_at in that
    interval. A row doing both lists once with role took+closed. Sort is
    earliest in-day event, then id.
    """
    start = day
    end = day + timedelta(days=1)

    def in_day(value) -> datetime | None:
        parsed = parse_ts(value)
        if parsed is not None and start <= parsed < end:
            return parsed
        return None

    entries = []
    for row in rows:
        took = in_day(row.get("taken_at"))
        closed = in_day(row.get("closed_at"))
        if took is None and closed is None:
            continue
        if took is not None and closed is not None:
            role = "took+closed"
            event = min(took, closed)
        elif took is not None:
            role = "took"
            event = took
        else:
            role = "closed"
            event = closed
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            rid = 0
        entries.append({"row": row, "role": role, "event": event, "id": rid})
    entries.sort(key=lambda e: (e["event"], e["id"]))
    return entries


def render_peer(peer: str, day: datetime, entries: list,
                as_of: datetime) -> str:
    """Plain-text peer-day report: one line per row, then a total."""
    day_txt = day.strftime("%Y-%m-%d")
    lines = [f"peer {peer} on {day_txt} (UTC): {len(entries)} row(s)"]
    if not entries:
        lines.append("  (no rows taken or closed that day)")
    held_total = 0.0
    held_n = 0
    open_held_total = 0.0
    open_held_n = 0
    unmeasured = 0
    for entry in entries:
        row = entry["row"]
        held = held_seconds(row)
        if held is not None:
            held_total += held
            held_n += 1
            held_txt = fmt_dur(held)
            lines.append(
                f"  #{row.get('id')} card={row.get('card_id')}"
                f" {entry['role']}"
                f" step={row.get('step') or '-'}"
                f" taken={fmt_ts(row.get('taken_at'))}"
                f" closed={fmt_ts(row.get('closed_at'))}"
                f" outcome={row_outcome(row)}"
                f" held={held_txt}")
            continue
        open_held = held_open_seconds(row, as_of)
        if open_held is not None:
            open_held_total += open_held
            open_held_n += 1
            held_txt = f"{fmt_dur(open_held)}*"
        else:
            unmeasured += 1
            held_txt = "-"
        lines.append(
            f"  #{row.get('id')} card={row.get('card_id')}"
            f" {entry['role']}"
            f" step={row.get('step') or '-'}"
            f" taken={fmt_ts(row.get('taken_at'))}"
            f" closed={fmt_ts(row.get('closed_at'))}"
            f" outcome={row_outcome(row)}"
            f" held={held_txt}")
    total = f"total held {fmt_dur(held_total)} over {held_n} measured"
    if open_held_n:
        total += f"; open-held* {fmt_dur(open_held_total)} over {open_held_n}"
    if unmeasured:
        total += f" ({unmeasured} unmeasured)"
    lines.append(total)
    if open_held_n:
        lines.append(f"* open-held measured to as_of "
                     f"{as_of.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return "\n".join(lines)


def peer_to_json(peer: str, day: datetime, entries: list,
                 as_of: datetime) -> dict:
    """The same peer-day report as JSON."""
    return {
        "peer": peer, "day": day.strftime("%Y-%m-%d"),
        "as_of": as_of.isoformat(),
        "rows": [{
            "id": e["row"].get("id"), "card_id": e["row"].get("card_id"),
            "role": e["role"], "step": e["row"].get("step"),
            "holder": e["row"].get("holder"),
            "taken_at": e["row"].get("taken_at"),
            "closed_at": e["row"].get("closed_at"),
            "outcome": row_outcome(e["row"]),
            "held_s": held_seconds(e["row"]),
            "held_open_s": held_open_seconds(e["row"], as_of),
        } for e in entries],
    }
