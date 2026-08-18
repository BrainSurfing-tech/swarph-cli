"""``swarph gh-route`` — the #397 PreToolUse hook body, and its inspector.

Two entry points, one resolver:

  ``swarph gh-route hook``    reads a Claude Code PreToolUse payload on stdin and
                              emits the hook JSON that rewrites `gh ...` into
                              `GH_TOKEN=$(gh auth token --user <login>) gh ...`,
                              or DENIES when the cell is unmapped.
  ``swarph gh-route show``    prints what THIS cell would resolve to, and exits
                              non-zero on a refusal.

>>> `show` EXISTS BECAUSE A ROUTER YOU CANNOT INTERROGATE IS A SECOND AMBIENT
IDENTITY. <<< The whole defect class here is that structured provenance and prose
provenance disagreed and nothing could notice. An operator must be able to ask
"who am I about to be?" without performing a write to find out — the same reason
GET /whoami exists on the gateway and the same reason a DELETE was the wrong way
to learn a token's binding.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from swarph_cli import gh_identity as ghid
from swarph_cli.console_safe import print_safe


def _deny(reason: str) -> int:
    """Emit a PreToolUse denial. The reason is operator-facing and must name the
    remedy — a refusal that does not carry its escape is #468's class."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"swarph gh-route (#397): {reason}",
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _allow_unchanged() -> int:
    """Say nothing. An empty output leaves the tool call exactly as it was — the
    router must be inert on every command it does not own."""
    return 0


def run_hook(stdin_text: Optional[str] = None) -> int:
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        # >>> FAIL OPEN ON A MALFORMED PAYLOAD, NOT CLOSED. <<< A router that cannot
        # parse its input has learned nothing about the command, so denying would
        # brick every Bash call on a harness change. It is the UNMAPPED CELL that
        # must fail closed, not the unreadable envelope.
        return _allow_unchanged()

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not ghid.targets_gh(command):
        return _allow_unchanged()
    if ghid.already_explicit(command):
        # The caller named a credential. #332: an explicit argument is a decision.
        return _allow_unchanged()

    peer = os.environ.get("SWARPH_SELF", "").strip()
    try:
        res = ghid.resolve(peer or None)
    except ghid.RouterRefusal as exc:
        return _deny(str(exc))

    rewritten = ghid.inject(command, res.login)
    updated = dict(payload.get("tool_input") or {})
    updated["command"] = rewritten
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        },
        # >>> STATE THE IDENTITY. <<< A wrong mapping must be DIAGNOSABLE rather
        # than silently wrong; the injected identity is the one fact a reader needs
        # and the one the old ambient path never printed.
        "systemMessage": f"gh identity: {res.peer} -> {res.login} (#397 router)",
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def run_show() -> int:
    peer = os.environ.get("SWARPH_SELF", "").strip()
    try:
        res = ghid.resolve(peer or None)
    except ghid.RouterRefusal as exc:
        print_safe(f"swarph gh-route: REFUSED\n  {exc}", file=sys.stderr)
        return 1
    print_safe(f"cell        {res.peer}")
    print_safe(f"github      {res.login}")
    print_safe(f"mapping     {res.source}")
    print_safe(f"injected as GH_TOKEN=$(gh auth token --user {res.login}) <your gh command>")
    return 0


def run_gh_route(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="swarph gh-route",
        description="#397 GitHub identity router: resolve a cell's gh identity, or refuse.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="PreToolUse hook body (reads the payload on stdin)")
    sub.add_parser("show", help="print what THIS cell resolves to; non-zero on refusal")
    args = p.parse_args(argv)
    return run_hook() if args.cmd == "hook" else run_show()
