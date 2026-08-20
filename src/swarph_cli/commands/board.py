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
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from swarph_cli.commands._content import ContentError, add_content_args, resolve_content
from swarph_cli.commands._display import sanitize_terminal as _s
from swarph_cli.commands.mesh import (
    _add_common,
    _http_get_json,
    _post_json,
    _resolve_self_name,
    _resolve_token,
)


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


def _card_add_payload(actor, project_id, title, *, body=None, ai2=False, priority=0,
                      labels=None) -> dict:
    p = {"actor": actor, "project_id": int(project_id), "title": title,
         "ai2": bool(ai2), "priority": int(priority)}
    if body:
        p["body"] = body
    if labels:
        p["labels"] = list(labels)
    return p


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


def _format_cards(data) -> str:
    rows = _cards_of(data)
    if not rows:
        return "(no cards)"
    lines = [f"{'ID':>4}  {'STAGE':<9} {'PRJ':>4} {'AI²':<3} TITLE"]
    for c in rows:
        ai2 = "AI²" if c.get("ai2") else ""
        lines.append(f"{c.get('id',''):>4}  {c.get('stage',''):<9} "
                     f"{c.get('project_id',''):>4} {ai2:<3} {_s(c.get('title'))}")
    return "\n".join(lines)


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


def _card_say_payload(from_node: str, to_node: str, kind: str, content: str,
                      thread_uuid: str) -> dict:
    return {
        "from_node": from_node,
        "to_node": to_node,
        "kind": kind,
        "content": content,
        "thread_id": thread_uuid,
    }


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
    deadline = f"overdue after {when}" if when else (
        "NO TIMEOUT — this never goes red on its own; pass --timeout-hours if it should")
    accept = d.get("accept")
    if not accept:
        falsifier = ("NO ACCEPT CHECK — reads RED in the sweep; pass --accept "
                     "\"PASS = ... | FAIL = ...\" naming an observable and a way "
                     "it comes out negative")
    elif "fail" not in str(accept).lower():
        falsifier = ("accept check has NO FAIL BRANCH — marked NO-FAIL-BRANCH in "
                     "the sweep; a check that cannot come out negative is not one")
    else:
        falsifier = f"accept: {accept}"
    return (f"obligation #{d.get('id')} on card #{d.get('card_id')}: "
            f"{d.get('holder')} owes it, status={d.get('status')}, {deadline}\n"
            f"  {falsifier}\n"
            f"  thread {d.get('thread_uuid')} — the holder's reply in this thread closes it")


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


# ── parser + dispatch ─────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph board", description="Mesh board (projects + cards).")
    top = p.add_subparsers(dest="group", required=True)

    proj = top.add_parser("projects", help="board projects").add_subparsers(dest="command", required=True)
    pl = proj.add_parser("list", help="list projects"); pl.add_argument("--json", action="store_true"); _add_common(pl)
    pa = proj.add_parser("add", help="create a project")
    pa.add_argument("slug"); pa.add_argument("--title", required=True); pa.add_argument("--goal")
    pa.add_argument("--json", action="store_true"); _add_common(pa)

    cards = top.add_parser("cards", help="board cards").add_subparsers(dest="command", required=True)
    cl = cards.add_parser("list", help="list cards")
    cl.add_argument("--project"); cl.add_argument("--stage"); cl.add_argument("--assignee")
    cl.add_argument("--label", help="only cards carrying this label (exact match)")
    cl.add_argument("--json", action="store_true"); _add_common(cl)
    cs = cards.add_parser("show", help="show one card"); cs.add_argument("id", type=int)
    cs.add_argument("--json", action="store_true"); _add_common(cs)
    ca = cards.add_parser("add", help="create a card")
    ca.add_argument("--project", required=True, help="project id or slug"); ca.add_argument("--title", required=True)
    add_content_args(ca, "--body", required=False); ca.add_argument("--ai2", action="store_true")
    ca.add_argument("--priority", type=int, default=0)
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
        "ask", help="MINT an obligation on this card: name who owes what, as a ROW")
    ck.add_argument("id", type=int)
    ck.add_argument("holder", help="the peer who OWES the delivery")
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
    ck.add_argument("--json", action="store_true"); _add_common(ck)

    cr = cards.add_parser("ready", help="flag a card ready-to-advance (move_ready) for the orchestrator")
    cr.add_argument("id", type=int); cr.add_argument("--clear", action="store_true", help="unset move_ready")
    cr.add_argument("--json", action="store_true"); _add_common(cr)
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
            try:
                body = resolve_content(args.body, getattr(args, "body_file", None), "--body")
            except ContentError as exc:
                print(f"swarph board cards add: {exc}", file=sys.stderr)
                return 1
            pid, err = _resolve_project(gw, token, args.project)
            if err or pid is None:
                print(f"swarph board: {err or 'project required'}", file=sys.stderr)
                return 1
            st, d = _post_json(f"{gw}/board/cards", _card_add_payload(
                self_name, pid, args.title, body=body, ai2=args.ai2,
                priority=args.priority, labels=getattr(args, "labels", None)), token)
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
            return _out(st, d, lambda x: f"card #{x.get('id')} -> {x.get('stage')}", aj)
        if args.command == "assign":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "assignee": args.assignee}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} assignee -> {x.get('assignee')}", aj)
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
            body = {"holder": args.holder, "what": args.what, "created_by": self_name,
                    "kind": args.kind}
            if args.timeout_hours is not None:
                body["timeout_hours"] = args.timeout_hours
            if args.accept is not None:
                body["accept"] = args.accept
            st, d = _post_json(f"{gw}/board/cards/{args.id}/ask", body, token)
            return _out(st, d, _format_ask, args.json)
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
            return _out(st, d, lambda x: f"posted id={x.get('id')} onto card "
                                         f"#{args.id} (to {to_node})", aj)
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
