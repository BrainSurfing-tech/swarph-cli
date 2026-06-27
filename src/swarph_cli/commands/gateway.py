"""``swarph gateway`` — run the bundled mesh-gateway server.

The mesh-gateway is the coordination/DM server behind the swarph mesh:
peer registry, DM inbox/outbox, feature aggregation + allowlist/caps,
and lane/service control. It used to live as a standalone deployment;
``swarph gateway serve`` bundles it as a first-class verb so any host
can stand up a gateway the same way it runs the client verbs.

The FastAPI/uvicorn server stack is an OPTIONAL extra so the core
client paths (one-shot / chat / mesh / brain-ask) stay dependency-light::

    pip install "swarph-cli[gateway]"

Token model (mirrors ``swarph mesh``'s ``MESH_GATEWAY_TOKEN`` bearer):

  * ``--token`` sets ``MESH_GATEWAY_TOKEN`` for the served process.
  * Otherwise an existing ``MESH_GATEWAY_TOKEN`` in the environment is used.
  * If neither is present a fresh token is minted (``secrets.token_urlsafe``)
    and printed once so the operator can hand it to the mesh cells.

Only the ``serve`` subcommand exists today; bare ``swarph gateway``
prints help.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph gateway",
        description=(
            "Run the bundled mesh-gateway server (peer registry + DM "
            "inbox/outbox + feature aggregation + lane/service control)."
        ),
    )
    sub = p.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the mesh-gateway HTTP server")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1; use a tailnet IP to expose to peers)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8788,
        help="bind port (default 8788)",
    )
    serve.add_argument(
        "--token",
        default=None,
        help="bearer token for the gateway (sets MESH_GATEWAY_TOKEN); "
        "minted + printed once if omitted and not already in the env",
    )
    serve.add_argument(
        "--db",
        default=None,
        help="path to the gateway SQLite DB (sets MESH_DB_PATH)",
    )
    return p


def _serve(args: argparse.Namespace) -> int:
    # Dependency gate — the server stack is an optional extra. Probe the
    # imports before touching uvicorn so a missing extra prints a clean
    # install hint instead of a traceback.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        sys.stderr.write(
            'swarph gateway needs the optional server deps: '
            'pip install "swarph-cli[gateway]"\n'
        )
        return 2

    if args.db:
        os.environ["MESH_DB_PATH"] = args.db

    token = args.token or os.environ.get("MESH_GATEWAY_TOKEN")
    if not token:
        token = secrets.token_urlsafe(48)
        print(
            "swarph gateway: minted a new MESH_GATEWAY_TOKEN "
            "(give this to your mesh cells):\n"
            f"  {token}",
            file=sys.stderr,
        )
    os.environ["MESH_GATEWAY_TOKEN"] = token

    uvicorn.run(
        "swarph_cli.gateway.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


def run_gateway(argv: list) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    # bare `swarph gateway` (no subcommand) — print help.
    parser.print_help()
    return 0
