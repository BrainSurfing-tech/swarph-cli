"""``swarph event`` — emit an event to a mesh channel (event-chaining P0).

``swarph event emit <event> <payload> [--chain-token <opaque>] [--channel events]``
posts to a channel via the shipped channel-post client, carrying an ``event`` tag,
the payload, and any ``--chain-token`` as an OPAQUE passthrough. The cell never
signs or parses the chain-token — signing/verifying is the producer's authority
(the P1 gateway guard). See ``swarph_cli.chain_token`` for the producer-side
sign/verify helpers.
"""

from __future__ import annotations

import argparse
import json
import sys

from ._gateway_client import (
    add_common_args,
    post_json,
    resolve_self_name,
    resolve_token,
)
from swarph_cli.redact import encode_outbound_json, redact_text


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph event",
        description="Emit an event to a mesh channel (opaque chain-token passthrough).",
    )
    sub = p.add_subparsers(dest="command")

    emit = sub.add_parser("emit", help="emit an event to a channel")
    emit.add_argument("event", help="event name/tag")
    emit.add_argument("payload", help="event payload (opaque string)")
    emit.add_argument(
        "--chain-token",
        default=None,
        help="opaque producer-signed chain token — passed through, never parsed",
    )
    emit.add_argument(
        "--channel",
        default="events",
        help="target channel (default: events)",
    )
    add_common_args(emit)
    return p


class ChainTokenAttachError(RuntimeError):
    """The redacted event bytes are not JSON, so the chain token is not sent."""


def _attach_chain_token(data: bytes, chain_token: str) -> bytes:
    """Add the CLI chain token onto an already-redacted event body.

    Same shape as ``mcp_server._search``: redact first, then set the one
    field that must reach the wire. A pasted chain_token line in the payload
    was already scrubbed and is not this value.

    Redaction can rewrite a quote inside the content string and leave bytes
    that are not JSON. That refusal is by name (``JSONDecodeError`` or
    ``UnicodeDecodeError``). The token is not copied into the error, and the
    caller sends nothing.
    """
    try:
        body_obj = json.loads(data.decode("utf-8"))
        inner = json.loads(body_obj["content"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChainTokenAttachError(
            f"_attach_chain_token refused: {type(exc).__name__}"
        ) from None
    inner["swarph_event"]["chain_token"] = chain_token
    body_obj["content"] = json.dumps(inner, separators=(",", ":"), sort_keys=True)
    return json.dumps(body_obj).encode("utf-8")


def _post_channel(
    *,
    channel: str,
    event: str,
    payload: str,
    chain_token: str | None = None,
    gateway: str | None = None,
    self_name: str | None = None,
    token_file: str | None = None,
) -> int:
    """Bridge to the shipped channel-post client (the ``/messages`` channel path
    that ``swarph channel`` / ``swarph mesh`` post through). Wraps the event tag +
    payload + (opaque) chain-token into the post body and returns 0 on a 2xx.

    NOTE: the plan's seam is ``_post_channel(**kw)``; the gateway's ``post_json``
    takes ``(url, body, token)``, so this function resolves identity/token and
    shapes the body, then delegates to that same shared client — it does NOT
    reinvent the gateway post path. Tests monkeypatch this function directly.
    """
    name = resolve_self_name(self_name)
    token = resolve_token(name, token_file)
    from swarph_cli.gateway_default import env_gateway

    # "" when unconfigured — post_json IS mesh._post_json, whose
    # _require_absolute_gateway_url then refuses by name rather than raising
    # `ValueError: unknown url type` from Request(). Checked in review of #318.
    base = (gateway or env_gateway()).rstrip("/")
    payload = redact_text(payload, caller="event.emit") or ""
    ev: dict = {"event": event, "payload": payload}
    # Sentinel-wrapped so P1's guard can UNAMBIGUOUSLY identify an event post: a
    # normal channel message won't carry the top-level `swarph_event` key, so the
    # guard keys off `content.swarph_event` — never a false-positive on a plain
    # message whose content happens to hold an "event" field. kind stays "fyi"
    # (gateway VALID_KINDS); P1 promotes this to a first-class kind/column when it
    # touches the gateway.
    content: dict = {"swarph_event": ev}
    body = {
        "from_node": name,
        "channel": channel,
        "kind": "fyi",
        "content": json.dumps(content, separators=(",", ":"), sort_keys=True),
    }
    # Redact with no chain token in the body. Attach the CLI value after, so
    # only that value skips redaction. An explicit "" is carried; omit only
    # when the flag was unset. Card #1472.
    if chain_token is None:
        status, resp = post_json(f"{base}/messages", body, token)
    else:
        # Attach after redaction. If those bytes are not JSON, name the
        # refusal and return before post_json: nothing is sent, no traceback.
        try:
            encoded = _attach_chain_token(
                encode_outbound_json(body, caller="event.emit"),
                chain_token,
            )
        except ChainTokenAttachError as exc:
            print(f"swarph event: {exc}", file=sys.stderr)
            return 2
        status, resp = post_json(
            f"{base}/messages",
            body,
            token,
            encoded=encoded,
        )
    if 200 <= status < 300:
        print(f"emitted event={event} channel={channel} id={resp.get('id')}")
        return 0
    detail = resp.get("detail", "<gateway error>")
    print(f"swarph event emit: gateway {status}: {detail}", file=sys.stderr)
    return 1


def run_event(argv: list) -> int:
    p = _build_parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        if args.command == "emit":
            return _post_channel(
                channel=args.channel,
                event=args.event,
                payload=args.payload,
                chain_token=args.chain_token,
                gateway=args.gateway,
                self_name=args.self_name,
                token_file=args.token_file,
            )
    except RuntimeError as e:
        print(f"swarph event: {e}", file=sys.stderr)
        return 2
    p.print_help()
    return 0
