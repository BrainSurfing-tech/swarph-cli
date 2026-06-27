"""``swarph brain`` — run the gbrain HTTP brain server (the $0 semantic memory).

``brain-ask`` *searches* a running brain; ``brain serve`` *runs* one. gbrain
(the sovereign $0 semantic-memory server) is an external binary — this verb is a
thin launcher that applies the swarph-blessed defaults: loopback bind (expose a
tailnet IP explicitly), and a 1-year token TTL (a short TTL silently 401s
long-lived mesh cells — a lesson learned the hard way). It replaces the current
process with ``gbrain serve`` via ``os.execvp`` so signals/stdio pass straight
through.

Usage:
  swarph brain serve [--port 8792] [--bind 127.0.0.1]
    [--token-ttl 31536000] [--gbrain-bin PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

_DEFAULT_PORT = 8792
_DEFAULT_BIND = "127.0.0.1"
_DEFAULT_TOKEN_TTL = 31536000  # 1 year — short TTLs silently 401 long-lived cells


def _find_gbrain(explicit) -> str | None:
    """Resolve the gbrain binary: --gbrain-bin, else GBRAIN_BIN, else PATH."""
    if explicit:
        return explicit if (os.path.isfile(explicit) or shutil.which(explicit)) else None
    env = os.environ.get("GBRAIN_BIN")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return env
    return shutil.which("gbrain")


def run_brain(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph brain",
        description="Run the gbrain HTTP brain server (the $0 semantic memory).")
    sub = p.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the gbrain HTTP brain server")
    serve.add_argument("--port", type=int, default=_DEFAULT_PORT,
                       help=f"HTTP port (default {_DEFAULT_PORT})")
    serve.add_argument("--bind", default=_DEFAULT_BIND,
                       help=f"bind address (default {_DEFAULT_BIND}; pass a tailnet "
                            "IP to expose on the mesh)")
    serve.add_argument("--token-ttl", type=int, default=_DEFAULT_TOKEN_TTL,
                       help="minted-token lifetime in seconds (default 1 year — "
                            "short TTLs silently 401 long-lived mesh cells)")
    serve.add_argument("--gbrain-bin", default=None,
                       help="path to the gbrain binary (else GBRAIN_BIN / PATH)")
    args = p.parse_args(argv)

    if args.cmd != "serve":
        p.print_help()
        return 0

    gbin = _find_gbrain(args.gbrain_bin)
    if not gbin:
        print("swarph brain serve needs the `gbrain` binary on PATH (the sovereign "
              "$0 semantic-memory server). Install it, or pass --gbrain-bin PATH / "
              "set GBRAIN_BIN. `swarph brain-ask` queries an already-running brain.",
              file=sys.stderr)
        return 2

    cmd = [gbin, "serve", "--http",
           "--port", str(args.port),
           "--bind", args.bind,
           "--token-ttl", str(args.token_ttl)]
    # Replace this process with gbrain so it owns the terminal / signals.
    os.execvp(gbin, cmd)
    return 0  # unreachable when execvp succeeds; keeps the type-checker happy
