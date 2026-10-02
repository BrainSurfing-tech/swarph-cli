"""``swarph followup`` — local append-only follow-up store (#919).

v1: orchestrators REQUEST a reminder about a COORDINATE at a time. The Intendant
reads ``list --due`` and folds it into its existing push — this verb does NOT
send DMs or write the board.

Store: SQLite under ``$HOME/swarph_state/<self>/followups.sqlite3``. Chosen over
JSONL so a corrupt file fails the schema check loudly (empty vs broken must not
render identically — 2026-09-22 four-hour confusion).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_COORD_RE = re.compile(
    r"^(#\d+|card:\d+|msg:\d+|obligation:\d+|row:\d+)$",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  requested_by TEXT NOT NULL,
  subject TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','fired','cancelled')),
  fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_due
  ON followups(state, fire_at);
"""

_META = "PRAGMA user_version = 1;"


def _resolve_self(name: str | None) -> str:
    if name:
        return name
    env = os.environ.get("SWARPH_SELF", "").strip()
    if env:
        return env
    raise SystemExit(
        "swarph followup: no identity — pass --as <peer> or set $SWARPH_SELF"
    )


def _store_path(self_name: str, override: str | None = None) -> Path:
    if override:
        return Path(override)
    return Path.home() / "swarph_state" / self_name / "followups.sqlite3"


def _connect(path: Path, *, create: bool, writable: bool = False) -> sqlite3.Connection:
    """Open the store. Corrupt/empty-file-that-is-not-a-db REFUSES (exit via raise)."""
    if path.exists() and path.stat().st_size == 0:
        raise RuntimeError(
            f"REFUSE — followup store is an empty file (broken, not empty-of-rows): {path}"
        )
    if path.exists() and not create:
        # Probe: must be a readable sqlite db with our table.
        try:
            mode = "rw" if writable else "ro"
            con = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='followups'"
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        f"REFUSE — followup store missing followups table: {path}"
                    )
                return con
            except sqlite3.DatabaseError as exc:
                con.close()
                raise RuntimeError(
                    f"REFUSE — followup store is corrupt (not a readable sqlite db): "
                    f"{path} ({exc})"
                ) from exc
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"REFUSE — followup store is corrupt (not a readable sqlite db): "
                f"{path} ({exc})"
            ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_SCHEMA)
        con.execute(_META)
        con.commit()
    except sqlite3.DatabaseError as exc:
        con.close()
        raise RuntimeError(
            f"REFUSE — followup store is corrupt: {path} ({exc})"
        ) from exc
    return con


def _parse_fire_at(raw: str) -> str:
    """Normalise to UTC ISO-8601. Accept Z or offset; naive = UTC."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise SystemExit(
            f"swarph followup add: --at must be ISO-8601 (got {raw!r})"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_subject(subject: str) -> str:
    s = subject.strip()
    if not _COORD_RE.match(s):
        raise SystemExit(
            "swarph followup add: REFUSE — subject must be a COORDINATE "
            "(#N, card:N, msg:N, obligation:N, or row:N), never prose. "
            f"got {subject!r}"
        )
    return s


def _cmd_add(args: argparse.Namespace) -> int:
    self_name = _resolve_self(args.as_name)
    subject = _validate_subject(args.subject)
    fire_at = _parse_fire_at(args.at)
    reason = (args.reason or "").strip()
    if not reason:
        raise SystemExit("swarph followup add: --reason is required (non-empty)")
    path = _store_path(self_name, args.store)
    try:
        con = _connect(path, create=True)
    except RuntimeError as exc:
        print(f"swarph followup: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cur = con.execute(
            "INSERT INTO followups "
            "(requested_by, subject, fire_at, reason, created_at, state, fired_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', NULL)",
            (self_name, subject, fire_at, reason, now),
        )
        con.commit()
        row_id = cur.lastrowid
    finally:
        con.close()
    print(
        json.dumps(
            {
                "id": row_id,
                "requested_by": self_name,
                "subject": subject,
                "fire_at": fire_at,
                "reason": reason,
                "created_at": now,
                "state": "open",
                "store": str(path),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    if not args.due:
        print(
            "swarph followup list: v1 only supports --due "
            "(open records with fire_at <= now)",
            file=sys.stderr,
        )
        return 2
    self_name = _resolve_self(args.as_name)
    path = _store_path(self_name, args.store)
    if not path.exists():
        # Empty-of-rows: no store yet. Explicit, exit 0 — NOT the corrupt path.
        print(json.dumps({"store": str(path), "n": 0, "due": [], "empty": True}))
        return 0
    try:
        con = _connect(path, create=False, writable=bool(args.mark_fired))
    except RuntimeError as exc:
        print(f"swarph followup: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = con.execute(
            "SELECT id, requested_by, subject, fire_at, reason, created_at, state, fired_at "
            "FROM followups WHERE state = 'open' AND fire_at <= ? "
            "ORDER BY fire_at ASC, id ASC",
            (now,),
        ).fetchall()
        due = [dict(r) for r in rows]
        if args.mark_fired and due:
            ids = [r["id"] for r in due]
            fired_at = now
            con.executemany(
                "UPDATE followups SET state = 'fired', fired_at = ? WHERE id = ? AND state = 'open'",
                [(fired_at, i) for i in ids],
            )
            con.commit()
            for r in due:
                r["state"] = "fired"
                r["fired_at"] = fired_at
        print(
            json.dumps(
                {
                    "store": str(path),
                    "as_of": now,
                    "n": len(due),
                    "due": due,
                    "empty": False,
                    "marked_fired": bool(args.mark_fired and due),
                },
                sort_keys=True,
            )
        )
    finally:
        con.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph followup",
        description=(
            "Local append-only follow-up store (#919). "
            "add a coordinate+time; list --due for the Intendant. "
            "Does not send DMs or write the board."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="append an open follow-up")
    add.add_argument(
        "--subject",
        required=True,
        help="coordinate only: #N | card:N | msg:N | obligation:N | row:N",
    )
    add.add_argument("--at", required=True, help="fire_at UTC ISO-8601")
    add.add_argument("--reason", required=True, help="why the reminder was requested")
    add.add_argument("--as", dest="as_name", default=None, help="peer identity")
    add.add_argument(
        "--store",
        default=None,
        help="override store path (tests); default $HOME/swarph_state/<self>/followups.sqlite3",
    )

    lst = sub.add_parser("list", help="list follow-ups")
    lst.add_argument(
        "--due",
        action="store_true",
        help="open records with fire_at <= now (v1: required)",
    )
    lst.add_argument(
        "--mark-fired",
        action="store_true",
        help="after listing, mark those rows fired (fire-once for the Intendant)",
    )
    lst.add_argument("--as", dest="as_name", default=None, help="peer identity")
    lst.add_argument("--store", default=None, help="override store path (tests)")
    return p


def run_followup(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "list":
        return _cmd_list(args)
    parser.error(f"unknown command: {args.command}")
    return 2
