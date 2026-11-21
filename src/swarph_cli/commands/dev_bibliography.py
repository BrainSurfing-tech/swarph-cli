"""Local bibliography sources for the card-scoped dev-extract (#1726)."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from swarph_cli.commands import timeline
from swarph_cli.commands.dev_extract import ROW_LIMIT, _one_line, _parse_instant
from swarph_cli.redact import redact_text

DESKS = None  # Resolve HOME at invocation time, as timeline does.
HISTORY_LIMIT = 1000
_HEADING = re.compile(r"(?:# )?bibliography v[1-9][0-9]*")
_CARD_LINK = re.compile(r"(?:^|\s)(?:card:)?#([0-9]+)\b|/board/cards/([0-9]+)\b")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class Unavailable(Exception):
    pass


def _clean(text, limit=240):
    return _one_line(redact_text(_CTRL.sub("", str(text)), caller="dev-extract") or "", limit)


def _rows(payload, key):
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise Unavailable(f"gateway did not return {key}")
    if any(not isinstance(row, dict) for row in payload[key]):
        raise Unavailable(f"gateway returned invalid {key} rows")
    return payload[key]


def _next_version(read, thread_url):
    posts = _rows(read(thread_url), "messages")
    if len(posts) >= HISTORY_LIMIT:
        raise Unavailable("card thread reached the gateway history limit")
    if any(not isinstance(post.get("content"), str) for post in posts):
        raise Unavailable("card thread has unreadable message content")
    return 1 + sum(bool(_HEADING.fullmatch(post["content"].partition("\n")[0].strip()))
                   for post in posts)


def _lifetime(card, now):
    start = _parse_instant(card.get("created_at"))
    end = now
    if card.get("stage") == "done":
        done = [_parse_instant(step.get("at")) for step in (card.get("stage_history") or [])
                if isinstance(step, dict) and step.get("stage") == "done"]
        end = next((when for when in reversed(done) if when is not None), None)
    if start is None or end is None or end < start:
        return None
    return start, end


def _links(card):
    return {int(left or right) for value in (card.get("links") or {}).values()
            if isinstance(value, str) for left, right in _CARD_LINK.findall(value)}


def _board_entries(card, payload, post_safe):
    neighbors = _links(card)
    for row in _rows(payload, "cards"):
        ident = row.get("id")
        if type(ident) is not int or ident == card["id"]:
            continue
        # The subject's readers need not have the author's other project grants.
        if post_safe and row.get("project_id") != card.get("project_id"):
            continue
        if ident in neighbors or card["id"] in _links(row):
            obligations = row.get("open_obligations")
            if obligations is None:
                opened = "unavailable"
            elif not obligations:
                opened = "floor 0"
            else:
                opened = ", ".join(f"#{r['id']}" for r in obligations[:ROW_LIMIT])
                if len(obligations) > ROW_LIMIT:
                    opened += f" (+{len(obligations) - ROW_LIMIT} more)"
            yield (f"card:#{ident}", f"{_clean(row.get('title') or '(untitled)')} "
                   f"· stage={_clean(row.get('stage') or 'unknown', 24)} · open rows: {opened}")


def _timeline_entries(lifetime):
    try:
        entries = timeline.load_entries(timeline._timeline_path())
    except FileNotFoundError:
        raise Unavailable("timeline file is missing") from None
    start, end = lifetime
    for entry in reversed(entries):
        if start <= entry.ts <= end:
            when = entry.ts.astimezone(timezone.utc)
            if entry.links:
                key = f"tl:{when.date()}/{entry.links[0]}"
            else:
                key = f"tl:{when.strftime('%Y-%m-%dT%H:%MZ')}/{entry.cell}"
            yield key, _clean(entry.text)


def _matches_card(row, card):
    thread = card.get("thread_uuid")
    coordinate = f"#{card['id']}"
    return (bool(thread and row.get("thread_id") == thread)
            or any(annot.get("status") == "resolved"
                   and annot.get("token") == coordinate
                   for annot in (row.get("annotations") or {}).get("cards", [])))


def _thread_entries(card, lifetime):
    start, end = lifetime
    root = DESKS or Path.home() / "secretary-consumer" / "runs" / "daily"
    runs = sorted(root.glob("*/annotated.jsonl"), reverse=True)
    if not runs:
        raise Unavailable("no annotated secretary desks")
    for run in runs:
        with run.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("record") == "manifest":
                    generated = _parse_instant(row.get("generated_at"))
                    if generated:
                        # An end-of-day desk can include messages from earlier days.
                        if generated < start:
                            break
                    continue
                when = _parse_instant(row.get("created_at"))
                ident = row.get("id")
                if (when is not None and start <= when <= end and type(ident) is int
                        and _matches_card(row, card)):
                    yield (f"dm:{ident}", f"{_clean(row.get('from') or '?', 64)} "
                           f"{_clean(row.get('kind') or '?', 24)} {_clean(row['created_at'], 40)}")


def _memory_results(query):
    # search can embed and rerank just like query (lab ruling #1726 / 102196).
    # shortcut: no verified keyword-only route, use the pinned Phase B route once shipped.
    raise Unavailable("keyword-only not assured on the deployed gbrain (card #1726 phase B)")


def _semantic_entries(card):
    query = "\n".join(_clean(card.get(field) or "", 2048) for field in ("title", "body")).strip()
    if not query:
        return
    for row in _memory_results(query):
        if row.get("slug"):
            yield f"mem:{row['slug']}", _clean(row.get("description") or row.get("title") or "")


def _section(name, as_of, find):
    entries = {}
    try:
        for key, description in find():
            entries.setdefault(_clean(key), description)
            if len(entries) == ROW_LIMIT:
                break
    except Unavailable as exc:
        return {"name": name, "as_of": as_of, "unavailable": _clean(exc), "entries": []}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"name": name, "as_of": as_of, "unavailable": "source unreadable or invalid", "entries": []}
    return {"name": name, "as_of": as_of,
            "entries": [{"key": key, "description": val} for key, val in entries.items()]}


def collect(card, read, thread_url, *, post_safe=False):
    now = datetime.now(timezone.utc)
    as_of = now.isoformat(timespec="seconds")
    lifetime = _lifetime(card, now)

    def dated(find):
        if lifetime is None:
            raise Unavailable("card lifetime unavailable")
        return find()

    try:
        version, reason = _next_version(read, thread_url), None
    except Unavailable as exc:
        version, reason = None, str(exc)
    return {"version": version, "version_unavailable": reason, "as_of": as_of, "sections": [
        _section("semantic", as_of, lambda: _semantic_entries(card)),
        _section("timeline", as_of, lambda: dated(lambda: _timeline_entries(lifetime))),
        _section("board", as_of, lambda: _board_entries(card, read("/board/cards"), post_safe)),
        _section("thread", as_of, lambda: dated(lambda: _thread_entries(card, lifetime))),
    ]}


def redact_json(value):
    if isinstance(value, dict):
        return {redact_json(k): redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    return redact_text(value, caller="dev-extract") if isinstance(value, str) else value


def render(extract, bibliography):
    version = bibliography["version"]
    header = (f"bibliography v{version}" if version is not None else
              f"bibliography (version unavailable: {bibliography['version_unavailable']})")
    extract = redact_text(_CTRL.sub("", extract), caller="dev-extract") or ""
    # Keep whole redacted lines; leave ample room below both gateway size limits.
    if len(json.dumps(extract).encode("utf-8")) > 96_000:
        out, used = [], 0
        for line in extract.splitlines():
            used += len(json.dumps(line))
            if used > 96_000:
                break
            out.append(line)
        extract = "\n".join(out) + "\ncut: legacy extract exceeded the post budget\n"
    lines = [header, "", extract.rstrip()]
    for section in bibliography["sections"]:
        lines += ["", f"## {section['name']} (as of {section['as_of']})"]
        if section.get("unavailable"):
            lines.append(f"unavailable: {section['unavailable']}")
        elif not section["entries"]:
            lines.append("floor: 0 entries in the available source")
        else:
            for entry in section["entries"]:
                lines.append(f"- {entry['key']} — {entry['description']}")
    return "\n".join(lines) + "\n"
