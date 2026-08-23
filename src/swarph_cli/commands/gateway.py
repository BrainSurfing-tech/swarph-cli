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

``bootstrap-ratify`` (#565-B) is the ratify ladder's first rung: on a fresh
gateway zero peers are ratified, so no witness exists and ``swarph ratify``
can never complete. This verb flips ONE peer ratified via a local DB write —
an explicit, audited act by the human commander on the gateway box, not a
migration accident. It self-destroys: it refuses when any ratified peer
already exists.

Bare ``swarph gateway`` prints help.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import socket
import sqlite3
import sys
from datetime import datetime, timezone


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

    boot = sub.add_parser(
        "bootstrap-ratify",
        help="ratify the FIRST peer on a fresh gateway (human commander, "
        "local DB write, audited)",
    )
    boot.add_argument("peer", help="peer name to bootstrap-ratify")
    boot.add_argument(
        "--db",
        default=None,
        help="path to the gateway SQLite DB (default: $MESH_DB_PATH or "
        "~/.swarph/mesh.db)",
    )
    boot.add_argument(
        "--reason",
        default=None,
        help="extra audit text appended to the bootstrap reason",
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


def _bootstrap_ratify(args: argparse.Namespace) -> int:
    """#565-B: the ratify ladder's first rung, as an audited local write.

    On a fresh gateway, `swarph ratify` can never complete — the witness must
    itself be ratified, and zero peers are. The live mesh only works because
    a one-time Phase 5.5 migration grandfathered its cohort; every deployment
    since hangs off that accident. This verb makes the bootstrap explicit:

      * LOCAL DB write — operator-with-filesystem-access is already god; this
        makes the act explicit and audited instead of a SQL escape hatch.
      * HUMAN-only — an interactive tty and a typed 'yes'. One prompt, no
        ceremony: the gate must not cut into the onboarding flow it serves.
      * SELF-DESTROYING — refuses when ANY ratified peer exists, so it can
        never become a second ratification path. Recovery stays possible:
        whenever zero ratified peers exist, the gate passes again.
      * HAND-IN-HAND with onboarding — the target must already be registered
        (onboard first), and the output names the next step.
    """
    db_path = (
        args.db
        or os.environ.get("MESH_DB_PATH")
        or os.path.expanduser("~/.swarph/mesh.db")
    )
    if not os.path.exists(db_path):
        sys.stderr.write(
            f"swarph gateway bootstrap-ratify: no gateway DB at {db_path}\n"
            "  Run this on the gateway box, or pass --db / set MESH_DB_PATH.\n"
        )
        return 2

    # Match server.py _conn(): autocommit with explicit transaction control
    # (we BEGIN IMMEDIATE ourselves) and FK enforcement ON — the audit insert
    # into peer_ratifications references claude_peers, and without the pragma
    # a dangling reference would write silently.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT ratified FROM claude_peers WHERE name = ?", (args.peer,)
        ).fetchone()
        if row is None:
            sys.stderr.write(
                f"swarph gateway bootstrap-ratify: {args.peer!r} is not "
                "registered —\n  onboard it first:\n"
                f"      swarph onboard {args.peer}\n"
            )
            return 2
        if row["ratified"]:
            print(f"{args.peer!r} is already ratified — nothing to do.")
            return 0
        existing = conn.execute(
            "SELECT name FROM claude_peers WHERE ratified = 1 LIMIT 1"
        ).fetchone()
        if existing:
            sys.stderr.write(
                f"swarph gateway bootstrap-ratify: refusing — "
                f"{existing['name']!r} is already ratified.\n"
                "  The ladder has its first rung; bootstrap exists ONLY for a "
                "fresh gateway.\n"
                f"  Ratify {args.peer!r} through the normal witness path "
                f"(a ratified witness runs:\n"
                f"      swarph ratify {args.peer} --reason \"<short text>\")\n"
            )
            return 2

        if not sys.stdin.isatty():
            sys.stderr.write(
                "swarph gateway bootstrap-ratify: refusing — this is the "
                "human commander's act.\n"
                "  Run it interactively on the gateway box (no pipes, no "
                "scripts, no cells).\n"
            )
            return 2

        operator = f"{getpass.getuser()}@{socket.gethostname()}"
        print(f"Bootstrap-ratify {args.peer!r} on {db_path}:")
        print("  - zero ratified peers exist (fresh gateway)")
        print("  - flips ratified=1 and writes an audited peer_ratifications "
              "row (binding_regime='bootstrap')")
        print(f"  - performed by {operator}")
        if input("Type 'yes' to confirm: ").strip().lower() != "yes":
            print("aborted — nothing written.")
            return 1

        now = datetime.now(timezone.utc).isoformat()
        reason = f"bootstrap by {operator} (#565)"
        if args.reason:
            reason += f": {args.reason}"
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT ratified FROM claude_peers WHERE name = ?", (args.peer,)
            ).fetchone()
            existing = conn.execute(
                "SELECT name FROM claude_peers WHERE ratified = 1 LIMIT 1"
            ).fetchone()
            if row is None or row["ratified"] or existing:
                conn.execute("ROLLBACK")
                sys.stderr.write(
                    "swarph gateway bootstrap-ratify: gateway state changed while "
                    "you confirmed; refusing to create a second first rung.\n"
                )
                return 2
            conn.execute(
                "UPDATE claude_peers SET ratified=1, ratified_at=?, "
                "ratified_by=?, ratification_reason=? WHERE name=?",
                (now, "bootstrap", reason, args.peer),
            )
            conn.execute(
                "INSERT INTO peer_ratifications "
                "(peer, ratified_by, ratified_at, reason, witness_dm_id, "
                "binding_regime) VALUES (?, ?, ?, ?, NULL, 'bootstrap')",
                (args.peer, "bootstrap", now, reason),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    print(f"{args.peer!r} ratified (bootstrap). The ladder has its first "
          "rung — hand in hand with onboarding:")
    print(f"  - {args.peer} can now WITNESS ratifications for the cells "
          "that follow")
    print(f"  - next cell: `swarph onboard <cell>` on its box, complete the "
          f"handshake, then a witness runs:")
    print(f"      swarph ratify <cell> --reason \"<short text>\"")
    return 0


def run_gateway(argv: list) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "bootstrap-ratify":
        return _bootstrap_ratify(args)

    # bare `swarph gateway` (no subcommand) — print help.
    parser.print_help()
    return 0
