"""Bounded platform action for draining receipt-gated peer replies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from swarph_cli.commands._gateway_client import resolve_self_name, resolve_token
from swarph_cli.peer_executor import PeerSpool
from swarph_cli.peer_service_reply import MeshGatewayReplyTransport, ReceiptGatedReplyOutbox


def run_peer_reply_drain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="swarph peer-reply-drain",
        description="Deliver only receipt-validated pending peer-service replies.",
    )
    parser.add_argument("--spool-dir", required=True, type=Path)
    parser.add_argument("--outbox-dir", required=True, type=Path)
    parser.add_argument("--as", dest="self_name", default=None)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args(argv)

    self_name = resolve_self_name(args.self_name)
    token = resolve_token(self_name, args.token_file)
    results = ReceiptGatedReplyOutbox(args.outbox_dir).drain(
        PeerSpool(args.spool_dir),
        MeshGatewayReplyTransport(args.gateway, token, self_name),
    )
    print(json.dumps([result.__dict__ for result in results], sort_keys=True))
    return 0 if all(result.state in {"sent", "already-sent", "already-drained"} for result in results) else 1
