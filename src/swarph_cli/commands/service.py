"""``swarph service`` — stand up a $0 subscription-LLM HTTP lane.

`swarph service serve --provider <claude|codex|gemini>` runs a small FastAPI app
that exposes the provider's *subscription* CLI as a $0 HTTP lane (`POST /delegate`,
`GET /health`, bearer auth). The subprocess env is billing-scrubbed so the lane
can only use the $0 subscription path, never metered API billing. The server
stack (fastapi/uvicorn) is the optional ``[service]`` extra; the verb prints an
install hint and exits 2 when it's absent.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys


def _have_server_deps() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def run_service(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph service",
        description="Stand up a $0 subscription-LLM HTTP lane.")
    sub = p.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run a $0 service lane for one provider")
    serve.add_argument("--provider", required=True,
                       help="claude | codex | gemini")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind host (default 127.0.0.1; pass a tailnet IP to expose)")
    serve.add_argument("--port", type=int, default=8799, help="HTTP port (default 8799)")
    serve.add_argument("--token", default=None,
                       help="bearer token (else SWARPH_SERVICE_TOKEN, else minted+printed)")
    serve.add_argument("--timeout", type=int, default=120,
                       help="per-call subprocess timeout in seconds (default 120)")
    args = p.parse_args(argv)

    if args.cmd != "serve":
        p.print_help()
        return 0

    from swarph_cli.service import providers as pv
    if args.provider not in pv.SUPPORTED:
        print(f"swarph service: unsupported provider {args.provider!r}; "
              f"supported: {', '.join(pv.SUPPORTED)}", file=sys.stderr)
        return 2

    if not _have_server_deps():
        print('swarph service serve needs the server extra: '
              'pip install "swarph-cli[service]"', file=sys.stderr)
        return 2

    token = args.token or os.environ.get("SWARPH_SERVICE_TOKEN")
    if not token:
        token = secrets.token_urlsafe(32)
        print("swarph service: minted bearer token (set SWARPH_SERVICE_TOKEN to "
              f"reuse it):\n  {token}", file=sys.stderr)

    from swarph_cli.service.app import build_app
    import uvicorn

    app = build_app(args.provider, token, timeout=args.timeout)
    print(f"swarph service: {args.provider} $0 lane on "
          f"http://{args.host}:{args.port}  (POST /delegate, GET /health)",
          file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
