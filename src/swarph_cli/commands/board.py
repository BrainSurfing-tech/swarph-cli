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

def _cards_list_url(gateway: str, *, project=None, stage=None, assignee=None) -> str:
    q = {k: v for k, v in (("project", project), ("stage", stage), ("assignee", assignee)) if v}
    base = f"{gateway.rstrip('/')}/board/cards"
    return f"{base}?{urllib.parse.urlencode(q)}" if q else base


def _card_add_payload(actor, project_id, title, *, body=None, ai2=False, priority=0) -> dict:
    p = {"actor": actor, "project_id": int(project_id), "title": title,
         "ai2": bool(ai2), "priority": int(priority)}
    if body:
        p["body"] = body
    return p


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
                     f"{c.get('project_id',''):>4} {ai2:<3} {c.get('title','')}")
    return "\n".join(lines)


def _format_card(card: dict) -> str:
    lines = [
        f"#{card.get('id')}  [{card.get('stage')}]  project={card.get('project_id')}"
        f"  assignee={card.get('assignee') or '-'}  {'AI²' if card.get('ai2') else ''}",
        f"  {card.get('title','')}",
    ]
    if card.get("body"):
        lines.append(f"\n{card['body']}")
    links = card.get("links") or {}
    if links:
        lines.append("\nlinks:")
        for k, v in links.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _format_projects(data) -> str:
    rows = data.get("projects", []) if isinstance(data, dict) else (data or [])
    if not rows:
        return "(no projects)"
    lines = [f"{'ID':>4}  {'SLUG':<22} TITLE"]
    for p in rows:
        lines.append(f"{p.get('id',''):>4}  {p.get('slug',''):<22} {p.get('title') or ''}")
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
    cl.add_argument("--json", action="store_true"); _add_common(cl)
    cs = cards.add_parser("show", help="show one card"); cs.add_argument("id", type=int)
    cs.add_argument("--json", action="store_true"); _add_common(cs)
    ca = cards.add_parser("add", help="create a card")
    ca.add_argument("--project", required=True, help="project id or slug"); ca.add_argument("--title", required=True)
    ca.add_argument("--body"); ca.add_argument("--ai2", action="store_true")
    ca.add_argument("--priority", type=int, default=0); ca.add_argument("--json", action="store_true"); _add_common(ca)
    cm = cards.add_parser("move", help="move a card to a stage")
    cm.add_argument("id", type=int); cm.add_argument("stage"); cm.add_argument("--json", action="store_true"); _add_common(cm)
    ck = cards.add_parser("link", help="add/update a link on a card (merges)")
    ck.add_argument("id", type=int); ck.add_argument("key"); ck.add_argument("value")
    ck.add_argument("--json", action="store_true"); _add_common(ck)
    cn = cards.add_parser("assign", help="set a card's assignee")
    cn.add_argument("id", type=int); cn.add_argument("assignee"); cn.add_argument("--json", action="store_true"); _add_common(cn)
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
            st, d = _http_get_json(_cards_list_url(gw, project=pid, stage=args.stage, assignee=args.assignee), token)
            return _out(st, d, _format_cards, aj)
        if args.command == "show":
            st, d = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            return _out(st, d, _format_card, aj)
        if args.command == "add":
            pid, err = _resolve_project(gw, token, args.project)
            if err or pid is None:
                print(f"swarph board: {err or 'project required'}", file=sys.stderr)
                return 1
            st, d = _post_json(f"{gw}/board/cards", _card_add_payload(self_name, pid, args.title, body=args.body, ai2=args.ai2, priority=args.priority), token)
            return _out(st, d, lambda x: f"created card #{x.get('id')} [{x.get('stage')}] (stage defaults to proposed — use `cards move` to advance)", aj)
        if args.command == "move":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "stage": args.stage}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} -> {x.get('stage')}", aj)
        if args.command == "assign":
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "assignee": args.assignee}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} assignee -> {x.get('assignee')}", aj)
        if args.command == "link":
            gst, gcard = _http_get_json(f"{gw}/board/cards/{args.id}", token)
            if not (gst and 200 <= gst < 300):
                return _out(gst, gcard, lambda x: x, aj)
            merged = _merge_link(gcard.get("links"), args.key, args.value)
            st, d = _patch_json(f"{gw}/board/cards/{args.id}", {"actor": self_name, "links": merged}, token)
            return _out(st, d, lambda x: f"card #{x.get('id')} link {args.key}={args.value}", aj)

    print("swarph board: unknown subcommand", file=sys.stderr)
    return 1
