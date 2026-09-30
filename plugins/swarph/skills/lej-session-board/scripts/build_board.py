#!/usr/bin/env python3
"""Build LEJ Session Board JSON from a ListAgents listing and commander rows.

Does not send, and does not publish. A caller that wants the page copies
assets/board-template.html and replaces the board-data JSON.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_SELF = re.compile(r"^This session is (\S+) \[([0-9a-f]{6})\]")
_NAME = re.compile(r"^(.+) \[([0-9a-f]{6})\]$")


def parse_listing(text: str) -> tuple[dict, list[dict]]:
    """Return (manager, peers) from a ListAgents listing."""
    manager = {"name": "", "id": ""}
    peers = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        self_m = _SELF.match(line)
        if self_m:
            manager = {"name": self_m.group(1), "id": self_m.group(2)}
            continue
        if " · " not in line:
            continue
        parts = [p.strip() for p in line.split(" · ")]
        name_m = _NAME.match(parts[0])
        if not name_m or len(parts) < 3:
            continue
        status = parts[2]
        if status not in ("idle", "busy", "offline"):
            continue
        peers.append({
            "name": name_m.group(1),
            "id": name_m.group(2),
            "kind": parts[1],
            "status": status,
            "detail": " · ".join(parts[3:]),
        })
    return manager, peers


def _bucket(peer: dict, waiting: bool) -> str:
    if peer["status"] == "offline":
        return "stale"
    if waiting:
        return "needs_followup"
    return "in_progress"


_IN_SESSION = re.compile(r"hard[- ]gate|\bdeploy", re.IGNORECASE)
_ANSWER = re.compile(r"(?m)^Re: (.+)$")


def _in_session(row: dict) -> bool:
    """A deploy or hard-gate named in the accept is answered inside that session."""
    return _IN_SESSION.search(row.get("accept") or "") is not None


def _questions(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        options = []
        for opt in row.get("options") or []:
            options.append({
                "label": opt["label"],
                "text": opt["text"],
                "rec": bool(opt.get("rec")),
            })
        if not options:
            title = row["title"]
            options = [
                {"label": "Answer on the card", "text": f"Record the commander's decision on card #{row['card_id']}: {title}", "rec": True},
                {"label": "Ask for a narrower question", "text": f"Ask the cell to narrow obligation #{row['obligation_id']} before the commander decides.", "rec": False},
                {"label": "Park it", "text": f"Park card #{row['card_id']} and say what changed.", "rec": False},
            ]
        question = {
            "id": f"obl-{row['obligation_id']}",
            "to": row["cell"],
            "to_node": row["cell"],
            "title": row["title"],
            "note": row.get("note") or f"card #{row['card_id']} obligation #{row['obligation_id']}",
            "in_session": _in_session(row),
            "options": options,
        }
        if row.get("card_id") is not None:
            question["card"] = row["card_id"]
        if row.get("obligation_id") is not None:
            question["obligation"] = row["obligation_id"]
        out.append(question)
    return out


def drop_answered(rows: list[dict], messages: list[str]) -> list[dict]:
    """Drop a row whose title was answered by a DM line `Re: <title>`."""
    answered = set()
    for message in messages:
        for match in _ANSWER.finditer(message or ""):
            answered.add(match.group(1).strip())
    return [row for row in rows if row.get("title") not in answered]


def public_board(board: dict) -> dict:
    """The JSON the commander receives. The sort bucket stays off the wire."""
    public = json.loads(json.dumps(board))
    for sess in public.get("sessions") or []:
        sess.pop("bucket", None)
    return public


def envelope(board: dict) -> str:
    """ONE status DM body: a header line, then the board JSON."""
    return "SWARPH-BOARD v1\n" + json.dumps(public_board(board), indent=2, ensure_ascii=False)


def _stable(body: str) -> str:
    text = body.split("\n", 1)[1] if body.startswith("SWARPH-BOARD v1\n") else body
    data = json.loads(text)
    data.pop("updated", None)
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def publish_if_changed(board: dict, previous: str | None, send) -> bool:
    """Call send(envelope) only when the board JSON changed. The clock is not a change."""
    body = envelope(board)
    if previous is not None and _stable(previous) == _stable(body):
        return False
    send(body)
    return True


def build(listing: str, rows: list[dict], *, project: str = "~/swarph") -> dict:
    manager, peers = parse_listing(listing)
    by_cell: dict[str, list[dict]] = {}
    for row in rows:
        by_cell.setdefault(row["cell"], []).append(row)
    sessions = []
    order = {"needs_followup": 0, "in_progress": 1, "stale": 2}
    ranked = []
    for peer in peers:
        waiting = peer["name"] in by_cell
        bucket = _bucket(peer, waiting)
        ranked.append((order[bucket], peer["name"], peer["id"], bucket, peer))
    for _ord, _name, _sid, bucket, peer in sorted(ranked, key=lambda row: row[:3]):
        qs = _questions(by_cell.get(peer["name"], []))
        if bucket == "stale":
            state = "Offline. Reported here so the commander can archive it in the app."
            tone = "done"
        elif bucket == "needs_followup":
            state = "Waiting on the commander."
            tone = "needs"
        elif peer["status"] == "busy":
            state = "Busy, with a next step of its own."
            tone = ""
        else:
            state = "Idle, with a next step of its own."
            tone = ""
        where = [
            f"{peer['kind']}, {peer['status']}.",
            peer["detail"] or "No further detail on the row.",
        ]
        if bucket == "needs_followup":
            where.append(f"{len(qs)} open row(s) wait on the commander.")
        elif bucket == "stale":
            where.append("This board does not archive it.")
        sessions.append({
            "id": peer["id"],
            "name": peer["name"],
            "state": state,
            "tone": tone,
            "where": where[:4],
            "questions": qs,
            "bucket": bucket,
        })
    groups = [{
        "title": "Your call",
        "items": [
            {"text": f"card #{row['card_id']} obligation #{row['obligation_id']}: {row['title']}"}
            for row in rows
        ],
    }]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "updated": now,
        "projectFolder": project,
        "manager": manager["name"],
        "todoIntro": "Open swarph rows that wait on the commander, one per cell section.",
        "sessions": sessions,
        "groups": groups,
    }


def fill_template(template: str, board: dict) -> str:
    """Replace the JSON inside board-data. The markup on either side stays."""
    start = template.find('<script type="application/json" id="board-data">')
    end = template.find("</script>", start)
    if start < 0 or end < 0:
        raise SystemExit("template has no board-data script")
    open_end = template.find(">", start) + 1
    payload = json.dumps(board, indent=2, ensure_ascii=False)
    return template[:open_end] + "\n" + payload + "\n" + template[end:]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--listing", required=True)
    p.add_argument("--rows", required=True)
    p.add_argument("--project", default="~/swarph")
    args = p.parse_args(argv)
    listing = Path(args.listing).read_text(encoding="utf-8")
    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    print(json.dumps(build(listing, rows, project=args.project), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
