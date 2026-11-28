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
  task '-'). The ordinal counts TAKEN rows only, in creation order
  within that key: rk#1 is the first taken row, 2, 3, ... are rework
  instances. A row with no parseable taken_at (offered, open-untaken,
  withdrawn-untaken) prints rk#- and consumes no ordinal; a row taken
  then later withdrawn keeps the ordinal it earned when taken.
  Grouping by step alone would merge distinct parallel tasks; grouping
  finer (e.g. by accept text) would split one logical rework chain every
  time the check was reworded.
- held(row): closed_at - taken_at, only when BOTH parse AND
  closed_at >= taken_at. A closed-earlier-than-taken row is corrupt
  input: it is flagged BAD-ORDER in the row output and contributes no
  held figure (never a negative, never a 0s). A taken but still-open row
  contributes held-open = as_of - taken_at instead, and that figure is
  reported SEPARATELY (marked *, as-of-dependent) — never folded into
  the closed held total.
- open-held / open-age (the asterisked to-as_of measures) apply ONLY
  when status == 'open'. A fallback_fired row, and any non-open row with
  no closed_at, is never measured as open (held unmeasured instead).
- queue(row): taken_at - created_at, only when BOTH parse. A row never
  taken contributes queue-open = as_of - created_at (marked *).
- commander-held: holder == 'commander' EXACTLY. Rows merely created
  (asked) by commander under another holder do not count.
- peer-day inclusion: the peer TOOK that day (taken_at in
  [day 00:00Z, next 00:00Z) on a row they hold) OR CLOSED that day
  (closed_at in that interval with closed_by == the peer, whatever the
  holder). The obligations list endpoint has no closed_by filter, so
  peer-day is ONE unfiltered GET of the same list URL with a client-side
  filter — no second endpoint. A row both taken and closed that day
  lists ONCE with role took+closed. Sort is by earliest in-day event,
  then id. An unknown peer (no rows held or closed by them anywhere in
  the fetched data) is exit 1 naming the peer; a known peer with no rows
  that day is exit 0 with an empty listing.
- totals count closed and withdrawn SEPARATELY (status == 'withdrawn'
  counts as withdrawn, never as closed); nothing is double-counted.
- every printed field (title, step, task, holder, status, outcome) passes
  through the board readout's sanitize_terminal, then CR/LF are escaped
  to visible \r / \n so every row stays exactly one physical line.
- missing/unparseable timestamps render '-' and exclude that duration
  from totals (counted as unmeasured). Nothing here crashes on them.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from swarph_cli.commands._display import sanitize_terminal as _s


def _ts(value) -> str:
    """Terminal-safe one-line text: escape CR/LF visibly, then sanitize.

    sanitize_terminal strips ESC and other controls but ALSO strips CR
    (0x0d) while keeping LF (0x0a) — so a literal CR would vanish and a
    literal LF would split one row across physical lines. Escaping first
    turns both into visible backslash sequences (which sanitize leaves
    alone), and the subsequent sanitize still removes ESC and the rest.
    """
    text = "" if value is None else str(value)
    return _s(text.replace("\r", "\\r").replace("\n", "\\n"))


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


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(value: str) -> datetime:
    """Strict ``YYYY-MM-DD`` -> that UTC midnight. Raises ValueError."""
    try:
        text = value.strip()
    except AttributeError:
        text = ""
    if not _DAY_RE.fullmatch(text):
        raise ValueError(
            f"bad --day {value!r} (use YYYY-MM-DD, a UTC calendar day)")
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
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
    """Row id -> 1-based ordinal within its (step, task) group, or None.

    Only rows with a parseable taken_at consume ordinals: rk#1 is the
    first TAKEN row in creation order. Untaken rows (offered,
    open-untaken, withdrawn-untaken) map to None (printed rk#-) and never
    shift the ordinals of taken rows; a row taken then withdrawn keeps
    the ordinal it earned.
    """
    seen: dict = {}
    ordinal: dict = {}
    for row in ordered:
        if parse_ts(row.get("taken_at")) is None:
            ordinal[row.get("id")] = None
            continue
        key = row_identity(row)
        seen[key] = seen.get(key, 0) + 1
        ordinal[row.get("id")] = seen[key]
    return ordinal


def time_reversed(row: dict) -> bool:
    """True when closed_at parses AND predates taken_at (corrupt input)."""
    taken = parse_ts(row.get("taken_at"))
    closed = parse_ts(row.get("closed_at"))
    return taken is not None and closed is not None and closed < taken


def held_seconds(row: dict):
    """taken->closed in seconds, else None.

    None covers open rows, half-stamped rows, AND closed-earlier-than-
    taken rows (those are flagged BAD-ORDER at render; no negative and
    no 0s figure is ever computed from them).
    """
    if time_reversed(row):
        return None
    taken = parse_ts(row.get("taken_at"))
    closed = parse_ts(row.get("closed_at"))
    if taken is None or closed is None:
        return None
    return max(0, (closed - taken).total_seconds())


def held_open_seconds(row: dict, as_of: datetime):
    """taken->as_of for a taken but unclosed OPEN row, else None.

    Status-gated: only status == 'open' is ever measured as open. A
    fallback_fired row (or any non-open row with no closed_at) is not an
    open row and yields no open-held figure.
    """
    if row.get("status") != "open":
        return None
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
    """created->as_of for an OPEN row with no closed_at, else None.

    Status-gated like held_open_seconds: only status == 'open' rows
    have an open age.
    """
    if row.get("status") != "open":
        return None
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
    return {"rows": 0, "closed": 0, "withdrawn": 0, "held_s": 0.0,
            "held_n": 0, "held_unmeasured": 0, "open_held_s": 0.0,
            "open_held_n": 0, "queue_s": 0.0, "queue_n": 0,
            "open_age_s": 0.0, "open_n": 0}


def _accumulate(bucket: dict, row: dict, as_of: datetime) -> None:
    bucket["rows"] += 1
    if row.get("status") == "withdrawn":
        # Withdrawn is its own terminal state: counted here, never as
        # closed, even if the row carries timestamps.
        bucket["withdrawn"] += 1
    elif (parse_ts(row.get("closed_at")) is not None
            or row.get("status") == "closed"):
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


def _held_display(row: dict, as_of: datetime) -> str:
    """Row held figure: BAD-ORDER flag, measured held, open-held*, or '-'."""
    if time_reversed(row):
        return "BAD-ORDER(closed<taken)"
    held = held_seconds(row)
    if held is not None:
        return fmt_dur(held)
    open_held = held_open_seconds(row, as_of)
    if open_held is not None:
        return f"{fmt_dur(open_held)}*"
    return "-"


def _bucket_line(name: str, bucket: dict) -> str:
    parts = [f"{bucket['rows']} row(s)",
             f"closed {bucket['closed']}, withdrawn {bucket['withdrawn']}"]
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
    # Bucket names are group keys built from row fields: sanitize them
    # like every other printed field (a hostile step/holder must not
    # reach the terminal raw here either).
    return f"  {_ts(name)}: " + ", ".join(parts)


def render_card(card_id: int, card: dict | None, rows: list,
                as_of: datetime, summary: dict) -> str:
    """Plain-text card history: one line per row, then totals."""
    ordered = sort_rows(rows)
    ordinal = assign_rework(ordered)
    as_of_txt = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    head = f"card #{card_id}"
    if isinstance(card, dict):
        title = _ts(card.get("title") or "")
        if len(title) > 70:
            title = title[:67] + "..."
        head += f" [{_ts(card.get('stage') or '-')}] {title}".rstrip()
    lines = [head + f"  {len(ordered)} obligation row(s), as_of {as_of_txt}"]
    if not ordered:
        lines.append("  (no obligation rows on this card)")
    for row in ordered:
        rk = ordinal.get(row.get("id"))
        rk_txt = f"rk#{rk}" if rk is not None else "rk#-"
        lines.append(
            f"  #{row.get('id')} {rk_txt}"
            f" step={_ts(row.get('step') or '-')}"
            f" task={_ts(row.get('task') or '-')}"
            f" holder={_ts(row.get('holder') or '-')}"
            f" status={_ts(row.get('status') or '-')}"
            f" created={fmt_ts(row.get('created_at'))}"
            f" taken={fmt_ts(row.get('taken_at'))}"
            f" closed={fmt_ts(row.get('closed_at'))}"
            f" outcome={_ts(row_outcome(row))}"
            f" held={_held_display(row, as_of)}"
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
            "closed_by": row.get("closed_by"),
            "rework": ordinal.get(row.get("id")),
            "created_at": row.get("created_at"),
            "taken_at": row.get("taken_at"),
            "closed_at": row.get("closed_at"),
            "outcome": row_outcome(row),
            "time_reversed": time_reversed(row),
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


def peer_day_entries(rows: list, day: datetime, peer: str) -> list:
    """Rows the peer TOOK or CLOSED that UTC day, one entry each.

    Inclusion is taken_at in [day, day+24h) on a row the peer holds, OR
    closed_at in that interval with closed_by == the peer (whatever the
    holder — a row held by someone else but closed by this peer counts).
    A row doing both lists once with role took+closed. Sort is earliest
    in-day event, then id.
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
        took = in_day(row.get("taken_at")) \
            if row.get("holder") == peer else None
        closed = in_day(row.get("closed_at")) \
            if row.get("closed_by") == peer else None
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


def peer_known(rows: list, peer: str) -> bool:
    """True when the peer holds (or ever held/closed) any fetched row.

    Unknown peer = exit 1 naming the peer; a known peer with no rows on
    the requested day is a quiet day (exit 0, empty listing).
    """
    return any(r.get("holder") == peer or r.get("closed_by") == peer
               for r in rows if isinstance(r, dict))


def render_peer(peer: str, day: datetime, entries: list,
                as_of: datetime) -> str:
    """Plain-text peer-day report: one line per row, then a total."""
    day_txt = day.strftime("%Y-%m-%d")
    lines = [f"peer {_ts(peer)} on {day_txt} (UTC): {len(entries)} row(s)"]
    if not entries:
        lines.append("  (no rows taken or closed that day)")
    held_total = 0.0
    held_n = 0
    open_held_total = 0.0
    open_held_n = 0
    unmeasured = 0
    for entry in entries:
        row = entry["row"]
        line_head = (
            f"  #{row.get('id')} card={row.get('card_id')}"
            f" {entry['role']}"
            f" step={_ts(row.get('step') or '-')}"
            f" holder={_ts(row.get('holder') or '-')}")
        line_tail = (
            f" taken={fmt_ts(row.get('taken_at'))}"
            f" closed={fmt_ts(row.get('closed_at'))}"
            f" outcome={_ts(row_outcome(row))}")
        if time_reversed(row):
            unmeasured += 1
            lines.append(line_head + line_tail
                         + " held=BAD-ORDER(closed<taken)")
            continue
        held = held_seconds(row)
        if held is not None:
            held_total += held
            held_n += 1
            lines.append(line_head + line_tail + f" held={fmt_dur(held)}")
            continue
        open_held = held_open_seconds(row, as_of)
        if open_held is not None:
            open_held_total += open_held
            open_held_n += 1
            held_txt = f"{fmt_dur(open_held)}*"
        else:
            unmeasured += 1
            held_txt = "-"
        lines.append(line_head + line_tail + f" held={held_txt}")
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
            "closed_by": e["row"].get("closed_by"),
            "taken_at": e["row"].get("taken_at"),
            "closed_at": e["row"].get("closed_at"),
            "outcome": row_outcome(e["row"]),
            "time_reversed": time_reversed(e["row"]),
            "held_s": held_seconds(e["row"]),
            "held_open_s": held_open_seconds(e["row"], as_of),
        } for e in entries],
    }
