#!/usr/bin/env python3
"""Publish the SWARPH-BOARD from open [commander] rows.

Roster comes from `swarph monitor status` plus `tmux has-session`.
The send uses `--token-file` (a path).
A self-send is refused before any runner is called.

The unit template is not installed by this module.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_board  # noqa: E402


class SelfSend(RuntimeError):
    """The service identity would DM itself."""


class ReadFailed(RuntimeError):
    """A gateway read failed. The timer must send nothing."""


# tmux session names are not always the cell name.
TMUX_FOR_CELL = {"lab-ovh": "lab"}
READ_AS = "lab-ovh"


def _is_path(value: str) -> bool:
    text = str(value or "")
    return "/" in text or "\\" in text


_TAG = "[commander]"
_IN_SESSION_MARK = "[in-session]"


def parse_monitor_status(text: str) -> dict:
    """One `swarph monitor status` report."""
    name = ""
    running = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("monitor ") and ":" in line:
            head, tail = line.split(":", 1)
            name = head[len("monitor "):].strip()
            running = tail.strip().startswith("running") and not tail.strip().startswith("not running")
            break
    supervisor = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("supervised by:"):
            supervisor = line.split(":", 1)[1].strip()
            break
    return {"name": name, "running": running, "supervisor": supervisor}


def tmux_session(cell: str) -> str:
    return TMUX_FOR_CELL.get(cell, cell)


def _supervised(supervisor: str, cell: str) -> bool:
    text = supervisor or ""
    if "swarph-monitor.service" in text and "swarph-monitor@" not in text:
        return True
    return f"swarph-monitor@{cell}.service" in text or "swarph-monitor.service" in text


def commander_title(accept: str) -> str | None:
    """Title only when the accept starts with the tag. A later mention is prose."""
    text = accept or ""
    if not text.startswith(_TAG):
        return None
    rest = text[len(_TAG):].lstrip()
    if rest.startswith(_IN_SESSION_MARK):
        rest = rest[len(_IN_SESSION_MARK):].lstrip()
    cut = len(rest)
    for marker in ("|", "PASS="):
        found = rest.find(marker)
        if found >= 0:
            cut = min(cut, found)
    return rest[:cut].strip()


def commander_rows(rows: list[dict]) -> list[dict]:
    """Open GET /board/obligations rows whose accept starts with [commander].

    The real row has id, holder, card_id, accept, and state. It has no title,
    no cell, and no obligation_id. The holder is the cell that gets the answer.
    """
    kept = []
    for row in rows:
        state = str(row.get("state") or row.get("status") or "open")
        if state.startswith("closed"):
            continue
        title = commander_title(row.get("accept") or "")
        if title is None:
            continue
        holder = row.get("holder") or row.get("cell") or ""
        kept.append({
            "title": title,
            "accept": row.get("accept") or "",
            "cell": holder,
            "holder": holder,
            "card_id": row.get("card_id"),
            "obligation_id": row.get("id", row.get("obligation_id")),
            "state": state,
        })
    return kept


def build_roster(roster: list[dict], rows: list[dict], *, project: str = "~/swarph", manager: str = "board-publisher") -> dict:
    """Sessions from monitor status and tmux. Questions are the commander rows."""
    by_cell: dict[str, list[dict]] = {}
    for row in rows:
        by_cell.setdefault(row["cell"], []).append(row)
    seen = {item.get("name") or "" for item in roster}
    sessions = []
    for item in roster:
        name = item.get("name") or ""
        if not name:
            continue
        qs = build_board._questions(by_cell.get(name, []))
        unit = _supervised(str(item.get("supervisor") or ""), name)
        up = bool(item.get("running") and item.get("tmux") and unit)
        if up and qs:
            state, tone = "Waiting on the commander.", "needs"
        elif up:
            state, tone = "Monitor is running and the tmux session is present.", ""
        else:
            state, tone = "Not unit-supervised, or the tmux session is absent.", "done"
        sessions.append({
            "id": name,
            "name": name,
            "state": state,
            "tone": tone,
            "where": [
                f"monitor {'running' if item.get('running') else 'not running'}.",
                f"tmux {'present' if item.get('tmux') else 'absent'}.",
                item.get("supervisor") or "no supervisor recorded.",
            ],
            "questions": qs,
        })
    for cell, cell_rows in by_cell.items():
        if cell in seen:
            continue
        sessions.append({
            "id": cell,
            "name": cell,
            "state": "No monitor status for this cell.",
            "tone": "needs",
            "where": ["The question is open.", "The cell was not in the monitor roster."],
            "questions": build_board._questions(cell_rows),
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
        "manager": manager,
        "todoIntro": "Open obligation rows tagged [commander].",
        "sessions": sessions,
        "groups": groups,
    }


def attach_tmux(roster: list[dict], tmux_sessions: set[str]) -> list[dict]:
    out = []
    for item in roster:
        row = dict(item)
        row["tmux"] = tmux_session(row.get("name") or "") in tmux_sessions
        out.append(row)
    return out


def send_argv(*, token_file: str, sender: str, recipient: str, content_file: str) -> list[str]:
    """Argv for the send. The token file is a path. Its contents are not read."""
    if sender == recipient:
        raise SelfSend(f"{sender} would send the board to itself")
    path = str(token_file or "")
    if not _is_path(path):
        raise ValueError("token-file must be a path, not a token value")
    return [
        "swarph", "mesh", "send", recipient,
        "--as", sender,
        "--kind", "status",
        "--token-file", path,
        "--content-file", content_file,
    ]


def run_once(
    *,
    rows: list[dict],
    messages: list[tuple[str, str]],
    statuses: list[str],
    tmux_sessions: set[str],
    previous: str | None,
    commander: str,
    sender: str,
    recipient: str,
    token_file: str,
    send,
) -> tuple[bool, str]:
    """Build one board and send only when it changed. send sees the path, not the token.

    ``messages`` is the manual fixture only (``--messages-file``). The timer
    does not pass replies. A live question leaves when its obligation row closes.
    """
    if sender == recipient:
        raise SelfSend(f"{sender} would send the board to itself")
    path = str(token_file or "")
    if not _is_path(path):
        raise ValueError("token-file must be a path, not a token value")
    roster = attach_tmux(
        [parse_monitor_status(text) for text in statuses],
        set(tmux_sessions),
    )
    open_rows = commander_rows(rows)
    remaining = build_board.drop_answered(open_rows, messages, commander=commander)
    board = build_roster(roster, remaining, manager=sender)

    def _send(body: str) -> None:
        send(body, token_file=path, sender=sender, recipient=recipient)

    return build_board.publish_if_changed(board, previous, _send), build_board.envelope(board)


def read_argv(*, cells: list[str], read_token_file: str, read_as: str = READ_AS) -> list[list[str]]:
    """Reads only. Monitor status, tmux, and open obligations. No inbox."""
    path = str(read_token_file or "")
    if not _is_path(path):
        raise ValueError("read token-file must be a path, not a token value")
    argv = []
    for cell in cells:
        argv.append(["swarph", "monitor", "status", "--as", cell])
        argv.append(["tmux", "has-session", "-t", tmux_session(cell)])
    argv.append([
        "swarph", "board", "obligations", "list",
        "--as", read_as,
        "--token-file", path,
        "--status", "open",
        "--json",
    ])
    return argv


def _require_json(proc, label: str):
    if getattr(proc, "returncode", 0):
        raise ReadFailed(f"{label} exited {proc.returncode}")
    text = (getattr(proc, "stdout", None) or "").strip()
    if not text:
        raise ReadFailed(f"{label} returned empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReadFailed(f"{label} returned invalid JSON") from exc


def live_once(
    *,
    cells: list[str],
    read_token_file: str,
    sender: str,
    recipient: str,
    token_file: str,
    commander: str,
    previous: str | None,
    runner,
    send,
    messages: list[tuple[str, str]] | None = None,
) -> bool:
    """One timer pass. Reads no inbox. A question leaves only when its row closes.

    ``messages`` is ignored. The manual ``--messages-file`` fixture is the only
    Re: path, and the timer does not call it.
    """
    del messages
    statuses = []
    tmux_sessions = set()
    rows: list[dict] = []
    saw_list = False
    for argv in read_argv(cells=cells, read_token_file=read_token_file):
        joined = " ".join(argv)
        if "inbox" in argv or "/messages" in joined:
            raise ReadFailed("the timer must not read the inbox")
        if argv[:3] == ["swarph", "monitor", "status"]:
            statuses.append(runner(argv).stdout or "")
        elif argv[:2] == ["tmux", "has-session"]:
            if runner(argv).returncode == 0:
                tmux_sessions.add(argv[-1])
        else:
            payload = _require_json(runner(argv), "obligations list")
            rows = payload if isinstance(payload, list) else payload.get("obligations") or []
            saw_list = True
    if not saw_list:
        raise ReadFailed("obligations list was not read")
    changed, _body = run_once(
        rows=rows,
        messages=[],
        statuses=statuses,
        tmux_sessions=tmux_sessions,
        previous=previous,
        commander=commander,
        sender=sender,
        recipient=recipient,
        token_file=token_file,
        send=send,
    )
    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token-file", required=True)
    p.add_argument("--as", dest="sender", required=True)
    p.add_argument("--to", dest="recipient", required=True)
    p.add_argument("--commander", default="commander")
    p.add_argument("--rows-file")
    p.add_argument("--messages-file", help="manual fixture only; the timer does not read replies")
    p.add_argument("--status-file")
    p.add_argument("--tmux", default="")
    p.add_argument("--previous-file")
    p.add_argument("--live", action="store_true")
    p.add_argument("--read-token-file")
    p.add_argument("--cells", default="")
    p.add_argument("--state-file")
    args = p.parse_args(argv)
    if args.sender == args.recipient:
        print(f"{args.sender} would send the board to itself", file=sys.stderr)
        return 2
    if args.live:
        return _main_live(args)
    if not args.rows_file:
        print("pass --rows-file; this entry does not DM a live cell", file=sys.stderr)
        return 2
    rows = json.loads(Path(args.rows_file).read_text(encoding="utf-8"))
    messages = []
    if args.messages_file:
        for item in json.loads(Path(args.messages_file).read_text(encoding="utf-8")):
            messages.append((item[0], item[1]))
    status_text = Path(args.status_file).read_text(encoding="utf-8") if args.status_file else ""
    statuses = [block for block in status_text.split("\n---\n") if block.strip()]
    previous = None
    if args.previous_file and Path(args.previous_file).is_file():
        previous = Path(args.previous_file).read_text(encoding="utf-8")
    sent: list[str] = []

    def _send(body: str, **_kwargs) -> None:
        sent.append(body)

    changed, _body = run_once(
        rows=rows,
        messages=messages,
        statuses=statuses,
        tmux_sessions={name for name in args.tmux.split(",") if name},
        previous=previous,
        commander=args.commander,
        sender=args.sender,
        recipient=args.recipient,
        token_file=args.token_file,
        send=_send,
    )
    print(f"sent={len(sent)} changed={str(changed).lower()}")
    return 0


def run_command(argv):
    """The timer's process runner. Tests replace this; they do not DM a cell."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _main_live(args) -> int:
    """Timer entry. Reads open obligations only. Sends only when the board changed."""
    cells = [cell for cell in args.cells.split(",") if cell]
    if not args.read_token_file:
        print("live publish needs --read-token-file (a path)", file=sys.stderr)
        return 2
    previous = None
    state = Path(args.state_file) if args.state_file else None
    if state and state.is_file():
        previous = state.read_text(encoding="utf-8")

    def send(body, *, token_file, sender, recipient):
        content = Path(str(state) + ".body") if state is not None else None
        if content is None:
            print("live publish needs --state-file", file=sys.stderr)
            raise SystemExit(2)
        content.write_text(body, encoding="utf-8")
        proc = run_command(send_argv(
            token_file=token_file, sender=sender, recipient=recipient, content_file=str(content),
        ))
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)

    try:
        changed = live_once(
            cells=cells,
            read_token_file=args.read_token_file,
            sender=args.sender,
            recipient=args.recipient,
            token_file=args.token_file,
            commander=args.commander,
            previous=previous,
            messages=[],
            runner=run_command,
            send=send,
        )
    except ReadFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if state is not None and changed:
        state.write_text(Path(str(state) + ".body").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"sent={int(changed)} changed={str(changed).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
