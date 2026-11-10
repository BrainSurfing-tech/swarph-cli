"""The local credential boundary; neither caller nor argv receives a token."""
import argparse
import getpass
import json
import sys


def run_proxy(argv):
    parser = argparse.ArgumentParser(prog="swarph proxy", allow_abbrev=False)
    sub = parser.add_subparsers(dest="action", required=True)
    server = sub.add_parser("serve", allow_abbrev=False)
    server.add_argument("--cell", required=True)
    server.add_argument("--config", help="proxy-owned JSON configuration file")
    client = sub.add_parser("call", allow_abbrev=False)
    client.add_argument("verb")
    client.add_argument("--args-json", default="{}")
    client.add_argument("--socket", default=f"/run/swarph-proxy/{getpass.getuser().removeprefix('cell-')}.sock")
    args = parser.parse_args(argv)
    if sys.platform not in {"linux", "darwin"}:
        print('{"ok":false,"error":"proxy_unavailable","detail":"The local proxy is unavailable."}')
        return 1
    from swarph_cli import local_proxy as proxy
    try:
        if args.action == "serve":
            # Hardening precedes configuration and credential reads.
            proxy.harden_process()
            proxy.serve(proxy.load_config(args.cell, args.config))
            return 0
        try:
            arguments = json.loads(args.args_json)
            if not isinstance(arguments, dict):
                raise ValueError
        except ValueError:
            result = proxy.failure(None, "bad_args")
        else:
            result = proxy.call(args.socket, args.verb, arguments)
    except Exception:
        result = proxy.failure(None, "proxy_unavailable")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(run_proxy(sys.argv[1:]))
