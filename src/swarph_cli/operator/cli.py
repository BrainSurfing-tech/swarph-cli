"""``swarph-me`` — operator console entrypoint (card #660).

Identity is pinned from operator config (or ``--as`` anywhere on the line).
This module never reads the cell-environment identity variable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .api import request_json
from .config import (
    CONFIG_PATH,
    OperatorConfig,
    default_token_file_for,
    load_config,
    save_config,
)
from .fmt import format_response


HELP = """swarph-me — operator mesh handle (human identity; refuses cell-env inheritance)

  swarph-me config --identity <peer> --gateway <url> [--token-file PATH]
  swarph-me channels
  swarph-me join <channel> [wake_policy]
  swarph-me leave <channel>
  swarph-me who <channel>
  swarph-me read <channel> [n]
  swarph-me say <channel> --content-file PATH
  swarph-me unread
  swarph-me cards [ready|stage|assignee]
  swarph-me show <id>
  swarph-me thread <id> [n]
  swarph-me move <id> <stage>
  swarph-me assign <id> <peer>
  swarph-me claim <id>
  swarph-me ready <id> [clear]
  swarph-me cardsay <id> --content-file PATH
  swarph-me due <id> <YYYY-MM-DD> [HH:MM]   |  swarph-me due <id> clear
  swarph-me sched
  swarph-me schedule <name> "<cron>" <target> [k=v ...] --content-file PATH
  swarph-me --as <peer> <cmd> ...     (--as accepted anywhere on the line)
"""


def extract_as(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Consume every ``--as <peer>`` pair from any position."""
    as_name: str | None = None
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--as":
            if i + 1 >= len(argv):
                raise SystemExit("swarph-me: --as needs a peer name")
            as_name = argv[i + 1]
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return as_name, out


def _read_token(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"swarph-me: no readable token at {path}")
    text = path.read_text(encoding="utf-8").strip()
    # Accept bare token or MESH_GATEWAY_TOKEN=... env-style (same shapes cells use).
    if text.startswith("MESH_GATEWAY_TOKEN="):
        text = text.split("=", 1)[1].strip().strip("\"'")
    if not text:
        raise SystemExit(f"swarph-me: token file empty: {path}")
    return text


def _read_content_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"swarph-me: content file not found: {path}")
    body = p.read_text(encoding="utf-8")
    if not body.strip():
        raise SystemExit(f"swarph-me: content file is empty: {path}")
    return body


def resolve_session(
    as_override: str | None,
    *,
    config_path: Path | None = None,
) -> tuple[str, str, str]:
    """Return ``(identity, gateway, token)`` from operator config + optional ``--as``.

    Never consults the cell-environment identity variable. ``--as`` changes which
    peer token is used and which identity is stamped; it does not fall back to
    the cell environment.
    """
    cfg = load_config(config_path)
    if cfg is None:
        raise SystemExit(
            "swarph-me: no operator config. Run once:\n"
            "  swarph-me config --identity <peer> --gateway <url>\n"
            f"Config path: {config_path or CONFIG_PATH}"
        )
    identity = as_override or cfg.identity
    if as_override:
        # Keep gateway from config; retarget token to the named peer's file
        # under the same directory convention unless token_file was absolute
        # and identity-specific — prefer ~/.config/swarph/<as>.peer_token.
        token_file = default_token_file_for(identity)
        session = OperatorConfig(
            identity=identity, gateway=cfg.gateway, token_file=token_file
        )
    else:
        session = cfg
    token = _read_token(session.token_path())
    return session.identity, session.gateway_url(), token


def _api(
    method: str,
    gateway: str,
    token: str,
    path: str,
    body: dict | None = None,
):
    return request_json(method, f"{gateway}{path}", token, body)


def cmd_config(args: argparse.Namespace) -> int:
    cfg = save_config(
        identity=args.identity,
        gateway=args.gateway,
        token_file=args.token_file,
        path=Path(args.config) if args.config else None,
    )
    print(f"  wrote {args.config or CONFIG_PATH}")
    print(f"  identity={cfg.identity}")
    print(f"  gateway={cfg.gateway}")
    print(f"  token_file={cfg.token_file}")
    return 0


def _announce(identity: str) -> None:
    print(f"  (acting as: {identity})", file=sys.stderr)


def run_verb(identity: str, gateway: str, token: str, argv: list[str]) -> int:
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return 0

    verb = argv[0]
    rest = argv[1:]
    _announce(identity)

    if verb == "channels":
        print(format_response("channels", _api("GET", gateway, token, "/channels")))
        return 0

    if verb == "join":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me join <channel> [wake_policy]")
        body = {"peer": identity, "wake_policy": rest[1] if len(rest) > 1 else "all"}
        print(format_response(
            "join", _api("POST", gateway, token, f"/channels/{rest[0]}/join", body)
        ))
        return 0

    if verb == "leave":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me leave <channel>")
        body = {"peer": identity}
        _api("POST", gateway, token, f"/channels/{rest[0]}/leave", body)
        print()
        return 0

    if verb == "who":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me who <channel>")
        print(format_response(
            "who", _api("GET", gateway, token, f"/channels/{rest[0]}/members")
        ))
        return 0

    if verb == "read":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me read <channel> [n]")
        limit = rest[1] if len(rest) > 1 else "15"
        print(format_response(
            "read",
            _api("GET", gateway, token, f"/messages?channel={rest[0]}&limit={limit}"),
        ))
        return 0

    if verb == "say":
        # #458: prose through a file, never a shell-interpolated argument.
        parser = argparse.ArgumentParser(prog="swarph-me say", add_help=False)
        parser.add_argument("channel")
        parser.add_argument("--content-file", required=True)
        try:
            ns, _ = parser.parse_known_args(rest)
        except SystemExit:
            raise SystemExit(
                "usage: swarph-me say <channel> --content-file PATH"
            ) from None
        content = _read_content_file(ns.content_file)
        body = {
            "from_node": identity,
            "channel": ns.channel,
            "kind": "fyi",
            "content": content,
        }
        print(format_response(
            "posted", _api("POST", gateway, token, "/messages", body)
        ))
        return 0

    if verb == "unread":
        print(format_response(
            "unread",
            _api(
                "GET",
                gateway,
                token,
                f"/messages?to_node={identity}&unread_only=true&limit=20",
            ),
        ))
        return 0

    if verb == "cards":
        filt = rest[0] if rest else "ready"
        if filt == "ready":
            print(format_response(
                "cards-ready", _api("GET", gateway, token, "/board/cards")
            ))
        elif filt in (
            "build", "test", "proposed", "plan", "spec", "idea", "parked", "done",
        ):
            print(format_response(
                "cards",
                _api("GET", gateway, token, f"/board/cards?stage={filt}"),
            ))
        else:
            print(format_response(
                "cards",
                _api("GET", gateway, token, f"/board/cards?assignee={filt}"),
            ))
        return 0

    if verb == "show":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me show <id>")
        print(format_response(
            "card", _api("GET", gateway, token, f"/board/cards/{rest[0]}")
        ))
        return 0

    if verb == "thread":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me thread <id> [n]")
        limit = rest[1] if len(rest) > 1 else "15"
        print(format_response(
            "thread",
            _api(
                "GET",
                gateway,
                token,
                f"/board/cards/{rest[0]}/thread?limit={limit}",
            ),
        ))
        return 0

    if verb == "move":
        if len(rest) < 2:
            raise SystemExit("usage: swarph-me move <id> <stage>")
        body = {"actor": identity, "stage": rest[1]}
        print(format_response(
            "moved",
            _api("PATCH", gateway, token, f"/board/cards/{rest[0]}", body),
        ))
        return 0

    if verb == "assign":
        if len(rest) < 2:
            raise SystemExit("usage: swarph-me assign <id> <peer>")
        body = {"actor": identity, "assignee": rest[1]}
        print(format_response(
            "assigned",
            _api("PATCH", gateway, token, f"/board/cards/{rest[0]}", body),
        ))
        return 0

    if verb == "claim":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me claim <id>")
        body = {"actor": identity, "assignee": identity}
        print(format_response(
            "assigned",
            _api("PATCH", gateway, token, f"/board/cards/{rest[0]}", body),
        ))
        return 0

    if verb == "ready":
        if len(rest) < 1:
            raise SystemExit("usage: swarph-me ready <id> [clear]")
        flag = False if (len(rest) > 1 and rest[1] == "clear") else True
        body = {"actor": identity, "move_ready": flag}
        print(format_response(
            "ready",
            _api("PATCH", gateway, token, f"/board/cards/{rest[0]}", body),
        ))
        return 0

    if verb == "cardsay":
        parser = argparse.ArgumentParser(prog="swarph-me cardsay", add_help=False)
        parser.add_argument("card_id")
        parser.add_argument("--content-file", required=True)
        try:
            ns, _ = parser.parse_known_args(rest)
        except SystemExit:
            raise SystemExit(
                "usage: swarph-me cardsay <id> --content-file PATH"
            ) from None
        content = _read_content_file(ns.content_file)
        card = _api("GET", gateway, token, f"/board/cards/{ns.card_id}")
        if not isinstance(card, dict) or not card.get("thread_uuid"):
            raise SystemExit(
                f"  card #{ns.card_id} has no bound thread (predates #181a)"
            )
        to = card.get("assignee")
        if not to:
            raise SystemExit(
                f"  card #{ns.card_id} has no assignee — assign it first "
                f"(`swarph-me assign {ns.card_id} <peer>`)."
            )
        resp = _api(
            "POST",
            gateway,
            token,
            "/messages",
            {
                "from_node": identity,
                "to_node": to,
                "kind": "status",
                "content": content,
                "thread_uuid": card["thread_uuid"],
            },
        )
        line = f"  posted #{resp.get('id')} onto card #{ns.card_id} (to {to})"
        closed = resp.get("closed_obligations") or [] if isinstance(resp, dict) else []
        if closed:
            line += f"  CLOSED obligations {closed}"
        print(line)
        return 0

    if verb == "due":
        if len(rest) < 2:
            raise SystemExit(
                "usage: swarph-me due <id> <YYYY-MM-DD> [HH:MM]  "
                "|  swarph-me due <id> clear"
            )
        if rest[1] == "clear":
            # "" CLEARS; None means "not mentioned" and the gateway skips the
            # write while echoing the old date back (#901, server.py ~12884).
            body = {"actor": identity, "due_at": ""}
        else:
            hhmm = rest[2] if len(rest) > 2 else "14:00"
            body = {
                "actor": identity,
                "due_at": f"{rest[1]}T{hhmm}:00+00:00",
            }
        c = _api("PATCH", gateway, token, f"/board/cards/{rest[0]}", body)
        if isinstance(c, dict) and "detail" in c:
            print(f"  refused: {c['detail']}")
            return 1
        if rest[1] == "clear" and c.get("due_at"):
            # #901: the echo is not the intent. A 200 carrying the date you just
            # tried to remove is a skipped write, not a success.
            print(
                f"  NOT CLEARED: the gateway still carries due_at {c.get('due_at')} "
                f"on #{c.get('id')} -- the write was skipped"
            )
            return 1
        print(
            f"  #{c.get('id')} [{c.get('stage')}] due: {c.get('due_at') or '(cleared)'}"
        )
        print(f"  {(c.get('title') or '')[:96]}")
        print(
            "  NOTE: due_state is computed only by GET /board/cards (the LIST). "
            "A single-card read returns due_state=None even when due_at is set."
        )
        return 0

    if verb == "sched":
        print(format_response(
            "sched", _api("GET", gateway, token, "/scheduled-events")
        ))
        return 0

    if verb == "schedule":
        # TIME TRIGGER ONLY (#185). Prose via --content-file (#458).
        parser = argparse.ArgumentParser(prog="swarph-me schedule", add_help=False)
        parser.add_argument("name")
        parser.add_argument("cron")
        parser.add_argument("target")
        parser.add_argument("--content-file", required=True)
        parser.add_argument("anchors", nargs="*")
        try:
            ns = parser.parse_args(rest)
        except SystemExit:
            raise SystemExit(
                'usage: swarph-me schedule <name> "<cron>" <target> '
                "[file=… memory=… repo=…] --content-file PATH"
            ) from None
        task = _read_content_file(ns.content_file).strip()
        anchors = []
        for a in ns.anchors:
            if "=" in a and a.split("=", 1)[0] in (
                "channel", "feature", "file", "memory", "repo",
            ):
                k, v = a.split("=", 1)
                anchors.append({k: v})
        if not anchors:
            anchors = [{"feature": ns.name}]
        body = {
            "name": ns.name,
            "trigger_type": "time",
            "cron": ns.cron,
            "target_cell": ns.target,
            "task": task,
            "context_ref": anchors,
            "created_by": identity,
        }
        resp = _api("POST", gateway, token, "/scheduled-events", body)
        print(json.dumps(resp, indent=2) if isinstance(resp, dict) else resp)
        return 0

    print(HELP)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        as_name, stripped = extract_as(raw)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    # Optional --config before verb for tests / alternate homes.
    config_path: Path | None = None
    if "--config" in stripped:
        i = stripped.index("--config")
        if i + 1 >= len(stripped):
            print("swarph-me: --config needs a path", file=sys.stderr)
            return 2
        config_path = Path(stripped[i + 1])
        del stripped[i : i + 2]

    if stripped and stripped[0] == "config":
        parser = argparse.ArgumentParser(prog="swarph-me config")
        parser.add_argument("--identity", required=True)
        parser.add_argument("--gateway", required=True)
        parser.add_argument("--token-file", default=None)
        parser.add_argument(
            "--config",
            default=None,
            help="override config path (default: ~/.config/swarph/operator.json)",
        )
        ns = parser.parse_args(stripped[1:])
        # Prefer an earlier --config on the line if both appear.
        if config_path is not None and ns.config is None:
            ns.config = str(config_path)
        return cmd_config(ns)

    # help needs no token — otherwise a fresh install cannot discover the verbs.
    if not stripped or stripped[0] in ("help", "-h", "--help"):
        print(HELP)
        return 0

    try:
        identity, gateway, token = resolve_session(as_name, config_path=config_path)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"swarph-me: {exc}", file=sys.stderr)
        return 2

    try:
        return run_verb(identity, gateway, token, stripped)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            if exc.args and not isinstance(exc.args[0], int):
                print(exc.args[0], file=sys.stderr)
            return code
        if code is None:
            return 0
        print(code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
