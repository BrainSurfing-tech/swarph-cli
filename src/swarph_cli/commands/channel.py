"""``swarph channel`` — mesh CHANNELS control-plane client (create / list /
join / leave / members) over the shared gateway HTTP layer."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from ._gateway_client import (
    add_common_args,
    get_json,
    post_json,
    resolve_self_name,
    resolve_token,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph channel",
        description="Mesh CHANNELS control-plane commands.",
    )
    sub = p.add_subparsers(dest="command")

    create = sub.add_parser("create", help="create a channel")
    create.add_argument("name", help="channel name")
    create.add_argument(
        "--kind", required=True, choices=["announce", "topic", "group"]
    )
    create.add_argument("--visibility", default=None, choices=["open", "invite"])
    create.add_argument("--description", default=None)
    add_common_args(create)

    listp = sub.add_parser("list", help="list channels")
    add_common_args(listp)

    join = sub.add_parser("join", help="join a channel")
    join.add_argument("name", help="channel name")
    join.add_argument(
        "--wake-policy",
        default=None,
        choices=["mentions_only", "here_and_mentions", "all", "muted"],
    )
    add_common_args(join)

    leave = sub.add_parser("leave", help="leave a channel")
    leave.add_argument("name", help="channel name")
    add_common_args(leave)

    members = sub.add_parser("members", help="list channel members")
    members.add_argument("name", help="channel name")
    add_common_args(members)

    post = sub.add_parser("post", help="post a message to a channel")
    post.add_argument("name", help="channel name")
    post.add_argument("--kind", default="fyi", help="message kind (default fyi)")
    post.add_argument("--content", required=True, help="message body")
    add_common_args(post)

    read = sub.add_parser("read", help="read recent messages in a channel")
    read.add_argument("name", help="channel name")
    read.add_argument("--limit", type=int, default=20, help="max messages")
    read.add_argument("--json", action="store_true", help="print raw JSON")
    add_common_args(read)

    return p


# ── pure helpers (unit-tested) ────────────────────────────────────────────────

def _post_payload(self_name: str, channel: str, kind: str, content: str) -> dict:
    """A channel post carries ``channel`` and NEVER ``to_node`` — the gateway
    rejects a message with both ("exactly one of {to_node, channel} required")."""
    return {"from_node": self_name, "channel": channel, "kind": kind, "content": content}


def _read_url(base: str, channel: str, limit: int) -> str:
    return f"{base.rstrip('/')}/messages?channel={urllib.parse.quote(channel, safe='')}&limit={int(limit)}"


def _format_channel_messages(payload) -> str:
    msgs = payload.get("messages", []) if isinstance(payload, dict) else (payload or [])
    if not msgs:
        return "(no messages)"
    lines = []
    for m in msgs:
        body = (m.get("content") or "").replace("\n", " ")
        if len(body) > 120:
            body = body[:117] + "..."
        lines.append(f"[{m.get('id')}] {m.get('from_node')} ({m.get('kind')}): {body}")
    return "\n".join(lines)


def _ctx(args: argparse.Namespace) -> tuple[str, str, str]:
    self_name = resolve_self_name(args.self_name)
    token = resolve_token(self_name, args.token_file)
    return self_name, token, args.gateway.rstrip("/")


def _fail(sub: str, status: int, payload: dict) -> int:
    detail = payload.get("detail", "<error>")
    print(f"swarph channel {sub}: gateway {status}: {detail}", file=sys.stderr)
    return 1


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _run_create(args: argparse.Namespace) -> int:
    self_name, token, base = _ctx(args)
    body = {"name": args.name, "kind": args.kind, "created_by": self_name}
    if args.visibility is not None:
        body["visibility"] = args.visibility
    if args.description is not None:
        body["description"] = args.description
    status, payload = post_json(f"{base}/channels", body, token)
    if not _ok(status):
        return _fail("create", status, payload)
    print(f"created channel {args.name}")
    return 0


def _run_list(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    status, payload = get_json(f"{base}/channels", token)
    if not _ok(status):
        return _fail("list", status, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _run_join(args: argparse.Namespace) -> int:
    self_name, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    body = {"peer": self_name}
    if args.wake_policy is not None:
        body["wake_policy"] = args.wake_policy
    status, payload = post_json(f"{base}/channels/{name}/join", body, token)
    if not _ok(status):
        return _fail("join", status, payload)
    print(f"joined {args.name}")
    return 0


def _run_leave(args: argparse.Namespace) -> int:
    self_name, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    status, payload = post_json(
        f"{base}/channels/{name}/leave", {"peer": self_name}, token
    )
    if not _ok(status):
        return _fail("leave", status, payload)
    print(f"left {args.name}")
    return 0


def _run_members(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    status, payload = get_json(f"{base}/channels/{name}/members", token)
    if not _ok(status):
        return _fail("members", status, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _run_post(args: argparse.Namespace) -> int:
    self_name, token, base = _ctx(args)
    status, payload = post_json(
        f"{base}/messages", _post_payload(self_name, args.name, args.kind, args.content), token
    )
    if not _ok(status):
        return _fail("post", status, payload)
    print(f"posted to #{args.name} (msg {payload.get('id')})")
    return 0


def _run_read(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    status, payload = get_json(_read_url(base, args.name, args.limit), token)
    if not _ok(status):
        return _fail("read", status, payload)
    print(json.dumps(payload, indent=2) if args.json else _format_channel_messages(payload))
    return 0


def run_channel(argv: list) -> int:
    p = _build_parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        if args.command == "create":
            return _run_create(args)
        if args.command == "list":
            return _run_list(args)
        if args.command == "join":
            return _run_join(args)
        if args.command == "leave":
            return _run_leave(args)
        if args.command == "members":
            return _run_members(args)
        if args.command == "post":
            return _run_post(args)
        if args.command == "read":
            return _run_read(args)
    except RuntimeError as e:
        print(f"swarph channel: {e}", file=sys.stderr)
        return 2
    p.print_help()
    return 0
