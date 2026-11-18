"""``swarph commander`` — propose a decision or an action, and read a standing go.

Card #1297: the cell asks. The commander decides, from the desktop, with a
credential this command never holds. ``ask`` posts a decision. ``request``
posts ``merge_pr`` or ``release_finish``.

Card #1300: ``authorization check`` reads the standing record. It exits 0
only when that scope is active, and it prints the id, the words, and
granted_at. A revoked or expired grant exits 1 and does not print the words.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from swarph_cli.commands import mesh as mesh_cmd
from swarph_cli.redact import redact_text


def ask_body(question: str, options: list[str], card: int | None) -> dict:
    body: dict = {"type": "decision", "question": question, "options": options}
    if card is not None:
        body["card_id"] = card
    return body


def request_body(kind: str, *, repo: str | None, pr: int | None,
                 head: str | None, version: str | None, card: int | None) -> dict:
    body: dict = {"type": kind, "pr": pr, "head_sha": head}
    if kind == "merge_pr":
        body["repo"] = repo
    if kind == "release_finish":
        body["version"] = version
    if card is not None:
        body["card_id"] = card
    return body


def post_request(gateway: str, token: str, body: dict) -> tuple[int, dict]:
    """The one POST that proposes to the commander queue, shared by the CLI and MCP."""
    return mesh_cmd._post_json(f"{gateway.rstrip('/')}/commander/requests", body, token)


def confirmed_id(payload) -> int | None:
    """The queue id of an accepted proposal, or None when a 2xx reply carries no
    integer id (200 with an empty object, a list, a null or string id). That is
    not a confirmation. bool is an int subclass, so it is refused by type."""
    rid = payload.get("id") if isinstance(payload, dict) else None
    return rid if type(rid) is int else None


def unconfirmed(status: int) -> str:
    return f"gateway {status} returned no request id; the proposal is not confirmed"


# The gateway's request statuses (mesh-gateway 4989daa, server.py): pending 4040,
# answered 4133, approved 4135, rejected 4137, executed/failed/stale 4165,
# withdrawn 4212. All are short lowercase words, so one pattern admits them and
# a future status; anything else in a reply is never echoed.
_STATUS_WORD = re.compile(r"[a-z][a-z_]{0,23}")


def safe_status(value) -> str:
    """A reply's status as it may be shown: a short lowercase word, else "unknown"."""
    return value if isinstance(value, str) and _STATUS_WORD.fullmatch(value) else "unknown"


def safe_text(text: str, token: str) -> str:
    """Gateway-supplied text as it may be shown: the caller's own bearer removed
    exactly (redact_text alone misses about 1 in 5 bare tokens), then the
    secret shapes redact_text knows. Ordinary text passes through unchanged."""
    if len(token) >= 16:  # a real credential; a short test token would mangle words
        text = text.replace(token, "[REDACTED]")
    return redact_text(text, caller="commander") or ""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph commander")
    sub = p.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="propose a decision")
    mesh_cmd._add_common(ask)
    ask.add_argument("--question", required=True)
    ask.add_argument("--options", default=None,
                     help="comma-separated choices, e.g. yes,no")
    ask.add_argument("--card", type=int, default=None)

    req = sub.add_parser("request", help="propose merge_pr or release_finish")
    mesh_cmd._add_common(req)
    req.add_argument("type", choices=["merge_pr", "release_finish"])
    req.add_argument("--repo", default=None)
    req.add_argument("--pr", type=int, default=None)
    req.add_argument("--head", default=None)
    req.add_argument("--version", default=None)
    req.add_argument("--card", type=int, default=None)

    auth = sub.add_parser("authorization", help="a standing authorization")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    check = auth_sub.add_parser(
        "check", help="exit 0 when an active standing authorization exists")
    check.add_argument("scope")
    mesh_cmd._add_common(check)
    return p


def _options(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def fetch_scope(scope: str, token: str, gateway: str) -> tuple[int, dict]:
    query = urllib.parse.urlencode({"scope": scope})
    url = f"{gateway.rstrip('/')}/commander/authorizations?{query}"
    mesh_cmd._require_absolute_gateway_url(url)
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {"detail": str(exc)}
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def render_check(rows: list) -> tuple[int, str, str]:
    """Exit code, stdout, stderr. Active prints id, text and granted_at."""
    active = [row for row in rows if row.get("status") == "active"]
    if active:
        row = max(active, key=lambda item: item.get("id") or 0)
        stdout = (
            f"id={row.get('id')}\n"
            f"text={row.get('text') or ''}\n"
            f"granted_at={row.get('granted_at') or ''}\n"
        )
        return 0, stdout, ""
    if any(row.get("status") == "revoked" for row in rows):
        return 1, "", "revoked\n"
    if any(row.get("status") == "expired" for row in rows):
        return 1, "", "expired\n"
    return 1, "", "none\n"


def _check(args) -> int:
    try:
        self_name = mesh_cmd._resolve_self_name(args.self_name)
        token = mesh_cmd._resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph commander: {exc}", file=sys.stderr)
        return 1
    gateway = (args.gateway or "").rstrip("/")
    if not gateway:
        print("swarph commander: MESH_GATEWAY_URL is not set", file=sys.stderr)
        return 2
    status, body = fetch_scope(args.scope, token, gateway)
    if status != 200 or not isinstance(body, dict):
        print("swarph commander: authorization check failed", file=sys.stderr)
        return 1
    code, stdout, stderr = render_check(body.get("authorizations") or [])
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return code


def run_commander(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "authorization":
        return _check(args)
    try:
        self_name = mesh_cmd._resolve_self_name(args.self_name)
        token = mesh_cmd._resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph commander: {exc}", file=sys.stderr)
        return 1
    gw = args.gateway.rstrip("/")
    if args.command == "ask":
        body = ask_body(args.question, _options(args.options), args.card)
    else:
        if args.pr is None or not args.head:
            print("swarph commander request: --pr and --head are required",
                  file=sys.stderr)
            return 2
        if args.type == "merge_pr" and not args.repo:
            print("swarph commander request: merge_pr requires --repo",
                  file=sys.stderr)
            return 2
        if args.type == "release_finish" and not args.version:
            print("swarph commander request: release_finish requires --version",
                  file=sys.stderr)
            return 2
        body = request_body(args.type, repo=args.repo, pr=args.pr, head=args.head,
                            version=args.version, card=args.card)
    status, payload = post_request(gw, token, body)
    if status in (200, 201):
        if confirmed_id(payload) is None:
            print(f"swarph commander: {unconfirmed(status)}", file=sys.stderr)
            return 1
        print(f"commander request #{payload.get('id')} {safe_status(payload.get('status'))}")
        return 0
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    print(safe_text(f"swarph commander: {detail}", token), file=sys.stderr)
    return 1
