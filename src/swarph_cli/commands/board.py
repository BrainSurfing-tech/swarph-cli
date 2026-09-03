"""``swarph board`` — CLI wrappers over the mesh-gateway board endpoints.

The mesh board (projects + cards kanban) was server-only: reachable only via raw
HTTP. This wraps it with the same ergonomics as ``swarph mesh``
(``--as``/``--gateway``/``--token-file``/``--json``). Pure helpers (URL/query +
payload builders, link-merge, formatters) are unit-tested; HTTP is the seam
(reused from mesh.py). Contract from the live gateway OpenAPI — note POST
/board/cards has NO ``stage`` field (the gateway defaults it to ``proposed``;
use ``cards move`` to advance).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# #532 — negative-branch marker detection. THREE COPIES exist and no single
# suite pins all three: here, mesh-gateway server.py (_accept_state), and
# mesh-gateway scripts/obligation_sweep.py. Unpinned-by-design is not the same
# decision as standalone-by-design — each repo's tests pin the same seven
# phrases. The detector is TWO-SIDED (drop-on-meta-edge, PR #124): it
# over-reads wishes ("must not fail") and under-reads real checks without the
# token ("Otherwise reject"); word boundaries remove the incidental compounds
# (failover, fail-safe, failsafe). Detection is named, never concluded — no
# substring test can know a falsifier exists.
_FAIL_MARKER_RE = re.compile(r"(?<![\w-])fail(?:s|ed|ing|ures?)?(?![\w-])", re.IGNORECASE)

from swarph_cli.commands._content import ContentError, add_content_args, resolve_content
from swarph_cli.commands._display import sanitize_terminal as _s
from swarph_cli.commands.mesh import (
    _add_common,
    _http_get_json,
    _post_json,
    _resolve_self_name,
    _resolve_token,
)


_SENTINEL = object()


# ── HTTP: PATCH (mesh.py has GET + POST; the board needs PATCH for move/link) ──

def _patch_json(url: str, body: dict, token: str, *, timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            return exc.code, {"detail": str(exc)}
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


# ── pure builders (unit-tested) ───────────────────────────────────────────────

def _cards_list_url(gateway: str, *, project=None, stage=None, assignee=None,
                    label=None) -> str:
    q = {k: v for k, v in (("project", project), ("stage", stage),
                           ("assignee", assignee), ("label", label)) if v}
    base = f"{gateway.rstrip('/')}/board/cards"
    return f"{base}?{urllib.parse.urlencode(q)}" if q else base


def _normalize_due_at(value: str) -> str:
    """``YYYY-MM-DD`` or ISO datetime → gateway ``due_at`` string (#145)."""
    raw = value.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw}T00:00:00"
    return raw


def _card_add_payload(actor, project_id, title, *, body=None, ai2=False, priority=0,
                      labels=None, due_at=None) -> dict:
    p = {"actor": actor, "project_id": int(project_id), "title": title,
         "ai2": bool(ai2), "priority": int(priority)}
    if body:
        p["body"] = body
    if labels:
        p["labels"] = list(labels)
    if due_at is not None:
        p["due_at"] = _normalize_due_at(due_at) if due_at else None
    return p


def _obligations_list_url(gateway: str, *, status=None, holder=None,
                          card_id=None, overdue=False) -> str:
    q = {k: v for k, v in (("status", status), ("holder", holder),
                           ("card_id", card_id)) if v is not None}
    if overdue:
        q["overdue"] = "true"
    base = f"{gateway.rstrip('/')}/board/obligations"
    return f"{base}?{urllib.parse.urlencode(q)}" if q else base


def _format_obligations(data: dict) -> str:
    """#590's readout. The empty state must say WHY it might be empty — on the
    endpoint whose purpose is auditable absence, a bare "(none)" cannot be
    distinguished from a filter that named nothing or a grant that hid rows.
    `as_of` always prints: a relative readout without its instant claims more
    than it measured (#145).
    """
    rows = data.get("obligations", [])
    as_of = data.get("as_of", "?")
    if not rows:
        return (f"(no obligations visible — none exist, none match the filter, "
                f"or none in projects you can read; as_of {as_of})")
    lines = []
    for o in rows:
        oid = _s(o.get("id")); card = _s(o.get("card_id"))
        holder = _s(o.get("holder")); status = _s(o.get("status"))
        marks = []
        if o.get("overdue"):
            marks.append("OVERDUE")
        if o.get("unclosable_reason"):
            marks.append(f"UNCLOSABLE:{_s(o['unclosable_reason'])}")
        state = _s(o.get("accept_state"))
        marks.append(f"accept:{state}" if state else "accept:missing")
        if o.get("status") != "open":
            # a NULL outcome on a closed row is a pre-#562 legacy close — name
            # the absence rather than print a blank that reads as recorded
            marks.append(f"outcome:{_s(o.get('close_outcome')) or 'none-recorded'}")
        lines.append(f"  #{oid} card={card} holder={holder} status={status} "
                     f"kind={_s(o.get('kind'))}  {' '.join(marks)}")
    lines.append(f"({len(rows)} obligation(s), as_of {as_of})")
    return "\n".join(lines)


def _card_edit_payload(actor, title, body, *, due_at=_SENTINEL) -> dict:
    """#191: the edit patch — title and/or body, the two fields that made a
    card's text write-once until the gateway grew them (BoardCardPatch).

    REFUSES THE EMPTY PATCH. A PATCH carrying only `actor` returns 200 and
    changes nothing — #256's class (accepted, silently unchanged), and for an
    editor it reads as "your correction landed" when nothing did. None means
    "not mentioned"; an empty string is a real value (it clears), so the
    distinction is `is not None`, never truthiness.

    ``due_at=_SENTINEL`` means the caller did not pass ``--due``; an explicit
    empty string clears the due date (#145).
    """
    patch = {"actor": actor}
    if title is not None:
        patch["title"] = title
    if body is not None:
        patch["body"] = body
    if due_at is not _SENTINEL:
        patch["due_at"] = _normalize_due_at(due_at) if due_at else None
    if len(patch) == 1:
        raise ValueError("nothing to edit — pass --title, --body, and/or --due")
    return patch


def _apply_label(current: list, action: str, label: str) -> list:
    """Compute the NEW label set. Pure, so the read-modify-write is testable
    without a gateway.

    The server takes a FULL REPLACEMENT (labels are a set — a merge-only field
    could never REMOVE one), so `label rm` is unavoidably read-modify-write. That
    is last-writer-wins between two simultaneous editors of the SAME card, which
    the board already has for `stage` and `assignee` — stated rather than hidden,
    because the alternative (server-side add/rm ops) is a bigger surface than
    this card justifies.
    """
    cur = [str(x).strip().lower() for x in (current or [])]
    lab = label.strip().lower()
    if action == "add":
        return cur if lab in cur else cur + [lab]
    return [x for x in cur if x != lab]


def _project_add_payload(actor, slug, title, *, goal=None) -> dict:
    p = {"actor": actor, "slug": slug, "title": title}
    if goal:
        p["goal"] = goal
    return p


def _merge_link(existing: Optional[dict], key: str, value: str) -> dict:
    merged = dict(existing or {})
    merged[key] = value
    return merged


def _project_ref_to_id(value, projects) -> Optional[int]:
    """Resolve a --project ref (numeric id OR slug) to a project_id (pure).

    Digit → passthrough; otherwise slug-lookup in the projects list. None if the
    slug is unknown so the caller can error clearly instead of sending a bad query.
    """
    if value is None:
        return None
    s = str(value)
    if s.isdigit():
        return int(s)
    for p in projects or []:
        if p.get("slug") == s:
            return p.get("id")
    return None


# ── formatters (unit-tested) ──────────────────────────────────────────────────

def _cards_of(data):
    return data.get("cards", []) if isinstance(data, dict) else (data or [])


def _rel_due(days_until) -> str:
    """Spell the relative age — a bare date makes the reader compute (#145)."""
    if days_until is None:
        return "?"
    n = int(days_until)
    if n < 0:
        d = -n
        return "1 day ago" if d == 1 else f"{d} days ago"
    if n == 0:
        return "today"
    return "in 1 day" if n == 1 else f"in {n} days"


def _format_due_head(data) -> list[str]:
    """Render the list endpoint's `due` summary + dated cards at the head.

    Built ONLY from the LIST response (#661: GET /board/cards/{id} returns
    due_state=None even when due_at is set — a single-card readout would
    report zero overdue forever with no error).
    """
    if not isinstance(data, dict):
        return []
    due = data.get("due")
    if not isinstance(due, dict):
        return []
    lines = [
        f"due:  overdue={due.get('overdue', 0)}  today={due.get('today', 0)}  "
        f"upcoming={due.get('upcoming', 0)}  undated={due.get('undated', 0)}"
    ]
    # Prefer overdue, then today, then upcoming — sorted by days_until ascending
    # so the most overdue sits first. Pull rows from the cards array (the due
    # block itself is counts only).
    dated = [c for c in _cards_of(data)
             if c.get("due_state") in ("overdue", "today", "upcoming")]
    order = {"overdue": 0, "today": 1, "upcoming": 2}
    dated.sort(key=lambda c: (
        order.get(c.get("due_state"), 9),
        c.get("days_until") is None,
        c.get("days_until") if c.get("days_until") is not None else 0,
    ))
    for c in dated:
        title = _s(c.get("title") or "")
        if len(title) > 60:
            title = title[:57] + "..."
        lines.append(
            f"  #{c.get('id')}  {c.get('due_state'):<9}  "
            f"{_rel_due(c.get('days_until')):<14}  {title}"
        )
    if not dated and (due.get("overdue") or due.get("today") or due.get("upcoming")):
        # Counts say something is dated but this page's cards don't carry them
        # (filtered list / pagination). Say so — silent empty is the #145 lie.
        lines.append("  (dated cards not in this page — widen the list filter)")
    elif not dated:
        lines.append("  (none dated in this list)")
    return lines


def _format_cards(data) -> str:
    head = _format_due_head(data)
    rows = _cards_of(data)
    if not rows:
        body = ["(no cards)"]
    else:
        body = [f"{'ID':>4}  {'STAGE':<9} {'PRJ':>4} {'AI²':<3} TITLE"]
        for c in rows:
            ai2 = "AI²" if c.get("ai2") else ""
            body.append(f"{c.get('id',''):>4}  {c.get('stage',''):<9} "
                        f"{c.get('project_id',''):>4} {ai2:<3} {_s(c.get('title'))}")
    if head:
        return "\n".join(head + [""] + body)
    return "\n".join(body)


def _format_card(card: dict) -> str:
    lines = [
        f"#{card.get('id')}  [{card.get('stage')}]  project={card.get('project_id')}"
        f"  assignee={_s(card.get('assignee')) or '-'}  {'AI²' if card.get('ai2') else ''}",
        f"  {_s(card.get('title'))}",
    ]
    if card.get("body"):
        lines.append(f"\n{_s(card['body'])}")
    links = card.get("links") or {}
    if links:
        lines.append("\nlinks:")
        for k, v in links.items():
            lines.append(f"  {_s(k)}: {_s(v)}")
    return "\n".join(lines)


def _format_projects(data) -> str:
    rows = data.get("projects", []) if isinstance(data, dict) else (data or [])
    if not rows:
        return "(no projects)"
    lines = [f"{'ID':>4}  {'SLUG':<22} TITLE"]
    for p in rows:
        lines.append(f"{p.get('id',''):>4}  {_s(p.get('slug')):<22} {_s(p.get('title'))}")
    return "\n".join(lines)


# ── #181: the card IS a thread ────────────────────────────────────────────────
# The gateway has carried `GET /board/cards/{id}/thread` and the card-gated attach
# path since 2026-08-05, and NOTHING COULD REACH THEM. No CLI verb existed, so the
# card↔DM fusion lived entirely in the database and the OpenAPI schema: shipped,
# deployed, and invisible to every human and cell. Found the way these are always
# found — I tried to write a finding onto a card and had to send a DM instead.

def _thread_url(gateway: str, card_id: int, *, limit: Optional[int] = None) -> str:
    base = f"{gateway.rstrip('/')}/board/cards/{card_id}/thread"
    return f"{base}?limit={limit}" if limit else base


def _thread_recipient(card: dict, explicit_to: Optional[str]) -> str:
    """Who a card-thread post is addressed to. `--to` wins, else the assignee.

    >>> RAISES RATHER THAN INVENTING A SENTINEL. <<< POST /messages requires
    exactly one of {to_node, channel}, so a card post needs a real recipient. The
    tempting move is a placeholder like "board" or "__card__" — but board card
    #259 measured what the gateway does with an unregistered to_node: it returns
    200 and the message is addressed to nobody, indistinguishable from delivery.
    A placeholder here would manufacture that defect once per card post.
    """
    if explicit_to:
        return explicit_to
    assignee = card.get("assignee")
    if assignee:
        return assignee
    raise RuntimeError(
        f"card #{card.get('id')} has no assignee, so there is no default recipient "
        f"for a thread post — pass --to <peer>. (A card post is still a DM on the "
        f"wire; it needs someone to be addressed to.)"
    )


def _say_line(resp: dict, card_id, to_node: str) -> str:
    """The post confirmation — AND any obligation the post just DISCHARGED.

    >>> THE GATEWAY ALREADY SAID SO AND THIS CLI THREW IT AWAY. <<< POST
    /messages returns `closed_obligations: [ids]` (server.py:4077-4099): any
    message a HOLDER posts to a thread closes their oldest open obligation on
    it, unconditionally, without consulting the `accept` falsifier #532 added.
    The old formatter read `id` only, so the terminal printed "posted id=NNN"
    and nothing else.

    MEASURED CONSEQUENCE (science-claude, card #562): obligation #22 on card
    #544 closed at 06:59:15 on a status post whose literal content was that the
    work was NOT done — one membrane passing, four CANNOT_EVALUATE, two
    proposals unstarted. IT STAYED WRONGLY CLOSED FOR SIX HOURS while its holder
    said in five further messages that it was unmet, and nothing compared the
    two. `obligation_sweep.py` could not catch it either: that sweep selects
    `status = 'open'`, so a wrongly-closed row falls out of the set forever.

    The signal existed, was correct, and travelled the wire. It died at the last
    hop, in the formatter. A one-line print is not the fix for the auto-close
    POLICY (that is #562's own question) -- it is the fix for the holder not
    being told, which is what made six hours of divergence possible.
    """
    line = f"posted id={resp.get('id')} onto card #{card_id} (to {to_node})"
    closed = resp.get("closed_obligations") or []
    if closed:
        ids = ", ".join(f"#{i}" for i in closed)
        line += (f"\n  >>> THIS POST CLOSED OBLIGATION {ids}. <<< Posting to a "
                 f"thread discharges your oldest open obligation on it — the "
                 f"accept check was NOT evaluated. If the work is not actually "
                 f"done, say so now: a closed obligation leaves the sweep set "
                 f"and nothing will chase it again.")
    return line


def _card_say_payload(from_node: str, to_node: str, kind: str, content: str,
                      thread_uuid: str) -> dict:
    return {
        "from_node": from_node,
        "to_node": to_node,
        "kind": kind,
        "content": content,
        "thread_id": thread_uuid,
    }


_NO_TIMEOUT = "NO TIMEOUT — this never goes red on its own; pass --timeout-hours if it should"


def _accept_line(accept) -> str:
    """#532's mint-time falsifier readout — shared by the pre-#591 and the §2
    success lines, because the rule is about the MOMENT, not the response
    shape: a row with no accept check reads RED in the sweep and looks
    identical to a sound one the instant after it is minted."""
    if not accept:
        return ("NO ACCEPT CHECK — reads RED in the sweep; pass --accept "
                "\"PASS = ... | FAIL = ...\" naming an observable and a way "
                "it comes out negative")
    if not _FAIL_MARKER_RE.search(str(accept)):
        return ("accept check has NO FAIL BRANCH — marked NO-FAIL-BRANCH in "
                "the sweep; a check that cannot come out negative is not one")
    return f"accept: {_s(accept)}"


def _thread_line(d) -> str:
    return (f"thread {d.get('thread_uuid')} — work lands IN that thread; CLOSE "
            f"it with `swarph board obligations close {d.get('id')}` plus an "
            f"outcome and evidence; a plain `mesh send` cannot close it "
            f"(#509/#562 — the VERB is the mechanism, the thread is where it lands)")


def _format_ask(d) -> str:
    """One line naming the obligation, its holder, and when it goes red.

    >>> NAMING THE DEADLINE OR ITS ABSENCE IS THE POINT, NOT DECORATION. <<< #145's
    lesson, one layer over: "0 overdue" must not be able to mean "nobody set dates".
    An obligation with no timeout never goes red on its own, and the operator has to
    be told that AT THE MOMENT THEY CREATE IT — afterwards it looks identical to one
    that simply is not late yet.

    #532 applies the same rule to the falsifier: an obligation with no accept
    check reads RED in the sweep, and one whose check has no FAIL branch is
    marked NO-FAIL-BRANCH — both are said AT MINT TIME, because afterwards the
    rows look identical to sound ones.
    """
    when = d.get("timeout_at")
    deadline = f"overdue after {when}" if when else _NO_TIMEOUT
    return (f"obligation #{d.get('id')} on card #{d.get('card_id')}: "
            f"{d.get('holder')} owes it, status={d.get('status')}, {deadline}\n"
            f"  {_accept_line(d.get('accept'))}\n"
            f"  {_thread_line(d)}")


def _format_thread(data) -> str:
    msgs = data.get("messages", []) if isinstance(data, dict) else (data or [])
    card_id = data.get("card_id") if isinstance(data, dict) else None
    if not msgs:
        # NOT "(no messages)" alone — an empty thread and an unreadable one must not
        # render the same. The gateway 403s an unreadable card and 409s an unmigrated
        # one, so reaching here with [] genuinely means nobody has posted yet.
        return f"card #{card_id}: thread is empty — no messages posted yet"
    lines = [f"card #{card_id} — {len(msgs)} message(s)"]
    for m in msgs:
        ts = (m.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"\n  [{m.get('id')}] {ts}  {_s(m.get('from_node'))} -> "
            f"{_s(m.get('to_node'))}  ({_s(m.get('kind'))})"
        )
        for ln in (_s(m.get("content")) or "").splitlines():
            lines.append(f"      {ln}")
    return "\n".join(lines)


# ── #591: the card step graph (contract v0.4.1) ───────────────────────────────
# ask/graph/take/decline/amend. Every success prints its next act and every
# refusal is the gateway's `detail` verbatim (`_out`) — the contract's premise
# is that a refusal naming its fix is a step, and one that only says no is the
# gap cells route around. Nothing here derives state: the gateway's `state`,
# `take_with`, `close_with`, `warn` strings are printed, never reconstructed.

def _step_list(values) -> list[str]:
    """`--needs a,b --needs +c` → ['a', 'b', 'c']. Repeated flags and comma
    lists both flatten (a plain `store` kept only the LAST `--needs`, silently
    — #591 review), and the contract's `+step` spelling (§1) drops its sign:
    sent verbatim, `+build` is off-menu and 400s. A `-step` keeps its sign for
    `_amend_payload` to route."""
    if isinstance(values, str):
        values = [values]
    names = (s.strip().lstrip("+") for v in values or [] for s in str(v).split(","))
    return [n for n in names if n]


def _ask_payload(self_name, what, *, holder=None, step=None, needs=None, hours=None,
                 done=None, accept=None, kind="action", timeout_hours=None) -> dict:
    """POST /board/cards/{id}/ask body. None = not mentioned = key ABSENT (#191's
    rule; test_532 pins that a null `accept` on the wire would overwrite the
    gateway's own normalization). No holder → `requested`; `me` → the caller's
    own `--as` identity (the contract binds `me` to the token, never to an env
    var). `needs` is whatever `--needs` collected — see `_step_list`.
    """
    p = {"what": what, "created_by": self_name, "kind": kind}
    if holder is not None:
        p["holder"] = self_name if holder == "me" else holder
    if step is not None:
        p["step"] = step
    if needs is not None and _step_list(needs):
        p["needs"] = _step_list(needs)  # an empty --needs is "no extra edges" = the key absent
    if hours is not None:
        p["hours"] = hours
    if done is not None:
        p["done"] = done
    if accept is not None:
        p["accept"] = accept
    if timeout_hours is not None:
        p["timeout_hours"] = timeout_hours
    return p


def _ask_line(d) -> str:
    """§2's stdout-on-success, keyed on the gateway's `state`. A response with
    no `state` is the pre-#591 shape — `_format_ask` keeps rendering it
    (test_307/test_532 pin that output). The gateway sets `state` on EVERY
    ask since #591, so the #532/#145 mint-time lines (NO ACCEPT CHECK /
    NO-FAIL-BRANCH; and on an unstepped row NO TIMEOUT + the thread/close
    hint) are appended here too — a live row not yet closed looks identical
    to a sound one the instant after this prints. `warn` prints last, on its
    own line, verbatim: the step-less ask is legal and buys no gate credit,
    and the moment it is minted is the only moment that is not silent.

    Contract §2 also carries `menu fleet vN`, `still missing on #20: …`,
    `ack by +24h`, `due +24h after build closes`, `(evidence: pull/57) ·
    unblocked: …` — fields the live response (b6a7bb1) does not send
    (`menu_source`, `missing`, `ack_by`, `due`, `evidence`, `unblocked`), and
    the rule above forbids deriving them here or reading the graph a second
    time. Each prints in its contract position the moment the gateway sends
    it; absent, the line is byte-identical to today's (#591 review item 2 —
    the gateway half is the open hypothesis)."""
    state = d.get("state")
    if state is None:
        return _format_ask(d)
    n = d.get("id"); card = d.get("card_id")
    step = _s(d.get("step")) or "unstepped"
    holder = _s(d.get("holder")); take = _s(d.get("take_with"))
    missing = ", ".join(_s(m) for m in d.get("still_missing") or [])  # the gateway's field (mesh-gateway #157)
    if state == "requested":
        elig = ", ".join(_s(e) for e in d.get("eligible") or []) or "(nobody)"
        menu = (f"menu {_s(d['menu_source'])} " if d.get("menu_source") else "menu ") + \
            f"v{d.get('menu_version') or '-'}"
        line = (f"minted #{n} requested ({step} on #{card}) · offered to: {elig} · {menu}"
                + (f" · still missing on #{card}: {missing}" if missing else "")
                + f" — take with: {take}")
    elif state == "offered":
        ack = f", ack by {_s(d['ack_by'])}" if d.get("ack_by") else ""
        line = (f"minted #{n} offered to {holder} ({step} on #{card}{ack}) — "
                f"{holder} takes with: {take}")
    elif state == "open":
        due = f", due {_s(d['due'])}" if d.get("due") else ""
        line = (f"#{n} open ({holder}, {step} on #{card}{due}) — close with: swarph board "
                f"obligations close {n} --outcome pass --evidence \"<container>\"")
    else:
        line = f"minted #{n} {step} on #{card} · {_s(state)}"
        if d.get("evidence"):
            line += f" (evidence: {_s(d['evidence'])})"
        if d.get("unblocked"):
            line += " · unblocked: " + ", ".join(_s(u) for u in d["unblocked"])
        if missing:
            line += f" · still missing: {missing}"
    if not str(state).startswith("closed"):
        line += "\n  " + _accept_line(d.get("accept"))
        if d.get("step") is None:
            # unstepped: no menu clock, no container — the pre-#591 rules hold
            if not d.get("timeout_at"):
                line += "\n  " + _NO_TIMEOUT
            line += "\n  " + _thread_line(d)
    if d.get("warn"):
        line += "\n" + _s(d["warn"])
    return line


def _format_graph(g) -> str:
    """GET /board/cards/{id}/graph: one header, one line per menu step (every
    step at every stage — the read renders the menu, not only the rows), then
    the `unstepped` section. `eligible` prints only when the gateway sent it
    (non-null): it is computed for missing/unstaffable steps only, and printing
    `eligible=-` on a closed step would read as "nobody may hold this"."""
    menu = g.get("menu") or {}
    gate = g.get("gate") or {}
    missing = ", ".join(_s(m) for m in g.get("missing") or []) or "none"
    lines = [f"card #{g.get('card_id')} stage={_s(g.get('stage')) or '-'} "
             f"implied={_s(g.get('stage_implied')) or '-'} "
             f"gate={_s(gate.get('mode')) or '-'} flip_at={_s(gate.get('flip_at')) or '-'} "
             f"menu {_s(menu.get('source')) or '?'} v{menu.get('version') or '-'} · missing: {missing}"]
    for s in g.get("steps") or []:
        needs = ", ".join(
            (f"{_s(n.get('step'))} — no row" if n.get("row_id") is None else
             f"{_s(n.get('step'))} #{n.get('row_id')} "
             f"{'ok' if n.get('satisfied') else _s(n.get('state')) or '-'}")
            for n in s.get("needs") or []) or "-"
        line = (f"  {_s(s.get('step'))} [{'M' if s.get('mandatory') else 'opt'}] "
                f"{_s(s.get('state'))} holder={_s(s.get('holder')) or '-'} "
                f"due={_s(s.get('due')) or '-'} needs={needs}")
        if s.get("eligible") is not None:
            line += f" eligible={', '.join(_s(e) for e in s['eligible']) or '(nobody)'}"
        lines.append(line)
    if g.get("unstepped"):
        lines.append("unstepped:")
        for r in g["unstepped"]:
            outcome = f" outcome={_s(r['close_outcome'])}" if r.get("close_outcome") else ""
            lines.append(f"  #{r.get('id')} holder={_s(r.get('holder')) or '-'} "
                         f"status={_s(r.get('status'))}{outcome}")
    return "\n".join(lines)


def _move_line(x) -> str:
    """`cards move` success — plus the §4 gate's `warn` on its own line. In
    warn mode the write succeeds and this line is the only place the missing
    steps and the flip date are said."""
    line = f"card #{x.get('id')} -> {x.get('stage')}"
    if x.get("warn"):
        line += "\n" + _s(x["warn"])
    return line


def _amend_payload(step=None, holder=None, accept=None, hours=None,
                   needs_add=None, needs_remove=None, needs=None) -> dict:
    """PATCH /board/obligations/{id}/amend body. None = not mentioned; an empty
    `holder` is a REAL value (clears → `requested`), so the test is
    `is not None`, never truthiness (#191). `needs` is the contract's signed
    spelling (§2: `+step` adds, `-step` removes) routed onto needs_add /
    needs_remove; every edge list flattens through `_step_list`. Refuses the
    empty patch locally — the gateway 400s it too, but a round trip to learn
    you said nothing is the #256 shape one hop later."""
    signed = _step_list(needs)
    add = _step_list(needs_add) + [x for x in signed if not x.startswith("-")]
    rem = _step_list(needs_remove) + [x[1:] for x in signed if x.startswith("-")]
    patch = {k: v for k, v in (("step", step), ("holder", holder), ("accept", accept),
                               ("hours", hours), ("needs_add", add or None),
                               ("needs_remove", rem or None)) if v is not None}
    if not patch:
        raise ValueError("nothing to amend — pass --step, --holder, --accept, --hours, "
                         "--needs-add and/or --needs-remove")
    return patch


def _obligation_act_line(d) -> str:
    """take / decline / amend success lines, told apart by the keys the gateway
    sends (decline carries `declined_by`, amend carries `amended`, take carries
    `close_with`). `close_with` is printed verbatim — the next act, not a
    reconstruction of it."""
    n = d.get("id"); state = _s(d.get("state")) or "-"; due = _s(d.get("due")) or "-"
    if "declined_by" in d:
        rem = ", ".join(_s(e) for e in d.get("remaining_eligible") or []) or "(nobody)"
        line = (f"#{n} declined by {_s(d.get('declined_by'))} · remaining eligible: {rem} "
                f"· state: {state}")
        if d.get("unstaffable"):
            line += ("\n  UNSTAFFABLE — every eligible holder declined; the asker or "
                     "assignee exits with `obligations close --outcome cannot_evaluate`")
        return line
    if "amended" in d:
        was = d.get("was") or {}
        fields = ", ".join(f"{_s(k)} (was: {_s(was.get(k)) or 'none'})" if k in was else _s(k)
                           for k in d.get("amended") or []) or "-"
        return f"#{n} amended: {fields} · state: {state} · due: {due}"
    return (f"#{n} {state} ({_s(d.get('holder'))}, {_s(d.get('step')) or 'unstepped'} on "
            f"#{d.get('card_id')}) · due: {due}\n  close with: {_s(d.get('close_with'))}")


# ── parser + dispatch ─────────────────────────────────────────────────────────

class _Parser(argparse.ArgumentParser):
    """`intermixed=True` lets positionals sit on either side of options —
    `ask <id> <holder> --accept "…" "<what>"`, the pre-#591 form with an
    option between its two positionals. Plain argparse matches positionals
    per contiguous chunk, so once `holder` became optional (nargs='?') that
    argv parsed holder=None what=<holder> and left "<what>" unrecognized
    (#591 review, measured on 3.14; identical on 3.11). The re-entrancy guard
    is for 3.10-3.12, whose intermixed parser calls parse_known_args on
    itself."""

    def __init__(self, *a, intermixed=False, **kw):
        super().__init__(*a, **kw)
        self._intermixed, self._parsing = intermixed, False

    def parse_known_args(self, args=None, namespace=None):
        if not self._intermixed or self._parsing:
            return super().parse_known_args(args, namespace)
        self._parsing = True
        try:
            return self.parse_known_intermixed_args(args, namespace)
        finally:
            self._parsing = False


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph board", description="Mesh board (projects + cards).")
    top = p.add_subparsers(dest="group", required=True)

    proj = top.add_parser("projects", help="board projects").add_subparsers(dest="command", required=True)
    pl = proj.add_parser("list", help="list projects"); pl.add_argument("--json", action="store_true"); _add_common(pl)
    pa = proj.add_parser("add", help="create a project")
    pa.add_argument("slug"); pa.add_argument("--title", required=True); pa.add_argument("--goal")
    pa.add_argument("--json", action="store_true"); _add_common(pa)

    cards = top.add_parser("cards", help="board cards").add_subparsers(
        dest="command", required=True, parser_class=_Parser)
    cl = cards.add_parser("list", help="list cards")
    cl.add_argument("--project"); cl.add_argument("--stage"); cl.add_argument("--assignee")
    cl.add_argument("--label", help="only cards carrying this label (exact match)")
    cl.add_argument("--json", action="store_true"); _add_common(cl)
    cs = cards.add_parser("show", help="show one card"); cs.add_argument("id", type=int)
    cs.add_argument("--json", action="store_true"); _add_common(cs)
    ca = cards.add_parser("add", help="create a card")
    ca.add_argument("--project", required=True, help="project id or slug")
    # #650: a TITLE is the field most likely to carry a command name or code
    # identifier — the exact strings with backticks — so #458's file escape
    # hatch applies here too, not only on --body.
    add_content_args(ca, "--title", required=True,
                     noun="card title", noun_plural="titles")
    add_content_args(ca, "--body", required=False); ca.add_argument("--ai2", action="store_true")
    ca.add_argument("--priority", type=int, default=0)
    ca.add_argument("--due", default=None, metavar="DATE",
                    help="due date (YYYY-MM-DD or ISO datetime); sent as due_at")
    ca.add_argument("--label", action="append", dest="labels", metavar="LABEL",
                    help="attach a label (repeatable)")
    ca.add_argument("--json", action="store_true"); _add_common(ca)
    cm = cards.add_parser("move", help="move a card to a stage")
    cm.add_argument("id", type=int); cm.add_argument("stage"); cm.add_argument("--json", action="store_true"); _add_common(cm)
    ck = cards.add_parser("link", help="add/update a link on a card (merges)")
    ck.add_argument("id", type=int); ck.add_argument("key"); ck.add_argument("value")
    ck.add_argument("--json", action="store_true"); _add_common(ck)
    cn = cards.add_parser("assign", help="set a card's assignee")
    cn.add_argument("id", type=int); cn.add_argument("assignee"); cn.add_argument("--json", action="store_true"); _add_common(cn)
    cb = cards.add_parser("label", help="add/remove a label on a card")
    cb.add_argument("action", choices=["add", "rm"])
    cb.add_argument("id", type=int); cb.add_argument("label")
    cb.add_argument("--json", action="store_true"); _add_common(cb)
    ct = cards.add_parser("thread", help="show the card's conversation (#181: a card IS a thread)")
    ct.add_argument("id", type=int)
    ct.add_argument("--limit", type=int, default=None, help="max messages (gateway caps at 1000)")
    _add_common(ct)
    cy = cards.add_parser("say", help="post a message onto the card's thread")
    cy.add_argument("id", type=int)
    add_content_args(cy)
    cy.add_argument("--kind", default="fyi", help="status|question|answer|unblock|fyi")
    cy.add_argument("--to", dest="to_node", default=None,
                    help="recipient peer (default: the card's assignee)")
    _add_common(cy)

    ck = cards.add_parser(
        "ask", intermixed=True,
        help="MINT an obligation on this card: name who owes what, as a ROW")
    ck.add_argument("id", type=int)
    ck.add_argument("holder", nargs="?", default=None,
                    help="the peer who OWES the delivery (optional since #591: "
                         "omit it, or pass --holder, to mint a `requested` row)")
    ck.add_argument("what", help="what is owed, in one line")
    ck.add_argument("--timeout-hours", type=int, default=None,
                    help="hours until this obligation reads OVERDUE (default: none, "
                         "which means it never goes red on its own)")
    ck.add_argument("--accept", default=None, metavar='"PASS = ... | FAIL = ..."',
                    help="the falsifiable check that closes this obligation "
                         "(#532). Omitting it mints an obligation that reads RED "
                         "in the sweep; a check with no FAIL branch is marked "
                         "NO-FAIL-BRANCH — 'verify it works' is not a check")
    ck.add_argument("--kind", default="action", help="obligation kind (default: action)")
    # #591: the step graph. `--holder` is an OPTION with its own dest — sharing
    # the positional's dest makes `--holder me --step build "<what>"` parse
    # holder=None (the `?` positional's empty match overwrites the option).
    ck.add_argument("--step", default=None, metavar="STEP",
                    help="the menu step this row fills (build, validate, plan-review, "
                         "...). Omitting it mints an `unstepped` row that buys no gate "
                         "credit — the gateway says so on success")
    ck.add_argument("--needs", action="append", default=None, metavar="S[,S]",
                    help="explicit edges by step name (added to the menu's); repeatable, "
                         "comma lists and the contract's `+step` spelling all accepted")
    ck.add_argument("--hours", type=int, default=None,
                    help="clock override, up to 4x the menu value")
    ck.add_argument("--holder", dest="holder_flag", default=None, metavar="PEER|me",
                    help="who owes it: a peer (offered), `me` (open: a bound self-ask "
                         "is a take), or omit (requested: the eligible set is DM'd)")
    ck.add_argument("--done", default=None, metavar="EVIDENCE",
                    help="mint + take + close in ONE act (needs --holder me); the "
                         "container the step names — a ref, >=40 words, or a verdict")
    ck.add_argument("--json", action="store_true"); _add_common(ck)

    cg = cards.add_parser("graph", help="read the card's step graph (#591): every menu "
                                        "step, its state, holder, due, edges, eligible set")
    cg.add_argument("id", type=int)
    cg.add_argument("--json", action="store_true"); _add_common(cg)

    cr = cards.add_parser("ready", help="flag a card ready-to-advance (move_ready) for the orchestrator")
    cr.add_argument("id", type=int); cr.add_argument("--clear", action="store_true", help="unset move_ready")
    cr.add_argument("--json", action="store_true"); _add_common(cr)

    ce = cards.add_parser(
        "edit", help="edit a card's title and/or body (#191: a correction "
                     "belongs ON the card, not only in its thread)")
    ce.add_argument("id", type=int)
    add_content_args(ce, "--title", required=False,
                     noun="card title", noun_plural="titles")
    add_content_args(ce, "--body", required=False)
    ce.add_argument("--due", default=_SENTINEL, metavar="DATE",
                    help="set due date (YYYY-MM-DD or ISO); pass empty string to clear")
    ce.add_argument("--json", action="store_true"); _add_common(ce)

    obl = top.add_parser(
        "obligations",
        help="card obligations — the close act (#562)").add_subparsers(
        dest="command", required=True)
    oc = obl.add_parser(
        "close",
        help="CLOSE an obligation: the DISTINCT ACT that records the check's "
             "outcome plus the evidence observed. A thread reply does NOT "
             "close an obligation (#562) — this does")
    oc.add_argument("id", type=int, help="the obligation id")
    oc.add_argument("--outcome", required=True,
                    choices=("pass", "fail", "cannot_evaluate", "skipped"),
                    help="the accept check's OUTCOME (`skipped` only on an "
                         "optional step — #591)")
    oc.add_argument("--evidence", required=True,
                    help="what you OBSERVED running the check — whitespace is "
                         "refused: whitespace evidence is the vibe-close this "
                         "endpoint exists to kill")
    oc.add_argument("--json", action="store_true"); _add_common(oc)

    ol = obl.add_parser(
        "list", help="list obligations — the READ half (#590): an obligation "
                     "that can be minted and closed but never read makes the "
                     "holder the only auditor")
    ol.add_argument("--status", choices=["open", "closed", "fallback_fired"])
    ol.add_argument("--holder", help="only obligations this peer owes")
    ol.add_argument("--card", type=int, dest="card_id", help="only this card")
    ol.add_argument("--overdue", action="store_true",
                    help="only open obligations past their timeout")
    ol.add_argument("--json", action="store_true"); _add_common(ol)

    # #591: the row lifecycle beyond close. take = the comprehension receipt
    # (silence is not evidence); decline = an ignored offer must not be free
    # and indistinguishable from never seeing it; amend = re-plan by the asker
    # or card assignee, mint-time fields only while requested/offered.
    ot = obl.add_parser("take", help="TAKE a requested/offered obligation: the receipt "
                                     "that you read it; starts the clock")
    ot.add_argument("id", type=int, help="the obligation id")
    ot.add_argument("--json", action="store_true"); _add_common(ot)
    od = obl.add_parser("decline", help="DECLINE an offer: removes you from the row's "
                                        "eligible set and prints who remains")
    od.add_argument("id", type=int, help="the obligation id")
    od.add_argument("--why", required=True, help="why you decline, in one line")
    od.add_argument("--json", action="store_true"); _add_common(od)
    oa = obl.add_parser("amend", help="AMEND a row's step/holder/accept/hours/edges "
                                      "(asker or card assignee; `--step` also backfills "
                                      "a pre-v0.4 unstepped row)")
    oa.add_argument("id", type=int, help="the obligation id")
    oa.add_argument("--step", default=None, metavar="STEP")
    oa.add_argument("--holder", default=None, metavar="PEER",
                    help="reassign; pass an empty string to clear (back to requested)")
    oa.add_argument("--accept", default=None, metavar='"PASS = ... | FAIL = ..."')
    oa.add_argument("--hours", type=int, default=None, help="clock override, up to 4x the menu value")
    oa.add_argument("--needs-add", action="append", dest="needs_add", default=None,
                    metavar="STEP", help="add an edge (repeatable)")
    oa.add_argument("--needs-remove", action="append", dest="needs_remove", default=None,
                    metavar="STEP", help="remove an edge (repeatable; asker/assignee only)")
    oa.add_argument("--needs", action="append", default=None, metavar="+STEP|-STEP",
                    help="the contract's signed spelling: `+step` adds, `-step` removes "
                         "(write the latter `--needs=-step`; a bare `-step` reads as a flag)")
    oa.add_argument("--json", action="store_true"); _add_common(oa)
    return p


def _out(status: int, data, ok_render, as_json: bool) -> int:
    if status and 200 <= status < 300:
        print(json.dumps(data, indent=2) if as_json else ok_render(data))
        return 0
    print(f"swarph board: gateway {status or 'unreachable'}: {data.get('detail', data)}", file=sys.stderr)
    return 1


def _resolve_project(gw: str, token: str, value) -> tuple[Optional[int], Optional[str]]:
    """(project_id, error) — passthrough numeric, else slug→id via GET /board/projects."""
    if value is None or str(value).isdigit():
        return (int(value) if value is not None else None), None
    st, d = _http_get_json(f"{gw}/board/projects", token)
    if not (st and 200 <= st < 300):
        return None, f"cannot list projects to resolve {value!r}: {d.get('detail', d)}"
    pid = _project_ref_to_id(value, d if isinstance(d, list) else d.get("projects", []))
    return (pid, None) if pid is not None else (None, f"unknown project slug {value!r}")


def run_board(argv: list[str]) -> int:
    # `merge-check` is dispatched BEFORE token resolution on purpose: it reads a card
    # JSON and queries gh, and needs no gateway token. Requiring one would make the
    # dry-run unusable on exactly the cells that hold a GitHub credential but no board
    # token (card #137).
    if argv and argv[0] == "merge-check":
        from swarph_cli.commands.board_merge_check import run_board_merge_check
        return run_board_merge_check(argv[1:])
    args = _build_parser().parse_args(argv)
    try:
        self_name = _resolve_self_name(args.self_name)
        token = _resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph board: {exc}", file=sys.stderr)
        return 1
    gw = args.gateway.rstrip("/")
    aj = getattr(args, "json", False)

    if args.group == "projects":
        if args.command == "list":
            st, d = _http_get_json(f"{gw}/board/projects", token)
            return _out(st, d, _format_projects, aj)
        if args.command == "add":
            st, d = _post_json(f"{gw}/board/projects", _project_add_payload(self_name, args.slug, args.title, goal=args.goal), token)
            return _out(st, d, lambda x: f"created project #{x.get('id')} ({x.get('slug')})", aj)

    if args.group == "obligations":
        if args.command == "close":
            # #562's CLI half: the explicit close act. The gateway guards the
            # identity (holder, creator, or board orchestrator) and refuses a
            # second close; the CLI's job is to refuse what it can see —
            # whitespace evidence — and to propagate the gateway's refusals
            # with their detail intact.
            evidence = args.evidence
            if not evidence.strip():
                print("swarph board obligations close: --evidence is "
                      "whitespace-only — the close act records what you "
                      "OBSERVED, and nothing was observed", file=sys.stderr)
                return 2
            st, d = _post_json(
                f"{gw}/board/obligations/{args.id}/close",
                {"outcome": args.outcome, "evidence": evidence}, token)
            if st < 200 or st >= 300:
                detail = d.get("detail", d) if isinstance(d, dict) else d
                print(f"swarph board obligations close: gateway {st}: {_s(detail)}",
                      file=sys.stderr)
                return 1
            if aj:
                print(json.dumps(d, indent=2))
            else:
                print(f"obligation #{_s(d.get('id', args.id))} CLOSED by "
                      f"{_s(d.get('closed_by', self_name))} — outcome: "
                      f"{_s(d.get('close_outcome', args.outcome))}")
            return 0
        if args.command == "list":
            st, d = _http_get_json(
                _obligations_list_url(gw, status=args.status, holder=args.holder,
                                      card_id=args.card_id, overdue=args.overdue),
                token)
            return _out(st, d, _format_obligations, aj)
        if args.command == "take":
            # The gateway takes no body (caller = token); `_post_json` always
            # serialises a dict, so send the empty one.
            st, d = _post_json(f"{gw}/board/obligations/{args.id}/take", {}, token)
            return _out(st, d, _obligation_act_line, aj)
        if args.command == "decline":
            st, d = _post_json(f"{gw}/board/obligations/{args.id}/decline",
                               {"why": args.why}, token)
            return _out(st, d, _obligation_act_line, aj)
        if args.command == "amend":
            try:
                patch = _amend_payload(step=args.step, holder=args.holder,
                                       accept=args.accept, hours=args.hours,
                                       needs_add=args.needs_add,
                                       needs_remove=args.needs_remove, needs=args.needs)
            except ValueError as exc:
                print(f"swarph board obligations amend: {exc}", file=sys.stderr)
                return 2
            st, d = _patch_json(f"{gw}/board/obligations/{args.id}/amend", patch, token)
            return _out(st, d, _obligation_act_line, aj)

    if args.group == "cards":
        if args.command == "list":
            pid, err = _resolve_project(gw, token, args.project)
            if err:
                print(f"swarph board: {err}", file=sys.stderr)
                return 1
            st, d = _http_get_json(_cards_list_url(gw, project=pid, stage=args.stage,
                                                   assignee=args.assignee,
                                                   label=getattr(args, "label", None)), token)
            return _out(st, d, _format_cards, aj)
        if args.command == "show":
            st, d = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            return _out(st, d, _format_card, aj)
        if args.command == "add":
            if args.title == "-" and args.body == "-":
                # Two stdin readers, one stdin: the second read gets "", and ""
                # is a REAL value (it clears) — refuse rather than post a
                # titleless card that reports success (#256's class).
                print("swarph board cards add: only one field can read stdin — "
                      "pass the other as --title-file/--body-file", file=sys.stderr)
                return 1
            try:
                body = resolve_content(args.body, getattr(args, "body_file", None), "--body")
                title = resolve_content(args.title, getattr(args, "title_file", None), "--title")
            except ContentError as exc:
                print(f"swarph board cards add: {exc}", file=sys.stderr)
                return 1
            if title is not None:
                # A title is a one-line field; a body is not. Strip the single
                # trailing newline an editor or `echo` appends to a title file —
                # byte-identical is --body's contract, not --title's.
                title = title.removesuffix("\n")
            pid, err = _resolve_project(gw, token, args.project)
            if err or pid is None:
                print(f"swarph board: {err or 'project required'}", file=sys.stderr)
                return 1
            st, d = _post_json(f"{gw}/board/cards", _card_add_payload(
                self_name, pid, title, body=body, ai2=args.ai2,
                priority=args.priority, labels=getattr(args, "labels", None),
                due_at=args.due), token)
            return _out(st, d, lambda x: f"created card #{x.get('id')} [{x.get('stage')}] (stage defaults to proposed — use `cards move` to advance)", aj)
        if args.command == "label":
            # READ the current set before replacing it. If the GET fails we must
            # NOT write — a PATCH built on a failed read would silently CLOBBER
            # every other label on the card. Refuse loudly instead.
            st, cur = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            if st != 200 or not isinstance(cur, dict):
                print(f"swarph board: cannot read card #{args.id} to modify its "
                      f"labels (HTTP {st}) — refusing to write, a replacement built "
                      f"on a failed read would drop the card's other labels",
                      file=sys.stderr)
                return 1
            new = _apply_label(cur.get("labels") or [], args.action, args.label)
            st, d = _patch_json(f"{gw}/board/cards/{args.id}",
                                {"actor": self_name, "labels": new}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} labels -> "
                                         f"{', '.join(x.get('labels') or []) or '(none)'}", aj)
        if args.command == "move":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "stage": args.stage}, token)
            return _out(st, d, _move_line, aj)
        if args.command == "assign":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "assignee": args.assignee}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} assignee -> {x.get('assignee')}", aj)
        if args.command == "edit":
            if args.title == "-" and args.body == "-":
                # As in `add`: two stdin readers, one stdin — and on edit the
                # drained second read is "", which CLEARS the stored title
                # while the CLI prints success.
                print("swarph board cards edit: only one field can read stdin — "
                      "pass the other as --title-file/--body-file", file=sys.stderr)
                return 1
            try:
                body_text = resolve_content(args.body, getattr(args, "body_file", None), "--body")
                title_text = resolve_content(args.title, getattr(args, "title_file", None), "--title")
            except ContentError as exc:
                print(f"swarph board cards edit: {exc}", file=sys.stderr)
                return 1
            if title_text is not None:
                title_text = title_text.removesuffix("\n")  # one-line field; see add
            try:
                patch = _card_edit_payload(
                    self_name, title_text, body_text, due_at=args.due)
            except ValueError as exc:
                print(f"swarph board cards edit: {exc}", file=sys.stderr)
                return 2
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", patch, token)
            # Surface body_version: it is the latch every verdict stamp keys on
            # (#199), so an edit that re-opens reviewed work is visible in the
            # success line, not only in a later audit.
            return _out(st, d, lambda x: f"card #{x.get('id')} edited "
                                         f"(body_version={x.get('body_version')})", aj)
        if args.command == "thread":
            st, d = _http_get_json(_thread_url(gw, args.id, limit=args.limit), token)
            # NO special-casing of 403/409. `_out` already returns 1 and prints the
            # gateway's `detail` verbatim for every non-2xx, so the refusal reaches
            # the operator intact — a 409 ("predates #181a") and a 403 ("not
            # readable") are never rendered as an empty thread.
            # >>> A HAND-WRITTEN BRANCH HERE WAS DELETED: it re-printed the same
            # detail under a different prefix, carried a comment asserting it
            # prevented a flattening `_out` never did, and a mutation that removed
            # it PASSED ALL 15 TESTS — the definition of a code path doing nothing.
            # Reading it would not have found that; mutating it did. <<<
            return _out(st, d, _format_thread, aj)
        if args.command == "ask":
            # >>> THE ASK IS THE MINT. <<< #307 requirement (2): the expectation must
            # be created BY THE ACT of asking, or it is one more thing to remember and
            # decays exactly like the prose it replaces. So there is no separate
            # "record an obligation" verb, deliberately — this posts the request AND
            # the row in one gateway call, or neither.
            if args.holder is not None and args.holder_flag is not None:
                print("swarph board cards ask: holder given twice (positional and "
                      "--holder) — pass one", file=sys.stderr)
                return 2
            holder = args.holder_flag or args.holder
            if holder is None and args.step is None:
                # The gateway 400s this too ("a holder-less ask needs --step"),
                # but the likeliest cause is the pre-#591 form with "<what>"
                # forgotten — `ask 20 lab-ovh --accept …` parses what='lab-ovh'
                # — and only the CLI can name that.
                print(f"swarph board cards ask: no holder and no --step — pass --step S "
                      f"(the step's eligible set is asked) or a holder (--holder P|me, "
                      f"or positional). If {args.what!r} was meant as the holder, the "
                      f"\"<what>\" text is missing", file=sys.stderr)
                return 2
            body = _ask_payload(self_name, args.what, holder=holder,
                                step=args.step, needs=args.needs, hours=args.hours,
                                done=args.done, accept=args.accept, kind=args.kind,
                                timeout_hours=args.timeout_hours)
            st, d = _post_json(f"{gw}/board/cards/{args.id}/ask", body, token)
            return _out(st, d, _ask_line, args.json)
        if args.command == "graph":
            st, d = _http_get_json(f"{gw}/board/cards/{args.id}/graph", token)
            return _out(st, d, _format_graph, aj)
        if args.command == "say":
            try:
                content = resolve_content(args.content, getattr(args, "content_file", None))
            except ContentError as exc:
                print(f"swarph board cards say: {exc}", file=sys.stderr)
                return 1
            st, card = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            if st != 200:
                print(f"swarph board cards say: cannot read card #{args.id}: "
                      f"{card.get('detail', card)}", file=sys.stderr)
                return 1
            thread_uuid = card.get("thread_uuid")
            if not thread_uuid:
                # Named refusal, not a silent no-op. Same reason the gateway 409s.
                print(f"swarph board cards say: card #{args.id} has no bound thread "
                      f"(it predates #181a). Run scripts/migrate_card_threads.py on "
                      f"the gateway host.", file=sys.stderr)
                return 1
            try:
                to_node = _thread_recipient(card, args.to_node)
            except RuntimeError as exc:
                print(f"swarph board cards say: {exc}", file=sys.stderr)
                return 1
            st, d = _post_json(
                f"{gw}/messages",
                _card_say_payload(self_name, to_node, args.kind, content, thread_uuid),
                token,
            )
            # A 403 here carries the gateway's explanation that attaching PUBLISHES
            # to everyone who can read the card, now and in future, so it needs an
            # explicit `propose` grant. `_out` passes `detail` through whole; it does
            # not need help, and a second copy of that logic is a second thing to
            # keep in sync.
            return _out(st, d, lambda x: _say_line(x, args.id, to_node), aj)
        if args.command == "ready":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "move_ready": not args.clear}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} move_ready -> {x.get('move_ready')}", aj)
        if args.command == "link":
            gst, gcard = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            if not (gst and 200 <= gst < 300):
                return _out(gst, gcard, lambda x: x, aj)
            merged = _merge_link(gcard.get("links"), args.key, args.value)
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "links": merged}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} link {args.key}={args.value}", aj)

    print("swarph board: unknown subcommand", file=sys.stderr)
    return 1
