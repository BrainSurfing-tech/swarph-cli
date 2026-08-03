"""``swarph onboard <peer-name>`` — Phase 5.5 mechanics-phase per PLAN.md §15.4.

Six mechanics steps execute automatically; the seventh — composing
and sending the handshake DM — is **manual by design** (§15.1) so the
new peer's own-words ack of the four invariants reflects active
understanding rather than boilerplate provisioning.

Idempotent: rerun safe. Each step's gateway call is upsert-shaped
(POST /peers/register on conflict updates) or guarded (scaffold dir
mkdir -p). Re-running on an already-onboarded peer surfaces "already
registered" without harming state.

Auth resolution (step 3):
  1. ``MESH_GATEWAY_TOKEN`` env var
  2. ``~/.swarph/secrets.toml`` mode 0600 (read-only — does not auto-create)
  3. Interactive prompt
  4. NEVER from argv (would land in shell history)

Cross-runtime (§15.6 #10): Claude-only in v0; Gemini/non-Claude
runtime scaffolding lands alongside that adapter's Phase 6 rollout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from getpass import getpass
from pathlib import Path
from typing import Optional
from swarph_cli.console_safe import print_safe


_HANDSHAKE_TEMPLATE = """\
# Handshake DM — {peer}

> **Manual step.** Per PLAN.md §15.1, the contract phase is preserved
> as a manual artifact so your own-words acknowledgement reflects active
> understanding, not provisioning. Edit each section below in your own
> words. Generic boilerplate will be flagged + rejected by the witness.

## 1. DM SEMANTICS

> Reference: hedge-fund-mcp `CLAUDE.md` Science Claude Mesh Bootstrap
> section, "DM semantics: AI-to-AI is the default…"

[your own-words ack here — what does AI-to-AI-by-default mean for
how you'll handle routine peer DMs vs. ones crossing a privilege
boundary?]

## 2. Framing-contagion

> Reference: auto-memory `project_peer_name_canonical.md`. Your
> canonical name in the registry is `{peer}`.

[your own-words ack — how will you stay canonical + flag wrong-name
DMs you receive?]

## 3. Transparency-by-default

> Reference: swarph paper main draft §3.7.

[your own-words ack — what does transparency-by-default look like
when you slip? When do you DM peers vs. self-fix?]

## 4. Mesh-secrets out-of-band

> Reference: hedge-fund-mcp `CLAUDE.md` Critical operational rules,
> "Mesh secrets out-of-band only" bullet.

[your own-words ack — what counts as a mesh secret + what's your
fallback channel when you must convey one?]

---

**To send:**

```
swarph "$(<{tmp_path})" --provider <your-llm> --caller {peer}.handshake.witness-{witness}
```

Or paste the rendered text into a DM via the gateway's
``POST /messages`` API to your witness peer (default
``science-claude``). The witness will read both this DM AND any
imported session JSONL (§17.2a flow), then run
``swarph ratify {peer}`` to flip ``ratified=true``.

**Status:** registered_unratified=true. You can read inbox + send
DMs (so the handshake itself works), but ``task_claim`` is
gateway-refused until ratified.
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph onboard",
        description="Phase 5.5 mechanics-phase peer onboarding per PLAN.md §15.4.",
    )
    p.add_argument("peer", help="canonical peer name (e.g. razorpeter)")
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", "http://localhost:8788"),
        help="mesh-gateway base URL (default: $MESH_GATEWAY_URL or http://localhost:8788)",
    )
    p.add_argument(
        "--token-file",
        default=None,
        help="explicit path to a credential file (mode 0600 expected). "
        "Default resolution order: --token-file → $MESH_GATEWAY_TOKEN → "
        "~/.swarph/secrets.toml → ~/.config/swarph/<$SWARPH_SELF>.peer_token. "
        "The per-peer file is the CURRENT convention; the first three are legacy "
        "and are absent on migrated cells. SWARPH_SELF must be set — this verb "
        "will not guess a peer name.",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="local state directory root (default: ~/swarph_state).",
    )
    p.add_argument(
        "--url",
        default=None,
        help="this peer's HTTP URL for the registry (default: http://<peer>:8787).",
    )
    p.add_argument(
        "--capability",
        action="append",
        default=[],
        help="capability advert as KEY=VALUE (repeatable). VALUE parsed as JSON if possible. "
        'Defaults to {"can_claim_tasks": true} if none given.',
    )
    return p


def _resolve_token(token_file_arg: Optional[str],
                   source_out: Optional[list] = None) -> str:
    """Step 3 — token resolution per §15.4. Read-only on the secrets file
    (does not auto-create per drop DM #726 #3 — privilege boundary).

    >>> ``source_out`` EXISTS SO A 401 IS ATTRIBUTABLE. <<< This resolver tries
    four locations and used to report only "ok", so when the gateway then
    answered ``401 bad token`` there was no way to know WHICH credential had
    been sent — and the most common cause is a stale ``$MESH_GATEWAY_TOKEN``
    silently winning over a perfectly good per-peer file two steps below it.
    A resolver that finds *a* credential has not told you it found *the right
    one*; naming the source is what makes the next failure diagnosable.
    Purely additive: callers that pass nothing are unaffected.
    """
    def _note(s: str) -> None:
        if source_out is not None:
            source_out.append(s)

    env_tok = os.environ.get("MESH_GATEWAY_TOKEN")
    if env_tok:
        _note("$MESH_GATEWAY_TOKEN (env)")
        return env_tok

    secrets_path = (
        Path(token_file_arg).expanduser()
        if token_file_arg
        else Path.home() / ".swarph" / "secrets.toml"
    )
    if secrets_path.exists():
        try:
            mode = secrets_path.stat().st_mode & 0o777
            if mode != 0o600:
                print_safe(
                    f"swarph onboard: WARNING: {secrets_path} mode is {oct(mode)}, "
                    f"expected 0600. Continuing — fix manually with `chmod 600 {secrets_path}`.",
                    file=sys.stderr,
                )
            content = secrets_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if line.startswith("MESH_GATEWAY_TOKEN"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        _note(f"{secrets_path}")
                        return val
            # >>> A RAW TOKEN FILE IS THE COMMON CASE AND WAS BEING DISCARDED IN
            # SILENCE. <<< This branch only ever understood secrets.toml syntax
            # (`MESH_GATEWAY_TOKEN=...`). A per-peer token file is a BARE TOKEN
            # with no key — so `--token-file <peer>.peer_token`, which is exactly
            # what the flag's own help invites, matched nothing, fell through
            # without a word, and the NEXT source's credential produced a 401
            # that looked unrelated to the flag the operator had just passed.
            # Reported from a Windows cell, 2026-08-03.
            # Accept it: a single non-empty line with no '=' IS the credential.
            stripped = [ln.strip() for ln in content.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")]
            if len(stripped) == 1 and "=" not in stripped[0]:
                _note(f"{secrets_path} (raw token)")
                return stripped[0]
            if token_file_arg:
                # EXPLICITLY POINTED AT A FILE THAT YIELDED NOTHING -> REFUSE.
                # Falling through here is what made the original failure
                # undiagnosable: the operator's explicit choice was overridden by
                # a default they never named. An explicit flag that silently
                # loses to an implicit source is worse than no flag.
                print_safe(
                    f"swarph onboard: --token-file {secrets_path} contained no usable "
                    f"credential.\n"
                    f"  Expected EITHER a bare token on one line, OR a secrets.toml "
                    f"line 'MESH_GATEWAY_TOKEN=...'.\n"
                    f"  REFUSING to silently fall back to another source — you named "
                    f"this file explicitly.",
                    file=sys.stderr,
                )
                return ""
        except Exception as exc:
            print_safe(
                f"swarph onboard: failed to read {secrets_path}: {exc}", file=sys.stderr
            )

    # ── #243: THE PER-PEER TOKEN — THE CREDENTIAL THAT ACTUALLY EXISTS ──────
    # >>> THIS VERB WAS LOOKING ONLY FOR THE CREDENTIAL THE R1 MIGRATION
    # RETIRED. <<< Measured on lab-ovh 2026-08-03: $MESH_GATEWAY_TOKEN UNSET,
    # ~/.swarph/secrets.toml ABSENT, and 10+ files present at
    # ~/.config/swarph/<peer>.peer_token. Every other verb resolves the per-peer
    # file; onboard (and ratify, which re-exports this function, and daemon,
    # which copied it) were left on the old path — classic mint-vs-cutover, the
    # credential moved and three consumers did not.
    #
    # AND THE GATEWAY NEVER REQUIRED THE SHARED TOKEN: measured, POST
    # /peers/register with a PER-PEER token returns 200 and mints. So this was
    # never a permissions problem — only a lookup that never learned.
    #
    # Placed AFTER the existing two so a working operator-token setup is
    # unchanged; this only fills the hole where the verb used to prompt.
    self_name = os.environ.get("SWARPH_SELF", "").strip()
    if self_name:
        peer_tok = Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"
        if peer_tok.exists():
            try:
                val = peer_tok.read_text(encoding="utf-8").strip()
                if val:
                    _note(str(peer_tok))
                    return val
            except Exception as exc:
                print_safe(f"swarph onboard: failed to read {peer_tok}: {exc}",
                           file=sys.stderr)

    # >>> REFUSE. DO NOT PROMPT. <<< getpass on a non-tty is a guaranteed
    # EOFError, and this verb runs from scripts, cron and spawned cells — the
    # traceback it produced was reported as a BROKEN VERB, not as a missing
    # credential, because the prompt hid which of the two it was.
    #
    # NOTE the deliberate absence of a default for SWARPH_SELF: guessing a name
    # makes a cell hunt ANOTHER CELL'S token, find nothing, and blame the
    # credential — measured on 6 of 6 cells 2026-07-29. Unset is named as unset.
    raise RuntimeError(
        "no gateway credential found. Tried, in order:\n"
        f"  1. --token-file            {'(not given)' if not token_file_arg else token_file_arg}\n"
        "  2. $MESH_GATEWAY_TOKEN     (unset)\n"
        f"  3. {secrets_path}\n"
        f"  4. ~/.config/swarph/<self>.peer_token  "
        + (f"(SWARPH_SELF={self_name!r} -> not found)" if self_name
           else "(SWARPH_SELF IS UNSET — set it; this verb will not guess a peer name)")
        + "\n  The per-peer token is sufficient: POST /peers/register accepts it."
    )


def _post_json(
    url: str, body: dict, token: str, *, method: str = "POST"
) -> tuple[int, dict]:
    """Tiny stdlib HTTP client. Avoids httpx dep at the CLI layer.

    Returns (status, parsed_body). On non-2xx, parsed_body is the error
    JSON payload (best-effort) so callers can surface gateway error text."""
    data = json.dumps(body).encode("utf-8")
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
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body


def _parse_capability(spec: str) -> tuple[str, object]:
    """``KEY=VALUE`` → (key, value). VALUE parsed as JSON when possible
    (so ``can_claim_tasks=true`` lands as bool, not string)."""
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"capability {spec!r} not KEY=VALUE shape")
    k, v = spec.split("=", 1)
    try:
        return k.strip(), json.loads(v)
    except json.JSONDecodeError:
        return k.strip(), v


def run_onboard(argv: list[str]) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch.

    Returns process exit code: 0 on success, 1 on validation fail,
    2 on gateway error."""
    args = _build_parser().parse_args(argv)

    # ── Step 1: validate_node_name ───────────────────────────────────
    print_safe(f"[1/6] validate_node_name({args.peer!r})")
    try:
        from swarph_shared.peer_registry import (
            validate_node_name,
            NotInRegistry,
            GatewayUnreachableError,
        )
    except ImportError as exc:
        print_safe(f"swarph onboard: missing swarph-shared>=0.2.0: {exc}", file=sys.stderr)
        return 1

    # NotInRegistry is expected here — onboard's whole point is that
    # the peer doesn't exist yet. We only enforce the regex shape.
    try:
        from swarph_shared.peer_registry import NAMING_CONVENTION_REGEX, KNOWN_ALIASES
    except ImportError:
        print_safe("swarph onboard: peer_registry primitives missing", file=sys.stderr)
        return 1

    canonical = KNOWN_ALIASES.get(args.peer, args.peer)
    if canonical != args.peer:
        print_safe(
            f"      WARN: {args.peer!r} resolved to canonical {canonical!r} "
            f"(contagion alias)",
            file=sys.stderr,
        )
    if not NAMING_CONVENTION_REGEX.match(canonical):
        print_safe(
            f"swarph onboard: {canonical!r} fails naming convention "
            f"(^[a-z][a-z0-9-]*[a-z0-9]$)",
            file=sys.stderr,
        )
        return 1
    print_safe(f"      ok ({canonical!r})")

    # ── Step 2: would-write peer-registry row (effectively step 4) ───
    # The PLAN's step 2 is logically subsumed by step 4 (the gateway
    # POST is the only persistent registry write). We surface it as a
    # planning/dry-run line for operator clarity.
    capabilities = dict(_parse_capability(c) for c in args.capability) if args.capability else {
        "can_claim_tasks": True
    }
    print_safe(f"[2/6] prepare peer-registry row (caps={capabilities})")

    # ── Step 3: resolve MESH_GATEWAY_TOKEN ───────────────────────────
    print_safe("[3/6] resolve MESH_GATEWAY_TOKEN")
    _src: list = []
    token = _resolve_token(args.token_file, _src)
    if not token:
        print_safe("swarph onboard: empty token", file=sys.stderr)
        return 1
    # NAME THE SOURCE. "ok" alone made a subsequent `401 bad token` undiagnosable:
    # four locations are tried and the winner was never stated, so a stale
    # $MESH_GATEWAY_TOKEN silently beating a good per-peer file two steps below
    # looked identical to having no credential problem at all.
    print_safe(f"      ok (from {_src[0] if _src else 'unknown source'})")

    # ── Step 4: POST /peers/register ─────────────────────────────────
    peer_url = args.url or f"http://{canonical}:8787"
    print_safe(f"[4/6] POST {args.gateway}/peers/register")
    status, body = _post_json(
        f"{args.gateway}/peers/register",
        {"name": canonical, "url": peer_url, "capabilities": capabilities},
        token,
    )
    if status != 200:
        print_safe(
            f"swarph onboard: gateway register failed: {status} {body}",
            file=sys.stderr,
        )
        return 2
    if body.get("registered_unratified") is False:
        print_safe(
            f"      ok (already ratified — peer existed pre-Phase-5.5 or was "
            f"witness-flipped already)"
        )
    else:
        print_safe(f"      ok (registered_unratified=true)")

    # ── Step 4b: PERSIST THE MINTED PER-PEER TOKEN ───────────────────
    # >>> THE GATEWAY MINTS THE PEER'S CREDENTIAL EXACTLY ONCE, HERE, AND
    # RETURNS IT IN THIS RESPONSE. <<< Earlier releases read only
    # `registered_unratified` and DISCARDED `peer_token` — which left every
    # onboarded peer registered, with a live token nobody held, and unable to
    # authenticate. The server never re-mints (a re-register returns
    # peer_token=None + token_status='existing'), so the credential was
    # unrecoverable without an admin revoke. Onboarding APPEARED to succeed and
    # produced a peer that could not join: the defect is silent precisely
    # because the failure surfaces later, somewhere else, as "auth doesn't work".
    minted = body.get("peer_token")
    token_status = body.get("token_status")
    if minted:
        dest = Path.home() / ".config" / "swarph" / f"{canonical}.peer_token"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 0600 from the instant it exists — os.open with O_EXCL, never
        # open()+chmod, which leaves a umask-mode window.
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(minted.strip() + "\n")
        print_safe(f"[4b/6] minted per-peer token -> {dest} (mode 0600)")
        print_safe(f"       set SWARPH_SELF={canonical} in the peer's environment; "
                   f"this path is how it authenticates.")
    elif token_status == "existing":
        # NOT a success. Name it as the unrecoverable state it is.
        print_safe(
            f"[4b/6] WARNING: token_status='existing' and no token returned.\n"
            f"       The gateway minted {canonical}'s credential on an EARLIER register\n"
            f"       and MINTS ONCE — so if no {canonical}.peer_token exists on disk,\n"
            f"       that credential is held by nobody and cannot be recovered here.\n"
            f"       Recovery is an ADMIN action: revoke the generation (or DELETE the\n"
            f"       peer) so a re-register mints a fresh one. A per-peer token cannot\n"
            f"       do it — deregister is ownership-gated to the caller itself.",
            file=sys.stderr,
        )

    # ── Step 5: subscription auth check ──────────────────────────────
    print_safe("[5/6] verify_subscription_setup()")
    try:
        from swarph_shared import verify_subscription_setup

        # The function returns either True or raises an informative error;
        # catch broadly so onboarding doesn't blow up on Claude-runtime-only
        # checks when the peer is non-Claude (§15.6 #10 deferred to Phase 6).
        verify_subscription_setup()
        print_safe("      ok (Claude subscription credentials + binary verified)")
    except Exception as exc:
        print_safe(
            f"      WARN: {type(exc).__name__}: {exc}\n"
            f"      Subscription path won't work for this peer until resolved. "
            f"Non-Claude runtimes (Gemini, etc.) ship in Phase 6 per §15.6 #10.",
            file=sys.stderr,
        )

    # ── Step 6: scaffold local state directory ───────────────────────
    state_root = (
        Path(args.state_dir).expanduser()
        if args.state_dir
        else Path.home() / "swarph_state"
    )
    peer_dir = state_root / canonical
    print_safe(f"[6/6] scaffold {peer_dir}")
    peer_dir.mkdir(parents=True, exist_ok=True)
    try:
        peer_dir.chmod(0o700)
    except OSError:
        pass  # best-effort; Windows or fs without POSIX modes
    inbox_log = peer_dir / "inbox.log"
    cursor_path = peer_dir / "cursor.json"
    env_example = peer_dir / ".env.example"
    daemon_sh = peer_dir / "run-daemon.sh"

    if not inbox_log.exists():
        inbox_log.touch()
    if not cursor_path.exists():
        cursor_path.write_text(
            json.dumps({"last_msg_id": 0, "tasks_snapshot": {}}, indent=2),
            encoding="utf-8",
        )
    if not env_example.exists():
        env_example.write_text(
            f"# swarph state for {canonical}\n"
            f"MESH_GATEWAY_TOKEN=\n"
            f"MESH_GATEWAY_URL={args.gateway}\n",
            encoding="utf-8",
        )
    if not daemon_sh.exists():
        daemon_sh.write_text(
            f"#!/usr/bin/env bash\n"
            f"# Phase 5.6 launcher — runs `swarph daemon` with this peer's state.\n"
            f"# Pre-launch via: nohup ./run-daemon.sh &\n"
            f"exec swarph daemon --state-dir {peer_dir}\n",
            encoding="utf-8",
        )
        try:
            daemon_sh.chmod(0o755)
        except OSError:
            pass  # best-effort; Windows or fs without POSIX modes
    print_safe(f"      ok (inbox.log, cursor.json, .env.example, run-daemon.sh)")

    # ── Step 7: handshake template (MANUAL) ──────────────────────────
    # tempfile.gettempdir() is the platform temp dir ('/tmp' on POSIX, the
    # %TEMP% path on Windows) so the write doesn't land on a nonexistent
    # '\tmp\' dir on Windows.
    tmp_path = Path(tempfile.gettempdir()) / f"{canonical}-handshake.md"
    tmp_path.write_text(
        _HANDSHAKE_TEMPLATE.format(
            peer=canonical, witness="science-claude", tmp_path=tmp_path
        ),
        encoding="utf-8",
    )
    print_safe(
        f"\n[manual] handshake template at {tmp_path}\n"
        f"  Edit each section in your own words, then send to your witness peer.\n"
        f"  After witness reads + judges sufficient, they run:\n"
        f"      swarph ratify {canonical} --reason \"<short text>\"\n"
        f"  to flip ratified=true.\n"
    )
    return 0
