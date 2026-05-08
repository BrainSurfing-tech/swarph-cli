"""``swarph ratify <peer-name>`` — Phase 5.5 witness flip per PLAN.md §15.4a.

Run by an already-ratified peer after they've read + judged a
handshake DM as sufficient (§15.5). Server-side gates are enforced
by ``mesh-gateway`` PR A; this command surfaces those gates as
friendly error messages and writes the audit row through the
``PATCH /peers/{name}`` endpoint.

Six steps mirror PLAN.md §15.4a.

§17.2a witness scope expansion: when the target peer ran
``swarph import`` before the handshake, the witness reads BOTH the
handshake DM AND the imported session JSONL, judging
substance↔context linkage. Cold-start peers (no import) skip that
layer. This subcommand doesn't know which path was taken — the
witness's `--reason` should reflect what they checked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph ratify",
        description="Phase 5.5 witness ratification per PLAN.md §15.4a.",
    )
    p.add_argument("peer", help="canonical peer name to ratify")
    p.add_argument(
        "--reason",
        default=None,
        help='short free-text explaining the witness judgment '
        '(e.g. "handshake covers all four invariants in own words; '
        'session import substance matches §17.2a")',
    )
    p.add_argument(
        "--witness-name",
        default=None,
        help="canonical name of the witness peer running this command. "
        "Defaults to $SWARPH_WITNESS or the first ratified peer this "
        "host can identify.",
    )
    p.add_argument(
        "--witness-dm-id",
        type=int,
        default=None,
        help="optional claude_messages.id pointer to the handshake DM that "
        "informed this ratification (audit-trail tightening).",
    )
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", "http://localhost:8788"),
        help="mesh-gateway base URL.",
    )
    p.add_argument(
        "--token-file",
        default=None,
        help="explicit path to a secrets file (mode 0600 expected). "
        "Default: $MESH_GATEWAY_TOKEN env → ~/.swarph/secrets.toml → prompt.",
    )
    return p


def _resolve_token(token_file_arg: Optional[str]) -> str:
    """Mirror the resolution order in onboard.py — ratify needs the
    same token to PATCH /peers/{name}."""
    # Re-export the onboard helper to keep token resolution behavior
    # identical across the two subcommands.
    from swarph_cli.commands.onboard import _resolve_token as _onboard_resolve

    return _onboard_resolve(token_file_arg)


def _resolve_witness(arg: Optional[str]) -> str:
    if arg:
        return arg
    env = os.environ.get("SWARPH_WITNESS")
    if env:
        return env
    return ""


def _http_json(
    url: str, *, token: str, method: str = "GET", body: Optional[dict] = None
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body


def run_ratify(argv: list[str]) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch."""
    args = _build_parser().parse_args(argv)

    # ── Step 1: validate_node_name ───────────────────────────────────
    print(f"[1/6] validate_node_name({args.peer!r})")
    try:
        from swarph_shared.peer_registry import (
            NAMING_CONVENTION_REGEX,
            KNOWN_ALIASES,
        )
    except ImportError as exc:
        print(f"swarph ratify: missing swarph-shared>=0.2.0: {exc}", file=sys.stderr)
        return 1
    canonical = KNOWN_ALIASES.get(args.peer, args.peer)
    if canonical != args.peer:
        print(
            f"      WARN: {args.peer!r} resolved to canonical {canonical!r}",
            file=sys.stderr,
        )
    if not NAMING_CONVENTION_REGEX.match(canonical):
        print(
            f"swarph ratify: {canonical!r} fails naming convention",
            file=sys.stderr,
        )
        return 1
    print(f"      ok ({canonical!r})")

    witness = _resolve_witness(args.witness_name)
    if not witness:
        print(
            "swarph ratify: cannot resolve witness peer name. "
            "Set $SWARPH_WITNESS or pass --witness-name <peer>.",
            file=sys.stderr,
        )
        return 1
    if witness == canonical:
        print(
            f"swarph ratify: witness {witness!r} == target {canonical!r} "
            f"— self-ratification rejected (§15.4a step 2).",
            file=sys.stderr,
        )
        return 1

    token = _resolve_token(args.token_file)
    if not token:
        print("swarph ratify: empty token", file=sys.stderr)
        return 1

    # ── Step 2: caller-side gate (witness must itself be ratified) ───
    print(f"[2/6] verify witness {witness!r} is ratified")
    status, witness_body = _http_json(
        f"{args.gateway}/peers/{witness}", token=token
    )
    if status == 404:
        print(
            f"swarph ratify: witness {witness!r} not registered with gateway",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(
            f"swarph ratify: gateway error fetching witness: {status} {witness_body}",
            file=sys.stderr,
        )
        return 2
    if not witness_body.get("ratified"):
        print(
            f"swarph ratify: witness {witness!r} is not ratified — "
            f"unratified peers cannot ratify others (§15.4a step 2).",
            file=sys.stderr,
        )
        return 1
    print(f"      ok (witness ratified)")

    # ── Step 3: target must be in registered_unratified state ────────
    print(f"[3/6] verify {canonical!r} is registered_unratified")
    status, target_body = _http_json(
        f"{args.gateway}/peers/{canonical}", token=token
    )
    if status == 404:
        print(
            f"swarph ratify: target {canonical!r} not registered. "
            f"Run `swarph onboard {canonical}` first.",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(
            f"swarph ratify: gateway error fetching target: {status} {target_body}",
            file=sys.stderr,
        )
        return 2
    if target_body.get("ratified"):
        print(
            f"swarph ratify: target {canonical!r} is already ratified "
            f"(by {target_body.get('ratified_by')!r} at "
            f"{target_body.get('ratified_at')}). "
            f"Re-ratification is rejected by the gateway (audit append-only).",
            file=sys.stderr,
        )
        return 1
    print(f"      ok (registered_unratified=true)")

    # ── Step 4: PATCH /peers/{name} with ratification flip ───────────
    print(f"[4/6] PATCH {args.gateway}/peers/{canonical}")
    patch_body = {
        "ratified": True,
        "ratified_by": witness,
        "reason": args.reason,
    }
    if args.witness_dm_id is not None:
        patch_body["witness_dm_id"] = args.witness_dm_id
    status, body = _http_json(
        f"{args.gateway}/peers/{canonical}",
        token=token,
        method="PATCH",
        body=patch_body,
    )
    if status != 200:
        print(
            f"swarph ratify: gateway PATCH failed: {status} {body}",
            file=sys.stderr,
        )
        return 2
    print(f"      ok (ratified=true at {body.get('ratified_at')})")

    # ── Step 5: audit row written by gateway as part of PATCH txn ────
    # Server-side §15.4a step 5 is atomic with step 4; we just verify
    # the audit query returns at least one row.
    print(f"[5/6] verify peer_ratifications audit row")
    status, audit_body = _http_json(
        f"{args.gateway}/peers/{canonical}/ratifications", token=token
    )
    if status != 200:
        print(
            f"swarph ratify: WARN — could not verify audit row: {status} {audit_body}",
            file=sys.stderr,
        )
    else:
        rows = audit_body.get("ratifications", [])
        latest = rows[-1] if rows else None
        if latest and latest.get("ratified_by") == witness:
            print(
                f"      ok (audit row id={latest['id']} reason={latest.get('reason')!r})"
            )
        else:
            print(
                f"      WARN: audit row missing or witness mismatch: {latest!r}",
                file=sys.stderr,
            )

    # ── Step 6: invalidate local TTL cache + print confirmation ──────
    print(f"[6/6] invalidate local TTL cache (next adapter read sees ratified=true)")
    try:
        # peer_registry's TTL cache is a module-level dict; nudge a fresh
        # fetch so subsequent calls in this process pick up the flip.
        from swarph_shared.peer_registry import canonical_names

        canonical_names(ttl_seconds=0)
    except Exception:
        pass  # cache invalidation is best-effort; gateway is source of truth
    print(f"      ok")

    print(
        f"\nratification complete: {canonical} ratified by {witness} "
        f"(reason={args.reason!r}). audit-trail row stays forever."
    )
    return 0
