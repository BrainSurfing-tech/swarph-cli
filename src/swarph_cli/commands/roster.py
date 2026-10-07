"""``swarph roster`` — read/write the gateway's operator-maintained capacity table.

card #1061 (ROSTER AS DATA): the roster replaces tribal knowledge about who
builds, who reviews, on which model/effort, under which quota, and who is out
until when. Reads hit GET /roster (any authenticated caller); writes hit
PUT /roster/{peer} (operators only — the gateway 403s the rest).

  swarph roster [--peer PEER] [--json]          list the table (or one row)
  swarph roster set PEER [--model M] [--effort E] [--roles r1,r2]
      [--quota-pool P] [--quota-used PCT --quota-as-of TS] [--resets DATE]
      [--until TS | --clear-until] [--note TEXT]

A quota VALUE without its as_of coordinate is a rumour: --quota-used /
--resets without --quota-as-of are refused client-side (exit 2) before the
gateway's own 400. The pool NAME needs no coordinate (identifier, not a
measurement). --clear-until is the grok-returns flow (explicit null clears).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# card #1061 role vocabulary — mirrors the gateway's _ROSTER_ROLES. A role
# the gateway does not know is refused here (exit 2) before its 400; when the
# vocabulary grows, BOTH pins change together (gateway tests + test_1061 below).
ROSTER_ROLES = ("build", "validate", "plan-review", "orchestrate")


def _http_get_json(url, token, *, timeout=10.0):
    from swarph_cli.commands.mesh import _http_get_json as _get
    return _get(url, token, timeout=timeout)


def _http_put_json(url, body, token, *, timeout=10.0):
    from swarph_cli.commands.mesh import _require_absolute_gateway_url
    _require_absolute_gateway_url(url)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def _resolve(args):
    from swarph_cli.tokens import resolve_token
    resolution = resolve_token(args.self_name, args.token_file,
                               identity_is_explicit=bool(args.self_name))
    if resolution is None:
        print("swarph roster: no token (set MESH_GATEWAY_TOKEN, pass "
              "--token-file, or --as a peer with a stored token)",
              file=sys.stderr)
        return None
    return resolution.token


def _quota_compact(r):
    if r.get("quota_used_pct") is None and not r.get("quota_pool"):
        return "-"
    parts = []
    if r.get("quota_pool"):
        parts.append(str(r["quota_pool"]))
    if r.get("quota_used_pct") is not None:
        parts.append(f"{r['quota_used_pct']}%")
    if r.get("quota_as_of"):
        parts.append(f"@{r['quota_as_of']}")
    if r.get("quota_resets_at"):
        parts.append(f"(resets {r['quota_resets_at']})")
    return " ".join(parts)


def _line(r):
    roles = ",".join(r["roles"]) if r.get("roles") else "-"
    bits = [r["peer"],
            f"model={r.get('model') or '-'}",
            f"effort={r.get('effort') or '-'}",
            f"roles={roles}",
            f"quota={_quota_compact(r)}"]
    if r.get("unavailable_until"):
        bits.append(f"out-until={r['unavailable_until']}")
    bits.append(f"updated_by={r.get('updated_by') or '?'}")
    return "  ".join(bits)


def _run_list(args):
    token = _resolve(args)
    if token is None:
        return 2
    gateway = args.gateway.rstrip("/")
    status, payload = _http_get_json(f"{gateway}/roster", token)
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>") if isinstance(payload, dict) else "<gateway error>"
        print(f"swarph roster: gateway {status}: {detail}", file=sys.stderr)
        return 1
    rows = payload.get("roster") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        print("swarph roster: unexpected /roster shape (no roster list)",
              file=sys.stderr)
        return 1
    if args.peer:
        rows = [r for r in rows if isinstance(r, dict) and r.get("peer") == args.peer]
        if not rows:
            print(f"swarph roster: no row for {args.peer!r}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(_line(r) if isinstance(r, dict) else r)
    return 0


def _run_set(args):
    token = _resolve(args)
    if token is None:
        return 2
    if ((args.quota_used is not None or args.resets is not None)
            and args.quota_as_of is None):
        print("swarph roster set: --quota-used / --resets need --quota-as-of "
              "(a quota value without its coordinate is a rumour)",
              file=sys.stderr)
        return 2
    body = {}
    if args.model is not None:
        body["model"] = args.model
    if args.effort is not None:
        body["effort"] = args.effort
    if args.roles is not None:
        roles = [x.strip() for x in args.roles.split(",") if x.strip()]
        bad = [x for x in roles if x not in ROSTER_ROLES]
        if bad:
            print(f"swarph roster set: unknown role(s) {bad} "
                  f"(vocabulary: {', '.join(ROSTER_ROLES)})", file=sys.stderr)
            return 2
        body["roles"] = roles
    if args.quota_pool is not None:
        body["quota_pool"] = args.quota_pool
    if args.quota_used is not None:
        body["quota_used_pct"] = args.quota_used
    if args.resets is not None:
        body["quota_resets_at"] = args.resets
    if args.quota_as_of is not None:
        body["quota_as_of"] = args.quota_as_of
    if args.until is not None:
        body["unavailable_until"] = args.until
    if args.clear_until:
        body["unavailable_until"] = None
    if args.note is not None:
        body["note"] = args.note
    if not body:
        print("swarph roster set: nothing to set (pass a field flag)",
              file=sys.stderr)
        return 2
    gateway = args.gateway.rstrip("/")
    status, payload = _http_put_json(
        f"{gateway}/roster/{args.peer}", body, token)
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>") if isinstance(payload, dict) else "<gateway error>"
        print(f"swarph roster set: gateway {status}: {detail}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"updated {args.peer} (updated_by={payload.get('updated_by', '?')})")
    return 0


def _add_global(p):
    p.add_argument("--as", dest="self_name", default=None,
                   help="peer identity for token resolution")
    from swarph_cli.gateway_default import env_gateway
    p.add_argument("--gateway", default=env_gateway(),
                   help="mesh-gateway base URL")
    p.add_argument("--token-file", default=None,
                   help="explicit Bearer [REDACTED] file")


def _build_parser():
    p = argparse.ArgumentParser(
        prog="swarph roster",
        description="Read/write the gateway capacity roster (card #1061).")
    _add_global(p)
    p.add_argument("--peer", default=None,
                   help="show only this peer (list mode)")
    p.add_argument("--json", action="store_true",
                   help="print the raw gateway payload")
    sub = p.add_subparsers(dest="command")
    s = sub.add_parser("set", help="upsert one roster row (operators only)")
    _add_global(s)
    s.add_argument("peer", help="peer the row belongs to")
    s.add_argument("--json", action="store_true",
                   help="print the raw gateway payload")
    s.add_argument("--model", default=None)
    s.add_argument("--effort", default=None)
    s.add_argument("--roles", default=None,
                   help="comma-separated subset of " + ",".join(ROSTER_ROLES))
    s.add_argument("--quota-pool", default=None)
    s.add_argument("--quota-used", type=float, default=None,
                   help="quota_used_pct (needs --quota-as-of)")
    s.add_argument("--quota-as-of", default=None)
    s.add_argument("--resets", default=None,
                   help="quota_resets_at date (needs --quota-as-of)")
    group = s.add_mutually_exclusive_group()
    group.add_argument("--until", default=None,
                       help="unavailable_until timestamp")
    group.add_argument("--clear-until", action="store_true",
                       help="clear unavailable_until (peer is back)")
    s.add_argument("--note", default=None)
    return p


def run_roster(argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            return _run_set(args)
        if args.command is None:
            return _run_list(args)
        parser.error(f"unknown command: {args.command}")
    except RuntimeError as exc:
        print(f"swarph roster: {exc}", file=sys.stderr)
        return 2
    return 2
