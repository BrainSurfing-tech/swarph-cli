#!/usr/bin/env python3
"""Refresh the commander question panel's cache (card #1224).

Runs OUTSIDE the render path (the module starts it on session.start and
turn.complete). It reads the board through the swarph CLI, which resolves its
own credential; this script reads no token or credential file. It writes one
file, ~/.claude/.swarph-panel-rows.json (or $CLAUDE_CONFIG_DIR/...), that the
AbovePrompt module reads on every redraw.

A row gets buttons only when lab-ovh's ask for it exists (card #1224): a press
posts one proposal DM that answers that ask, and the commander confirms on the
board. The cache therefore also carries each row's ``ask_id``, the label of the
newest valid ``panel:`` proposal already made for it (from the asker, after the
ask, naming one of the row's options), and the ``--as`` peer the press sends as.

The title and tap-option rules below are PORTED VERBATIM from
~/tools/board-publisher (build_board.py `_tap_options` and friends,
publish_board.py `commander_title`, `offer_question`, `untagged_title`), so the
panel shows the same question and the same taps as the board. That directory
lives outside this repo and is not imported. tests/tap_golden.json pins the
port against the original's output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---- ported verbatim from board-publisher/build_board.py (lines 75-178) ----

# card #291 ruling_1157 (1): tap-closable commander rows (holder commander,
# step build or step-less) with a binary title render as yes/no with the
# FIXED mapping yes=pass, no=fail. Every other option is display-only
# (outcome None) and closes nothing — no prose option closes PASS by
# default. card #1197: a choice title renders its choices instead (below).
_TAP_YES = {"label": "Yes", "text": "yes", "rec": True, "outcome": "pass"}
_TAP_NO = {"label": "No", "text": "no", "rec": False, "outcome": "fail"}


def _tap_closable(row: dict) -> bool:
    holder = row.get("holder") or row.get("cell") or ""
    return holder == "commander" and row.get("step") in (None, "build")


# card #1197: a fixed yes/no lost the commander's choice on "extend, flip,
# or kill?" rows. A title is NON-BINARY only when it ends with "?" and its
# final clause (after the last ':' or ' — ') contains " or ". Such a clause
# renders one tap per choice when it parses, and no tap at all
# (display-only, closes nothing) when it does not. Every other title keeps
# yes/no, even with " or " elsewhere in it.
_OR = re.compile(r"\sor\s", re.IGNORECASE)
_CHOICE_SPLIT = re.compile(r"\s*,\s*(?:or\s+)?|\s+or\s+", re.IGNORECASE)
_CHOICE = re.compile(r"^[A-Za-z][A-Za-z0-9'/-]*(?: [A-Za-z0-9'/-]+)?$")
# Three or four words, still a plain token each. A two-word tail is a stem.
_CHOICE_MEDIUM = re.compile(
    r"^[A-Za-z][A-Za-z0-9'/-]*(?: [A-Za-z0-9'/-]+){2,3}$"
)
# "Should we ..." is the question, not the first choice (review #2144).
_STEM = re.compile(
    r"^(?:Should|Do|Does|Is|Are|Can|Will)\s+\S",
    re.IGNORECASE,
)


def _choice_clause(title: str) -> str | None:
    """The final clause of a question title when it offers a choice, else None.

    Only a title ending in '?' qualifies. The clause is the last sentence.
    A ':' or ' — ' inside that sentence still cuts it. A colon in an earlier
    sentence does not (card #1206, row #2076). The clause must contain " or ".
    """
    text = (title or "").strip()
    if not text.endswith("?"):
        return None
    sentence = re.split(r"\.\s+", text[:-1].rstrip())[-1].strip()
    if re.search(r":| \u2014 ", sentence):
        sentence = re.split(r":| \u2014 ", sentence)[-1].strip()
    return sentence if sentence and _OR.search(sentence) else None


def _choices(clause: str) -> list[str] | None:
    """The 2-5 choices of a choice clause ("a, b, or c" / "a or b"), else None.

    The short form is one or two plain words, so a question stem ("Should we
    ship or hold") or a parenthetical ("(now or never)") does not parse.
    card #1206: a comma-separated list of three or more longer phrases is
    the same choice list. A two-part stem stays unparsed, so a binary
    question stays Yes/No. A comma after the final or-part is one phrase,
    not another choice (card #1206, row #2126), so the clause does not parse.
    Three or more options of three or four words, each within 24 characters,
    are a choice list too (card #1206, row #2140). A shorter part in that
    list is a stem, so the clause stays unparsed. A leading interrogative
    (Should, Do, Does, Is, Are, Can, or Will, plus a subject) keeps the
    medium and long forms unparsed (card #1206, review #2144).
    """
    # The part the final " or " introduces must not itself contain a comma.
    if re.search(r"\sor\s[^,]*,", clause, re.IGNORECASE):
        return None
    parts = [p.strip() for p in _CHOICE_SPLIT.split(clause)]
    if not 2 <= len(parts) <= 5:
        return None
    if len({p.lower() for p in parts}) != len(parts):
        return None
    # A comma inside a parenthesis must not become its own tap.
    if any(p.count("(") != p.count(")") for p in parts):
        return None
    if all(len(p) <= 24 and _CHOICE.match(p) for p in parts):
        return parts
    # Row #2140: every option is 3-4 words and still <= 24 characters, so it
    # misses both the short form and the long form. A 1-2 word part is a stem.
    # A leading interrogative is the question itself, not a choice.
    stemmed = bool(parts) and _STEM.match(parts[0])
    if (len(parts) >= 3 and not stemmed
            and all(len(p) <= 24 and _CHOICE_MEDIUM.match(p) for p in parts)):
        return parts
    if (len(parts) >= 3 and not stemmed and any(len(p) > 24 for p in parts)
            and all(p and len(p) <= 120 and "?" not in p for p in parts)):
        return parts
    return None


def _tap_options(row: dict) -> list[dict] | None:
    """Options for a tap-closable row, or None when it must not be a tap."""
    clause = _choice_clause(row.get("title") or "")
    if clause is not None:
        choices = _choices(clause)
        if choices is None:
            return None
        if {c.lower() for c in choices} != {"yes", "no"}:
            return [
                {"label": c[:1].upper() + c[1:], "text": c, "rec": False, "outcome": "pass"}
                for c in choices
            ]
    return [dict(_TAP_YES), dict(_TAP_NO)]


# ---- ported verbatim from board-publisher/publish_board.py (lines 42-43, 77-152) ----

_TAG = "[commander]"
_IN_SESSION_MARK = "[in-session]"


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


_TITLE_MAX = 160
_LEAD_PASS = re.compile(r"^\s*PASS\s*=\s*", re.IGNORECASE)
_TITLE_CUT = re.compile(r"\||\b(?:PASS|FAIL)\s*=", re.IGNORECASE)


_ACCEPT_MARK = "\n\nACCEPT CHECK:"


def offer_question(row: dict, messages: list | None) -> str | None:
    """The offer question for one commander-held row, or None.

    The card-thread message addressed to the commander whose body contains
    ``ACCEPT CHECK: `` plus this row's accept. The question is the text
    before ``\\n\\nACCEPT CHECK:``. A message whose accept differs does
    not match. card #1206.
    """
    accept = str(row.get("accept") or "")
    if not accept or not messages:
        return None
    needle = "ACCEPT CHECK: " + accept
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if (msg.get("to_node") or msg.get("to") or "") != "commander":
            continue
        body = str(msg.get("content") or msg.get("body") or "")
        if needle not in body:
            continue
        head, sep, _rest = body.partition(_ACCEPT_MARK)
        if not sep:
            continue
        question = head.strip()
        if question:
            return question
    return None


def untagged_title(row: dict) -> str:
    """A title for a commander-held row whose accept carries no [commander] tag.

    The accept text without a leading 'PASS =', cut at the first '|' or
    'PASS=' / 'FAIL=' and trimmed. A JSON accept reads as its pass
    conditions. No accept reads as the row's ids.
    """
    accept = str(row.get("accept") or "").strip()
    text = ""
    try:
        parsed = json.loads(accept) if accept.startswith("{") else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("pass"), list):
        text = "; ".join(str(p.get("text") or "") for p in parsed["pass"] if isinstance(p, dict))
    elif accept:
        text = _TITLE_CUT.split(_LEAD_PASS.sub("", accept), 1)[0]
    text = " ".join(text.split())
    if not text:
        return f"card #{row.get('card_id')} obligation #{row.get('id', row.get('obligation_id'))}"
    if len(text) > _TITLE_MAX:
        text = text[:_TITLE_MAX - 1].rstrip() + "…"
    return text


# ---- glue (not ported) -------------------------------------------------

CACHE_NAME = ".swarph-panel-rows.json"
OPTION_KEYS = ("label", "text", "outcome")


def cache_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / CACHE_NAME


PANEL_PREFIX = "panel:"
_PANEL_ROW = re.compile(r"(?:^|\s)on\s+#\d+\s*$")


def ask_message(row: dict, messages: list[dict]) -> dict | None:
    """lab-ovh's ask FOR a row: its DM to the commander carrying the row's obligation_id.

    Structural, never parsed from prose; the same predicate and first-match rule as the
    commander's board page (meta-edge-surfer app.js boardAskMessage), so a proposal
    answers the ask the page will cite when it closes the row.
    """
    for msg in messages:
        if (msg.get("from_node") == "lab-ovh" and msg.get("to_node") == "commander"
                and msg.get("obligation_id") == row.get("id")):
            return msg
    return None


def proposed_label(ask: dict, messages: list[dict], options: list[dict] | None) -> str | None:
    """The label of the newest valid ``panel: <label> on #<row>`` proposal answering this ask, else None.

    Same rule as the board page (meta-edge-surfer app.js boardPanelProposal). A message counts only when
    ALL hold: it is addressed to the commander, sent by the cell that sent the ask, has an integer id above
    the ask's, replies to the ask, starts with ``panel:``, and its label is exactly one of the row's option
    labels. Anything else is ignored, so a message from another node cannot name the proposal.
    """
    labels = {o["label"] for o in options or [] if isinstance(o, dict) and isinstance(o.get("label"), str)}
    best_id, best_label = None, None
    for m in messages:
        mid = m.get("id")
        if (m.get("reply_to") != ask.get("id") or m.get("to_node") != "commander"
                or m.get("from_node") != ask.get("from_node")
                or not isinstance(mid, int) or isinstance(mid, bool) or mid <= ask["id"]):
            continue
        content = str(m.get("content") or "")
        if not content.startswith(PANEL_PREFIX):
            continue
        label = _PANEL_ROW.sub("", content[len(PANEL_PREFIX):].strip()).strip()
        if label in labels and (best_id is None or mid > best_id):
            best_id, best_label = mid, label
    return best_label


def commander_rows(rows: list[dict], messages: list[dict]) -> list[dict]:
    """The rows the board shows the commander: tagged [commander], or held by the commander.

    Same keep rule and title order as publish_board.commander_rows: the tag's
    title, else the card-thread offer question, else untagged_title. A row with no
    ask from lab-ovh gets no options: a press has no ask to answer.
    """
    kept = []
    for row in rows:
        if str(row.get("state") or row.get("status") or "open").startswith("closed"):
            continue
        holder = row.get("holder") or ""
        title = commander_title(row.get("accept") or "")
        if title is None:
            if holder != "commander":
                continue
            title = offer_question(row, messages) or untagged_title(row)
        # A row that closes nothing on a tap (validate, plan-review) gets no buttons. Nor does a row with
        # no title: _tap_options would answer yes/no to an empty question, which is a guessed button.
        if not title.strip():
            title = f"card #{row.get('card_id')} row #{row.get('id')}"
            options = None
        else:
            options = _tap_options({"title": title}) if _tap_closable(row) else None
        ask = ask_message(row, messages)
        ask_id = ask.get("id") if ask else None
        if ask_id is None:
            options = None
        kept.append({
            "id": row.get("id"),
            "card_id": row.get("card_id"),
            "created_at": row.get("created_at") or "",
            "title": title,
            "options": None if options is None else [{k: o[k] for k in OPTION_KEYS} for o in options],
            "ask_id": ask_id,
            "proposed": None if ask_id is None else proposed_label(ask, messages, options),
        })
    kept.sort(key=lambda r: r["created_at"])
    return kept


def _cli_json(run, argv: list[str]):
    proc = run(argv, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:4])} exited {proc.returncode}")
    return json.loads(proc.stdout)


def refresh(*, as_peer: str, board_url: str, run=subprocess.run, out: Path | None = None) -> dict:
    """Read the board and rewrite the cache. A failed read raises and leaves the old cache."""
    base = ["swarph", "board"]
    listing = _cli_json(run, base + ["obligations", "list", "--status", "open", "--json", "--as", as_peer])
    rows = listing if isinstance(listing, list) else listing.get("obligations") or []
    # One card-thread read per card with a commander-held row: it holds the offer question and lab-ovh's ask.
    messages: list[dict] = []
    for card_id in sorted({
        r["card_id"] for r in rows
        if r.get("holder") == "commander" and r.get("card_id") is not None
    }):
        try:
            thread = _cli_json(run, base + ["cards", "thread", str(card_id), "--json", "--as", as_peer])
        except (RuntimeError, ValueError):
            continue  # shortcut: a failed thread read means no ask, so no buttons and the old title fallback
        found = thread.get("messages") if isinstance(thread, dict) else thread
        messages.extend(m for m in found or [] if isinstance(m, dict))
    cache = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "board_url": board_url,
        "peer": as_peer,
        "rows": commander_rows(rows, messages),
    }
    target = out or cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a redraw never reads half a file.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, target)
    return cache


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--as", dest="as_peer", default="lab-ovh", help="read identity for the swarph CLI")
    p.add_argument("--board-url", default=os.environ.get("SWARPH_PANEL_BOARD_URL", ""))
    p.add_argument("--min-age", type=float, default=60, help="skip when the cache is newer than this many seconds")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    target = args.out or cache_path()
    try:
        if time.time() - target.stat().st_mtime < args.min_age:
            return 0
    except OSError:
        pass
    try:
        refresh(as_peer=args.as_peer, board_url=args.board_url, run=run, out=target)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"swarph-panel refresh: {exc}; the last cache is kept", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
