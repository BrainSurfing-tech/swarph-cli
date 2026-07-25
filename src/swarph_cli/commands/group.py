"""``swarph group`` / ``swarph rights`` — CLI wrappers over the mesh-gateway
RBAC group endpoints (board card #103).

The mesh RBAC group primitive (named groups of peers with grants attached) is
server-only, reachable via raw HTTP. This wraps it with the same ergonomics as
``swarph board``/``swarph channel`` (``--as``/``--gateway``/``--token-file``/
``--json``). Pure helpers (URL builders, payload builders, formatters) are
unit-tested; HTTP is the seam (reused from ``_gateway_client``). Contract from
the droplet gateway spec §4:
  POST   /groups                        {name, description, kind}      -> 201
  GET    /groups                         -> [{name,kind,description,member_count,created_by,created_at}]
  DELETE /groups/{name}                  -> 204 (cascades grants+members)
  GET    /groups/{name}/members          -> [{peer,added_by,added_at}]
  POST   /groups/{name}/members          {peer}  -> 201
  DELETE /groups/{name}/members/{peer}   -> 204
  GET    /groups/{name}/grants           -> [{grant_type,target,level,added_by,added_at}]
  POST   /groups/{name}/grants           {grant_type,target,level} -> 201
  DELETE /groups/{name}/grants           {grant_type,target}       -> 204 (body carries grant_type+target)
  POST   /authz/check    {peer, grant_type, target}  -> {allow, via_group, level}
  GET    /peers/{peer}/grants   -> {peer, groups:[...], grants:[{grant_type,target,level,via_group,direct}]}
The gateway endpoints are not deployed yet; this client is verified fully
offline against mocked HTTP helpers.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from swarph_cli.commands._display import sanitize_terminal as _s
from swarph_cli.commands._gateway_client import (
    add_common_args,
    delete_json,
    get_json,
    post_json,
    resolve_self_name,
    resolve_token,
)


# ── HTTP: DELETE-with-body (revoke needs {grant_type,target} on the DELETE) ──

def _delete_json_body(url: str, body: dict, token: str, *, timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="DELETE",
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

def _group_url(gateway: str, name: str) -> str:
    return f"{gateway.rstrip('/')}/groups/{urllib.parse.quote(name, safe='')}"


def _with_actor(url: str, actor: str) -> str:
    """Append ?actor=<self>. The gateway reads `actor` as a QUERY PARAMETER on
    every DELETE (groups_delete / groups_member_remove / groups_grant_remove all
    declare it in the signature, not in a body model). Sending it in a JSON body
    — which the CLI did until card #114 — arrives as None, `_board_actor` then
    resolves a non-orchestrator under the shared-token regime, and the gateway
    returns 403. Verified live: body -> 403, ?actor=lab-ovh -> 200."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}actor={urllib.parse.quote(actor, safe='')}"


def _revoke_url(gateway: str, name: str, grant_type: str, target: str, actor: str) -> str:
    """DELETE /groups/{name}/grants takes grant_type, target AND actor as QUERY
    params — `groups_grant_remove(name, grant_type, target, actor, ...)`.

    The §4 contract said the body carried grant_type+target and the CLI was built
    to that; the implementation diverged. Body-encoded, the request reaches the
    server with the required query params missing. Unit tests could not catch it
    because they mock the HTTP layer — only a live call does."""
    q = urllib.parse.urlencode({"grant_type": grant_type, "target": target, "actor": actor})
    return f"{_grants_url(gateway, name)}?{q}"


def _members_url(gateway: str, name: str) -> str:
    return f"{_group_url(gateway, name)}/members"


def _member_url(gateway: str, name: str, peer: str) -> str:
    return f"{_members_url(gateway, name)}/{urllib.parse.quote(peer, safe='')}"


def _grants_url(gateway: str, name: str) -> str:
    return f"{_group_url(gateway, name)}/grants"


def _peer_grants_url(gateway: str, peer: str) -> str:
    return f"{gateway.rstrip('/')}/peers/{urllib.parse.quote(peer, safe='')}/grants"


def _group_create_payload(name: str, description: Optional[str], kind: str) -> dict:
    p = {"name": name, "kind": kind}
    if description:
        p["description"] = description
    return p


def _member_add_payload(peer: str) -> dict:
    return {"peer": peer}


def _grant_add_payload(grant_type: str, target: str, level: str) -> dict:
    return {"grant_type": grant_type, "target": target, "level": level}


def _revoke_payload(grant_type: str, target: str) -> dict:
    return {"grant_type": grant_type, "target": target}


def _check_payload(peer: str, grant_type: str, target: str) -> dict:
    return {"peer": peer, "grant_type": grant_type, "target": target}


# ── formatters (unit-tested) ──────────────────────────────────────────────────

def _rows_of(data, key: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get(key, []) or []
    return []


def _format_groups(data) -> str:
    rows = _rows_of(data, "groups")
    if not rows:
        return "(no groups)"
    lines = []
    for g in rows:
        desc = _s(g.get("description")) if g.get("description") else ""
        lines.append(
            f"{_s(g.get('name'))}  {_s(g.get('kind'))}  "
            f"({g.get('member_count', 0)} members)  {desc}".rstrip()
        )
    return "\n".join(lines)


def _format_members(data) -> str:
    rows = _rows_of(data, "members")
    if not rows:
        return "(no members)"
    return "\n".join(_s(m.get("peer")) for m in rows)


def _format_grants(data) -> str:
    rows = _rows_of(data, "grants")
    if not rows:
        return "(no grants)"
    return "\n".join(
        f"{_s(g.get('grant_type'))}  {_s(g.get('target'))}  {_s(g.get('level'))}" for g in rows
    )


def _format_rights(data) -> str:
    peer = _s(data.get("peer")) if isinstance(data, dict) else ""
    groups = (data.get("groups") or []) if isinstance(data, dict) else []
    lines = [f"{peer} — groups: {', '.join(_s(g) for g in groups)}"]
    grants = (data.get("grants") or []) if isinstance(data, dict) else []
    if not grants:
        lines.append("(no grants)")
    else:
        for g in grants:
            # The gateway marks each effective grant with a `direct` bool
            # (peer-direct vs group-inherited). On a live UNION endpoint a
            # (grant_type,target) can be present both directly AND via a group,
            # so `direct` is the canonical signal — trust it over inferring from
            # via_group. Fall back to the via_group==null heuristic for an older
            # gateway that predates the field.
            is_direct = g["direct"] if "direct" in g else g.get("via_group") is None
            via = "direct" if is_direct else _s(g.get("via_group"))
            lines.append(
                f"{_s(g.get('grant_type'))}  {_s(g.get('target'))}  {_s(g.get('level'))}  (via {via})"
            )
    return "\n".join(lines)


def _format_check(data) -> str:
    if not isinstance(data, dict) or not data.get("allow"):
        return "allow=false"
    via = _s(data.get("via_group")) if data.get("via_group") else "direct"
    return f"allow=true via {via} ({_s(data.get('level'))})"


# ── parsers ────────────────────────────────────────────────────────────────

def _build_group_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph group", description="Mesh RBAC groups.")
    sub = p.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a group")
    create.add_argument("name")
    create.add_argument("--description", default=None)
    create.add_argument("--kind", default="custom", choices=["role", "custom"])
    create.add_argument("--json", action="store_true")
    add_common_args(create)

    listp = sub.add_parser("list", help="list groups")
    listp.add_argument("--json", action="store_true")
    add_common_args(listp)

    delete = sub.add_parser("delete", help="delete a group")
    delete.add_argument("name")
    delete.add_argument("--json", action="store_true")
    add_common_args(delete)

    members = sub.add_parser("members", help="list group members")
    members.add_argument("name")
    members.add_argument("--json", action="store_true")
    add_common_args(members)

    addp = sub.add_parser("add", help="add a peer to a group")
    addp.add_argument("name")
    addp.add_argument("peer")
    addp.add_argument("--json", action="store_true")
    add_common_args(addp)

    remove = sub.add_parser("remove", help="remove a peer from a group")
    remove.add_argument("name")
    remove.add_argument("peer")
    remove.add_argument("--json", action="store_true")
    add_common_args(remove)

    grants = sub.add_parser("grants", help="list group grants")
    grants.add_argument("name")
    grants.add_argument("--json", action="store_true")
    add_common_args(grants)

    grant = sub.add_parser("grant", help="add a grant to a group")
    grant.add_argument("name")
    grant.add_argument("grant_type")
    grant.add_argument("target")
    grant.add_argument("--level", default="read", choices=["read", "execute", "admin"])
    grant.add_argument("--json", action="store_true")
    add_common_args(grant)

    revoke = sub.add_parser("revoke", help="remove a grant from a group")
    revoke.add_argument("name")
    revoke.add_argument("grant_type")
    revoke.add_argument("target")
    revoke.add_argument("--json", action="store_true")
    add_common_args(revoke)

    check = sub.add_parser("check", help="check whether a peer is authorized")
    check.add_argument("peer")
    check.add_argument("grant_type")
    check.add_argument("target")
    check.add_argument("--json", action="store_true")
    add_common_args(check)

    return p


def _build_rights_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph rights", description="Effective RBAC grants for a peer.")
    p.add_argument("peer", nargs="?", default=None, help="peer name (default: resolved self)")
    p.add_argument("--json", action="store_true")
    add_common_args(p)
    return p


# ── shared output/error rendering (mirrors board.py's `_out`) ───────────────

def _out(status: int, data, ok_render, as_json: bool, *, prefix: str = "group") -> int:
    if status and 200 <= status < 300:
        print(json.dumps(data, indent=2) if as_json else ok_render(data))
        return 0
    detail = data.get("detail", data) if isinstance(data, dict) else data
    print(f"swarph {prefix}: gateway {status or 'unreachable'}: {detail}", file=sys.stderr)
    return 1


# ── dispatch ──────────────────────────────────────────────────────────────

def run_group(argv: list[str]) -> int:
    args = _build_group_parser().parse_args(argv)
    try:
        self_name = resolve_self_name(args.self_name)
        token = resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph group: {exc}", file=sys.stderr)
        return 1
    gw = args.gateway.rstrip("/")
    aj = getattr(args, "json", False)

    if args.command == "create":
        payload = _group_create_payload(args.name, args.description, args.kind)
        st, d = post_json(f"{gw}/groups", payload, token)
        return _out(st, d, lambda x: f"created group {x.get('name')} ({x.get('kind')})", aj)

    if args.command == "list":
        st, d = get_json(f"{gw}/groups", token)
        return _out(st, d, _format_groups, aj)

    if args.command == "delete":
        st, d = delete_json(_with_actor(_group_url(gw, args.name), self_name), token)
        return _out(st, d, lambda _x: f"deleted group {args.name}", aj)

    if args.command == "members":
        st, d = get_json(_members_url(gw, args.name), token)
        return _out(st, d, _format_members, aj)

    if args.command == "add":
        st, d = post_json(_members_url(gw, args.name), _member_add_payload(args.peer), token)
        return _out(st, d, lambda _x: f"added {args.peer} to {args.name}", aj)

    if args.command == "remove":
        st, d = delete_json(_with_actor(_member_url(gw, args.name, args.peer), self_name), token)
        return _out(st, d, lambda _x: f"removed {args.peer} from {args.name}", aj)

    if args.command == "grants":
        st, d = get_json(_grants_url(gw, args.name), token)
        return _out(st, d, _format_grants, aj)

    if args.command == "grant":
        payload = _grant_add_payload(args.grant_type, args.target, args.level)
        st, d = post_json(_grants_url(gw, args.name), payload, token)
        return _out(
            st, d,
            lambda _x: f"granted {args.grant_type} {args.target} ({args.level}) to {args.name}",
            aj,
        )

    if args.command == "revoke":
        payload = _revoke_payload(args.grant_type, args.target)
        st, d = delete_json(_revoke_url(gw, args.name, args.grant_type, args.target, self_name), token)
        return _out(
            st, d,
            lambda _x: f"revoked {args.grant_type} {args.target} from {args.name}",
            aj,
        )

    if args.command == "check":
        payload = _check_payload(args.peer, args.grant_type, args.target)
        st, d = post_json(f"{gw}/authz/check", payload, token)
        return _out(st, d, _format_check, aj)

    print("swarph group: unknown subcommand", file=sys.stderr)
    return 1


def run_rights(argv: list[str]) -> int:
    args = _build_rights_parser().parse_args(argv)
    try:
        self_name = resolve_self_name(args.self_name)
        token = resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph rights: {exc}", file=sys.stderr)
        return 1
    gw = args.gateway.rstrip("/")
    aj = getattr(args, "json", False)
    peer = args.peer or self_name
    st, d = get_json(_peer_grants_url(gw, peer), token)
    return _out(st, d, _format_rights, aj, prefix="rights")
