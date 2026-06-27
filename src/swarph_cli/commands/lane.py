"""``swarph lane`` — client for the gateway's $0-lane orchestration.

A lane is a pool of provider-backed workers the gateway scales on demand;
``list``/``create``/``scale``/``delete``/``enqueue`` drive that control plane.
Create/scale/delete are operator-gated server-side, so a 403 surfaces through
the non-2xx path here like any other gateway error.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from ._gateway_client import (
    add_common_args,
    delete_json,
    get_json,
    post_json,
    resolve_self_name,
    resolve_token,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph lane",
        description="Drive the gateway's $0-lane orchestration.",
    )
    sub = p.add_subparsers(dest="command")

    ls = sub.add_parser("list", help="list lanes")
    add_common_args(ls)

    create = sub.add_parser("create", help="create a lane (operator-gated)")
    create.add_argument("name", help="lane name")
    create.add_argument("--provider", required=True, help="lane provider")
    create.add_argument("--model", required=True, help="provider model")
    create.add_argument("--n", type=int, default=0, help="initial worker count")
    create.add_argument("--context-text", default=None, help="inline context text")
    create.add_argument(
        "--context-file",
        action="append",
        default=[],
        help="context file path (repeatable)",
    )
    add_common_args(create)

    scale = sub.add_parser("scale", help="scale a lane (operator-gated)")
    scale.add_argument("name", help="lane name")
    scale.add_argument("--n", type=int, required=True, help="target worker count")
    add_common_args(scale)

    delete = sub.add_parser("delete", help="delete a lane (operator-gated)")
    delete.add_argument("name", help="lane name")
    add_common_args(delete)

    enqueue = sub.add_parser("enqueue", help="enqueue a job on a lane")
    enqueue.add_argument("name", help="lane name")
    enqueue.add_argument("--prompt", required=True, help="job prompt")
    enqueue.add_argument("--context-text", default=None, help="inline context text")
    enqueue.add_argument(
        "--context-file",
        action="append",
        default=[],
        help="context file path (repeatable)",
    )
    add_common_args(enqueue)

    return p


def _fail(sub: str, status: int, payload: dict) -> int:
    detail = payload.get("detail", "<error>")
    print(f"swarph lane {sub}: gateway {status}: {detail}", file=sys.stderr)
    return 1


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _auth(args: argparse.Namespace) -> tuple[str, str]:
    self_name = resolve_self_name(args.self_name)
    token = resolve_token(self_name, args.token_file)
    return self_name, token


def _run_list(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    status, payload = get_json(f"{base}/lanes", token)
    if not _ok(status):
        return _fail("list", status, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _run_create(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    body = {
        "name": args.name,
        "provider": args.provider,
        "model": args.model,
        "n": args.n,
        "context_text": args.context_text or "",
        "context_files": args.context_file,
    }
    status, payload = post_json(f"{base}/lanes", body, token)
    if not _ok(status):
        return _fail("create", status, payload)
    print(f"created lane {args.name}")
    return 0


def _run_scale(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    name = urllib.parse.quote(args.name, safe="")
    status, payload = post_json(f"{base}/lanes/{name}/scale", {"n": args.n}, token)
    if not _ok(status):
        return _fail("scale", status, payload)
    print(f"scaled {args.name} -> {args.n}")
    return 0


def _run_delete(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    name = urllib.parse.quote(args.name, safe="")
    status, payload = delete_json(f"{base}/lanes/{name}", token)
    if not _ok(status):
        return _fail("delete", status, payload)
    print(f"deleted lane {args.name}")
    return 0


def _run_enqueue(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    name = urllib.parse.quote(args.name, safe="")
    body = {
        "prompt": args.prompt,
        "context_text": args.context_text or "",
        "context_files": args.context_file,
    }
    status, payload = post_json(f"{base}/lanes/{name}/enqueue", body, token)
    if not _ok(status):
        return _fail("enqueue", status, payload)
    print(f"enqueued on {args.name}: {json.dumps(payload)}")
    return 0


_DISPATCH = {
    "list": _run_list,
    "create": _run_create,
    "scale": _run_scale,
    "delete": _run_delete,
    "enqueue": _run_enqueue,
}


def run_lane(argv: list) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return _DISPATCH[args.command](args)
    except RuntimeError as exc:
        print(f"swarph lane: {exc}", file=sys.stderr)
        return 2
