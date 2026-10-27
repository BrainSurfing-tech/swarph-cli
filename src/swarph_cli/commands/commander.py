"""``swarph commander`` — propose a decision or an action. Card #1297.

The cell asks. The commander decides, from the desktop, with a credential
this command never holds. ``ask`` posts a decision. ``request`` posts
``merge_pr`` or ``release_finish``. An MCP tool for the same queue is a
later card.
"""
from __future__ import annotations

import argparse
import sys

from swarph_cli.commands import mesh as mesh_cmd


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
    return p


def _options(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def run_commander(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
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
    status, payload = mesh_cmd._post_json(f"{gw}/commander/requests", body, token)
    if status in (200, 201):
        print(f"commander request #{payload.get('id')} {payload.get('status')}")
        return 0
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    print(f"swarph commander: {detail}", file=sys.stderr)
    return 1
