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
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from getpass import getpass
from pathlib import Path
from typing import Optional
from swarph_cli import tokens
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
        "--check",
        action="store_true",
        help="PROBE ONLY. Print where this peer actually stands on the onboarding "
             "ladder and what the next action is for each gap. Changes nothing.",
    )
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", "http://100.107.222.72:8788"),
        help="mesh-gateway base URL (default: $MESH_GATEWAY_URL or http://100.107.222.72:8788)",
    )
    p.add_argument(
        "--token-file",
        default=None,
        help="explicit credential file (bare token or env-style; mode 0600 "
        "expected). WHEN GIVEN IT WINS OUTRIGHT — no fallback is attempted, and "
        "an unreadable path is an error, not a hint to guess (#332). When NOT "
        "given: $MESH_GATEWAY_TOKEN → ~/.swarph/secrets.toml → "
        "~/.config/swarph/<self>.peer_token. This verb never prompts (#243).",
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


def _resolve_token(token_file_arg: Optional[str], *,
                   target_peer: Optional[str] = None) -> str:
    """Step 3 — token resolution per §15.4. Read-only on the secrets file
    (does not auto-create per drop DM #726 #3 — privilege boundary).

    `target_peer` — the peer name this credential will be used to REGISTER, when
    that is known. Only rung 4 (the cell's own per-peer token) cares: a per-peer
    token may register ITSELF and nothing else. Default None means "not a
    registration", which is why `ratify` and `daemon` — which import this
    function and never call POST /peers/register — are unaffected.

    >>> WHY THIS PARAMETER EXISTS (#467b, found by drop-on-meta-edge). <<< The
    gateway now binds POST /peers/register's `name` to the authenticated caller.
    `swarph onboard <peer>` takes the peer name as a POSITIONAL ARGUMENT — the
    help text's own example is a different box — and rung 4 hands it THIS cell's
    credential, so `swarph onboard razorpeter` on lab-ovh presents lab-ovh's
    token to register razorpeter. That is a cell credential performing an
    OPERATOR action, and it worked only because the server never asked.

    Contrast `swarph mesh register`, which passes allow_peer_token=False and
    registers only itself: two verbs, one endpoint, opposite credential policies,
    and only one was written with this question in mind.

    Refusing HERE rather than letting the gateway 403 is the point. The server's
    message names a binding rule ("peer 'lab-ovh' may not act as
    register_mint_target='razorpeter'"); it cannot name the verb, the credential
    to use instead, or the fact that the CLIENT is what needs changing. This rung
    has already caused that exact confusion once — see the REFUSE-DO-NOT-PROMPT
    note below, where a missing credential was reported as a broken verb. A
    server-side 403 on the onboarding path would reproduce it through a new door.
    """
    # ── #332: AN EXPLICIT ARGUMENT IS A DECISION, NOT A HINT ────────────────
    # >>> THIS BLOCK USED TO SIT BELOW THE $MESH_GATEWAY_TOKEN LOOKUP, so a
    # stale value in the environment silently overrode the credential the
    # operator NAMED on the command line. <<< It shipped in 0.41.6 and was
    # inert until the shared token's VALUE was ROTATED on 2026-08-05: before
    # that the env value and the file usually agreed, so the wrong order still
    # produced a working credential. Rotation made them disagree, and the
    # symptom was a 401 saying UNAUTHORIZED rather than "I ignored the file you
    # gave me".
    #
    # >>> BE PRECISE ABOUT WHAT ROTATION DID AND DID NOT DO, because the mesh
    # spent two days believing the shared credential was RETIRED and it is not
    # (gpt-ops caught this claim sitting in these very comments). Rotation
    # invalidated the OLD VALUE. The shared-token REGIME is current, required
    # and privileged: the gateway refuses to start without one configured, it is
    # the FIRST auth branch checked — before per-peer — and it resolves to ROOT
    # (unscoped DM reads, board authz bypass, peer=None so unattributable).
    #
    # EVIDENCE BOUNDARY, stated here because a claim in source outlives the
    # thread that produced it (gpt-ops, PR #187 review): the DM-firehose and
    # boot-precondition claims were confirmed by TWO independent readers. THE
    # BOARD-AUTHZ BYPASS WAS NOT — it is LAB-VERIFIED ONLY, read at
    # mesh-gateway/server.py:1626 and :1665 on the DEPLOYED gateway. The peer
    # who reviewed this change could not reverify it because its gateway fork
    # HAS NO BOARD LAYER. Treat it as one reader's finding, not consensus.
    #
    # WHICH IS WHY THIS ORDERING WAS A PRIVILEGE ESCALATION, NOT A RELIABILITY
    # BUG: preferring the ambient value over an explicit per-peer file meant
    # that whenever the environment held a CURRENT shared value, this verb ran
    # as ROOT instead of as the scoped peer the operator named — silently, and
    # reading green throughout. The 401 was the GOOD outcome; it appeared only
    # once the value went stale. See board cards #332 and #333. <<<
    #
    # Parsing goes through the one shared reader (swarph_cli.tokens) so that a
    # bare-token file and an env-style file both work behind the one flag.
    # Reordering alone would have broken raw-token users: onboard's own parser
    # only ever understood KEY=VALUE, so an explicit bare-token file would have
    # matched nothing and fallen through — turning a silent WRONG credential
    # into a silent MISSING one. gpt-ops caught that in review before it shipped.
    if token_file_arg:
        from swarph_cli.tokens import read_token_file

        explicit = Path(token_file_arg).expanduser()
        try:
            return read_token_file(explicit)
        except RuntimeError as exc:
            # NAME WHAT WAS NOT TRIED, AND WHY. The pre-#332 refusal listed all
            # four credential sources, which was right when this verb kept
            # searching. It no longer does — so a bare "cannot read file" would
            # drop the operator from a four-item map to a one-line dead end, and
            # a silent fallback would authenticate them as somebody else on a
            # typo. Say both: the cause, and the doors deliberately left shut.
            raise RuntimeError(
                f"{exc}\n"
                "  NO FALLBACK WAS ATTEMPTED. --token-file names a specific "
                "credential, and an explicit argument is a decision, not a hint.\n"
                "  NOT TRIED (in the order they would have been): "
                "$MESH_GATEWAY_TOKEN, ~/.swarph/secrets.toml, "
                "~/.config/swarph/<self>.peer_token\n"
                "  Fix the path, or omit --token-file to use those fallbacks."
            ) from exc

    # ── THE NAMED CELL'S OWN CREDENTIAL OUTRANKS THE AMBIENT ONE ────────────
    # >>> #332's PRINCIPLE, APPLIED TO THE OTHER WAY A CALLER NAMES SOMETHING.
    # <<< #332 established that an explicit --token-file must beat an ambient
    # value, because a value nobody named must not override one that was named.
    # SWARPH_SELF is the same kind of statement — it says WHICH CELL THIS IS —
    # so the credential that identity owns deserves the same standing. Reported
    # from workstation-lc against 0.41.6 as the general case; cards #332/#333.
    #
    # The stale-value symptom (401 while a usable peer token sits unread) is the
    # GOOD outcome, because it is visible. The dangerous one reads green: where
    # the ambient value is USABLE, this verb authenticated with it while the
    # operator believed it used the named cell's. And one process-global variable
    # cannot be the credential for more than one cell, so on a multi-cell host
    # naming a cell selected an IDENTITY WITHOUT CARRYING ITS CREDENTIAL —
    # workstation-lc runs three, with three pairwise-distinct token files.
    #
    # NOTE what is deliberately NOT claimed here. An earlier draft of this
    # comment said the ambient path "runs as ROOT" and the peer file is "scoped".
    # gpt-ops blocked that on #190: source category and file location do not
    # establish bearer class, measured five ways on 2026-08-06. This change
    # affects WHICH credential is selected, not what it is.
    #
    # Limited to "SWARPH_SELF is set", so an operator who names no cell is
    # unaffected and #243's additivity guarantee survives for that population.
    env_tok = os.environ.get("MESH_GATEWAY_TOKEN")
    self_name = os.environ.get("SWARPH_SELF", "").strip()
    # Set when a per-peer credential was found but is not usable for THIS target
    # (#467b). Distinguishes "you have the WRONG credential" from "you have NO
    # credential" at the refusal below — two different operator problems, and
    # conflating them is what made a missing credential read as a broken verb.
    peer_token_withheld = False
    if self_name:
        res = tokens.resolve_token(
            self_name,
            None,
            env_keys=("MESH_GATEWAY_TOKEN",),
            identity_is_explicit=True,
            warn=lambda m: print_safe(f"swarph onboard: {m}", file=sys.stderr),
        )
        if res is not None and res.source in ("peer-token", "legacy-peer-token"):
            # >>> #467b: A PER-PEER CREDENTIAL MAY REGISTER ITSELF AND NOTHING ELSE.
            # <<< WITHHOLD IT AND FALL THROUGH rather than returning or refusing
            # here. Falling through is the load-bearing choice: an operator token in
            # $MESH_GATEWAY_TOKEN (or secrets.toml) is BELOW this branch, so
            # refusing at this point would reject a caller who HAS the right
            # credential — and the refusal text advertises exactly that remedy.
            # A refusal naming a remedy it then blocks is worse than no refusal.
            if target_peer is not None and target_peer != self_name:
                peer_token_withheld = True
            else:
                return res.token

    if env_tok:
        return env_tok

    secrets_path = Path.home() / ".swarph" / "secrets.toml"
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
                        return val
        except Exception as exc:
            print_safe(
                f"swarph onboard: failed to read {secrets_path}: {exc}", file=sys.stderr
            )

    # ── #243: THE PER-PEER TOKEN — THE CREDENTIAL THAT ACTUALLY EXISTS ──────
    # >>> THIS VERB WAS LOOKING ONLY FOR THE CREDENTIAL THE R1 MIGRATION MOVED
    # OFF OF. <<< (This comment said "RETIRED" until 2026-08-06. It was wrong,
    # and it is where the mesh's two-day belief that the shared token was gone
    # came from — a claim in a comment, restated until it read as settled. The
    # shared credential is NOT retired: see the block above and card #333.)
    # Measured on lab-ovh 2026-08-03: $MESH_GATEWAY_TOKEN UNSET,
    # ~/.swarph/secrets.toml ABSENT, and 10+ files present at
    # ~/.config/swarph/<peer>.peer_token. Every other verb resolves the per-peer
    # file; onboard (and ratify, which re-exports this function, and daemon,
    # which copied it) were left on the old path — classic mint-vs-cutover, the
    # credential moved and three consumers did not.
    #
    # AND THE GATEWAY NEVER REQUIRED A CALLER TO PRESENT THE SHARED TOKEN:
    # measured, POST /peers/register with a PER-PEER token returns 200 and
    # mints. So this was never a permissions problem — only a lookup that never
    # learned.
    #
    # >>> THAT MEASUREMENT WAS CORRECT WHEN TAKEN AND IS NOW FALSE FOR THE CASE
    # THIS VERB ACTUALLY EXERCISES (#467b, 2026-08-18). <<< mesh-gateway e4a1f6a
    # binds POST /peers/register's `name` to the authenticated caller, so a
    # per-peer token still returns 200 — but ONLY when registering ITSELF. The
    # 2026-08-03 measurement happened to register the running cell's own name, so
    # it could not distinguish "the gateway accepts a per-peer token" from "the
    # gateway accepts a per-peer token FOR ITS OWN NAME". One measurement, two
    # questions, and the conclusion was written at the wider scope — the same
    # shape as the sentence corrected directly below. The cross-name case is now
    # refused CLIENT-side at rung 4; see _resolve_token's docstring.
    #
    # >>> THAT SENTENCE USED TO READ "THE GATEWAY NEVER REQUIRED THE SHARED
    # TOKEN", FULL STOP — AND IT IS A DIFFERENT CLAIM. The measurement answered
    # "what may a CLIENT present?"; the sentence generalised it to "what does
    # the SERVER require?". Those diverge: server.py refuses to start at all
    # without MESH_GATEWAY_TOKEN configured (`if not AUTH_TOKEN: raise 500`).
    # One sentence answering two questions, correct on the half that was
    # measured — which is exactly why nobody caught it for months. <<<
    #
    # Placed AFTER the existing two so a working operator-token setup is
    # unchanged; this only fills the hole where the verb used to prompt.
    self_name = os.environ.get("SWARPH_SELF", "").strip()
    if self_name:
        peer_tok = Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"
        if peer_tok.exists():
            # #467b, same rule as the branch above: withhold for a cross-name
            # target, do not return and do not raise. This rung sits BELOW the env
            # and secrets lookups, so reaching it already means no operator
            # credential was found — the specific refusal is raised at the bottom.
            if target_peer is not None and target_peer != self_name:
                peer_token_withheld = True
                peer_tok = None
        if peer_tok is not None and peer_tok.exists():
            try:
                val = peer_tok.read_text(encoding="utf-8").strip()
                if val:
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
    # >>> #467b: TWO DIFFERENT OPERATOR PROBLEMS, TWO DIFFERENT MESSAGES. <<< "You
    # hold the wrong credential for this target" and "you hold no credential" have
    # different remedies, and this verb has already cost the mesh a day by making
    # one look like the other (see REFUSE-DO-NOT-PROMPT above: a missing credential
    # was reported as a broken verb). Answering both with the generic list would
    # tell an operator holding a perfectly good per-peer token that nothing was
    # found — which is false, and points them at the wrong fix.
    if peer_token_withheld:
        raise RuntimeError(
            f"refusing to onboard {target_peer!r} with {self_name}'s own per-peer "
            f"token.\n\n"
            f"  found:  ~/.config/swarph/{self_name}.peer_token\n"
            f"  That credential authenticates as {self_name!r}, and the gateway binds\n"
            f"  POST /peers/register's `name` to the authenticated caller — so it can\n"
            f"  register {self_name!r} and no other peer. Presenting it would 403.\n\n"
            f"  ONBOARDING ANOTHER CELL IS AN OPERATOR ACTION. Use an operator "
            f"credential:\n"
            f"      swarph onboard {target_peer} --token-file <operator-token>\n"
            f"      MESH_GATEWAY_TOKEN=<operator-token> swarph onboard {target_peer}\n\n"
            f"  To onboard THIS cell, the per-peer token IS the right credential:\n"
            f"      swarph onboard {self_name}"
        )
    raise RuntimeError(
        "no gateway credential found. Tried, in order:\n"
        f"  1. --token-file            {'(not given)' if not token_file_arg else token_file_arg}\n"
        "  2. $MESH_GATEWAY_TOKEN     (unset)\n"
        f"  3. {secrets_path}\n"
        f"  4. ~/.config/swarph/<self>.peer_token  "
        + (f"(SWARPH_SELF={self_name!r} -> not found)" if self_name
           else "(SWARPH_SELF IS UNSET — set it; this verb will not guess a peer name)")
        + "\n  The per-peer token is sufficient TO ONBOARD THAT SAME CELL — the"
          "\n  gateway binds register's name to the caller, so it cannot onboard"
          "\n  another peer. For that, use an operator credential (1 or 2)."
        # >>> NAME THE ALTERNATIVE. `onboard` JOINS AN EXISTING MESH, so it needs
        # that mesh's URL and a token ITS operators issue — an outsider cannot
        # complete it, ever, without them. The command a newcomer actually wants
        # is `init`, and nothing told them: reconstructed 2026-08-05 in a clean
        # sandbox as the path our external paper reviewer would have hit.
        # A refusal that knows the remedy and does not say it is a wall with no
        # door.
        + "\n\n  NOTE: `swarph onboard` JOINS AN EXISTING MESH — it needs that\n"
          "  mesh's gateway URL and a token its operators issue for you.\n"
          "  To stand up YOUR OWN instead, no token required:\n"
          "      pip install 'swarph-cli[gateway]'\n"
          "      swarph gateway serve --port 8788 --db ~/.swarph/mesh.db\n"
          "      swarph init <name>        # scaffold a cell\n"
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


_UNKNOWN = "?"


def _probe_onboarding(peer: str, gateway: str) -> list:
    """Non-mutating probe of the onboarding ladder. Returns [(mark, label, detail)].

    >>> WRITTEN BECAUSE THE LADDER HAD NO STATUS SURFACE, ONLY A FORWARD RUN. <<<
    The 2026-08-18 fresh-eyes audit (board #464) found onboarding is a CLOSED LOOP
    WITH NO ENTRANCE: ratification needs a handshake DM, sending needs your own
    peer token, and the only pointer to getting one is a file that is not shipped.
    A fresh cell therefore hit `403 caller-binding`, exit 1, with no way to see
    which rung it was on or who could unblock it.

    Every rung reports one of: ok / MISSING / '?'. >>> '?' IS A FIRST-CLASS RESULT
    AND MUST NEVER BE RENDERED AS ok. <<< A probe that cannot see a rung says so;
    the whole failure family this session catalogued (#458, #459, #463) is a check
    reporting silence as success.
    """
    import urllib.error
    import urllib.request

    rows = []
    base = gateway.rstrip("/")

    # 1. gateway reachable at all — everything below is meaningless otherwise
    try:
        urllib.request.urlopen(base + "/peers", timeout=8)
        reachable = True
        rows.append(("ok", "gateway reachable", base))
    except urllib.error.HTTPError:
        reachable = True   # answered, just refused us — that IS reachable
        rows.append(("ok", "gateway reachable", base + " (auth required)"))
    except Exception as exc:
        reachable = False
        rows.append(("MISSING", "gateway reachable",
                     f"{base} -> {type(exc).__name__}. Fix this first; every rung "
                     f"below is unknowable until it answers."))

    # 2. own peer token on disk
    tok_path = pathlib.Path.home() / ".config" / "swarph" / f"{peer}.peer_token"
    token = None
    if tok_path.exists():
        token = tok_path.read_text(encoding="utf-8").strip()
        mode = oct(tok_path.stat().st_mode & 0o777)
        if not token:
            # >>> F1 (drop, PR #247 review). EXISTENCE IS NOT CONTENT. <<< This rung
            # tested exists() and the mode and never that the file HELD a credential,
            # so a 0-byte 0600 file read as a clean [x] -- and then rung 3 printed
            # "not attempted (no token on disk)" one line below it. The same checklist
            # asserted the token was present AND absent. A fresh cell, the entire
            # audience for this verb, reads ok and looks elsewhere.
            token = None
            rows.append(("MISSING", "peer token on disk",
                         f"{tok_path} present but EMPTY -- a mint that was never written, "
                         f"or a truncated copy. Treat as no token."))
        else:
            rows.append(("ok" if mode == "0o600" else "MISSING",
                         "peer token on disk",
                         f"{tok_path} (mode {mode}" + ("" if mode == "0o600" else "; expected 0600") + ")"))
    else:
        rows.append(("MISSING", "peer token on disk",
                     f"{tok_path} absent. THIS IS THE RUNG THAT CANNOT BE SELF-SERVED: "
                     f"a token is minted by POST /peers/register and returned ONCE. "
                     f"RECOVERY, if the peer is already registered: re-registering returns "
                     f"HTTP 200 with token_status=existing and NO token -- it looks like "
                     f"success and changes nothing. An operator must DELETE /peers/{peer} "
                     f"first, which purges the old token and writes a revocation row; the "
                     f"next register then mints a fresh generation. Deliver it OUT OF BAND "
                     f"-- never over the mesh, where message content is retained."))

    # 3. does that token actually IDENTIFY as this peer?
    #
    # >>> WAS: "reads its own inbox -> 200". THAT WAS A PROXY AND IT COST A FALSE
    # RATIFICATION. <<< Peers may read ALL mesh DMs (commander ruling 2026-08-05), so
    # ANY valid token returns 200 on /messages?to_node=<anyone>. The check could not
    # fail for the reason it appeared to pass. lab ratified meta-muse on exactly that
    # evidence at 04:32 and retracted it 40 minutes later: the file named
    # meta-muse.peer_token authenticates as META-MUSE-2.
    #
    # GET /whoami is the discriminating route, found by drop-on-meta-edge: it returns
    # the bound peer name, so it ANSWERS DIFFERENTLY FOR A DIFFERENT PRINCIPAL. The
    # operational test drop sharpened out of this: run the same call with a second
    # token and see whether the answer moves; if it does not move, it was never about
    # identity. Read-only, zero blast radius -- lab had previously reached for DELETE
    # to learn this, which the harness classifier correctly blocked.
    if token and reachable:
        req = urllib.request.Request(f"{base}/whoami",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as _r:
                who = json.loads(_r.read().decode())
            bound = who.get("peer")
            if bound == peer:
                rows.append(("ok", "token identifies as this peer",
                             f"/whoami -> peer={bound!r} regime={who.get('regime')} "
                             f"gen={who.get('key_generation')}"))
            else:
                rows.append(("MISSING", "token identifies as this peer",
                             f"FILENAME/BINDING MISMATCH: this file authenticates as "
                             f"{bound!r}, NOT {peer!r}. Every by-name lookup in the tree "
                             f"resolves the wrong credential. Do not ratify on this."))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # The gateway ANSWERED the identity call with a refusal — that is
                # a claim about the token, and the DEAD reading is earned.
                rows.append(("MISSING", "token identifies as this peer",
                             f"HTTP {e.code}. A token that exists but is refused is DEAD, not "
                             f"missing -- mint-once means re-registering returns 200 with a "
                             f"null token and changes nothing. Recovery needs a deregister "
                             f"first, which is an operator action."))
            else:
                # >>> 404 IS ROUTE-ABSENT, NOT TOKEN-DEAD. <<< Demonstrated
                # 2026-08-21 on a scratch fork-gateway: the fork does not serve
                # /whoami (a deployed-gateway route), so this branch fired on
                # EVERY fresh cell with "token is DEAD ... deregister to
                # recover" — a destructive remedy prescribed for a token minted
                # seconds earlier. A route the gateway does not serve (404) or
                # a gateway that fails mid-call (5xx) means THE PROBE CANNOT
                # SEE this rung, which is the exact state '?' exists for. The
                # doctrine two rungs up applies here too: a probe that cannot
                # see says so; it does not cry DEAD and prescribe surgery.
                rows.append((_UNKNOWN, "token identifies as this peer",
                             f"HTTP {e.code} on /whoami -- this gateway does not serve the "
                             f"identity route (or failed answering it), so the rung is "
                             f"UNDETERMINED. This says NOTHING about the token, which may "
                             f"be perfectly healthy -- token recovery is not indicated by "
                             f"this result. /whoami shipped on the reference gateway; a "
                             f"fork deployment predating it cannot be probed this way."))
        except Exception as e:
            rows.append((_UNKNOWN, "token identifies as this peer",
                         f"could not determine: {type(e).__name__}"))
    else:
        rows.append((_UNKNOWN, "token identifies as this peer",
                     "not attempted (no token on disk)" if not token else "not attempted (gateway down)"))

    # 4/5/6. registry facts, if we can read the registry at all
    peer_row = None
    registry_read = False          # F3: did the READ succeed, regardless of the result?
    if reachable and token:
        req = urllib.request.Request(base + "/peers", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            allp = data if isinstance(data, list) else data.get("peers", [])
            registry_read = True
            peer_row = next((x for x in allp if x.get("name") == peer), None)
        except Exception:
            peer_row = None

    if peer_row is None:
        if registry_read:
            # >>> F3 (drop). peer_row=None HAD TWO CAUSES AND ONE MESSAGE. <<< The read
            # SUCCEEDING and the peer being absent is the single most likely fresh-cell
            # state, and reporting it as "could not read the registry" points at
            # gateway/permissions when the truth is "you are not registered yet" -- the
            # exact rung this whole ladder exists to surface.
            rows.append(("MISSING", "registered",
                         f"gateway answered and {peer} is NOT in the registry. Run "
                         f"`swarph mesh register --as {peer}` (or have an operator do it)."))
            detail = "not attempted (peer is not registered)"
        else:
            rows.append((_UNKNOWN, "registered",
                         "could not read the registry" if token else "not attempted"))
            detail = "could not read the registry" if token else "not attempted"
        rows.append((_UNKNOWN, "ratified", detail))
        rows.append((_UNKNOWN, "capabilities declared", detail))
        # >>> F2 (drop). THIS RUNG USED TO VANISH. <<< It was appended only when
        # peer_row was truthy, so an unreadable registry silently produced a SIX-row
        # checklist. An absent row is worse than a '?': a '?' cannot be missed, but the
        # reader cannot count what was never printed, and a fresh cell never learns the
        # rung exists. Every rung must appear in every branch.
        rows.append((_UNKNOWN, "health check has succeeded", detail))
    else:
        rows.append(("ok", "registered", f"registered_at {str(peer_row.get('registered_at'))[:19]}"))
        rows.append(("ok" if peer_row.get("ratified") else "MISSING", "ratified",
                     f"by {peer_row.get('ratified_by')}" if peer_row.get("ratified")
                     else "a witness must run `swarph ratify <peer>`. Unratified cells "
                          "still operate -- this gates trust, not function."))
        caps = peer_row.get("capabilities") or {}
        extra = {k: v for k, v in caps.items() if k != "can_claim_tasks"}
        rows.append(("ok" if extra else "MISSING", "capabilities declared",
                     f"{sorted(caps)}" if extra else
                     "only can_claim_tasks. NOTHING CAN BE ROUTED TO YOU BY MODEL OR "
                     "PROVIDER until you re-register with a real roster -- an "
                     "undeclared capability is invisible capacity."))

    # 7. health — the field that separates 'up' from 'reachable'
    if peer_row is not None:
        url = str(peer_row.get("url") or "")
        if peer_row.get("last_health"):
            rows.append(("ok", "health check has succeeded", str(peer_row.get("last_health"))[:19]))
        elif url.startswith("outbound-only://"):
            # >>> N/A IS NOT A GAP, AND THE DISTINCTION IS THE POINT. <<< A cell with
            # no inbound listener can NEVER close this rung. Marking it MISSING mints a
            # permanent red mark, and a checklist that everyone permanently fails is one
            # nobody reads -- openwolf's 2.0.3 lesson (false-positive warnings train
            # users to ignore the system) applied to us before we ship it. Raised by
            # cursor-lin, which is outbound-only and would have carried this forever.
            rows.append(("ok", "health check has succeeded",
                         "N/A -- url is outbound-only://, so this cell has no inbound "
                         "endpoint to health-check. Not a gap; liveness for this cell is "
                         "its own polling, not an inbound probe."))
        else:
            rows.append(("MISSING", "health check has succeeded",
                         "last_health is null -- no health check has EVER succeeded. "
                         "last_seen is not a substitute: it stays fresh while a cell is "
                         "deaf. If this cell has no inbound listener, register it with "
                         "url=outbound-only://<name> so this rung reads N/A rather than "
                         "failing forever."))
    return rows


def _print_checklist(peer: str, gateway: str) -> int:
    rows = _probe_onboarding(peer, gateway)
    print_safe(f"onboarding checklist for {peer} @ {gateway}\n")
    width = max(len(lbl) for _, lbl, _ in rows)
    for mark, label, detail in rows:
        sym = {"ok": "[x]", "MISSING": "[ ]"}.get(mark, "[?]")
        print_safe(f"  {sym} {label.ljust(width)}  {detail}")
    gaps = [l for m, l, _ in rows if m == "MISSING"]
    unknown = [l for m, l, _ in rows if m == _UNKNOWN]
    print_safe("")
    if gaps:
        print_safe(f"  {len(gaps)} gap(s): {', '.join(gaps)}")
    if unknown:
        print_safe(f"  {len(unknown)} UNDETERMINED: {', '.join(unknown)} "
                   f"-- undetermined is NOT ok; it means the probe could not see.")
    if not gaps and not unknown:
        print_safe("  fully onboarded.")
    return 0


def run_onboard(argv: list[str]) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch.

    Returns process exit code: 0 on success, 1 on validation fail,
    2 on gateway error."""
    args = _build_parser().parse_args(argv)

    if getattr(args, "check", False):
        return _print_checklist(args.peer, args.gateway)

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
    # target_peer=canonical: rung 4 must know WHICH peer is being registered, so a
    # cell's own per-peer token is refused for onboarding anyone but itself (#467b).
    token = _resolve_token(args.token_file, target_peer=canonical)
    if not token:
        print_safe("swarph onboard: empty token", file=sys.stderr)
        return 1
    print_safe("      ok")

    # ── Step 4: POST /peers/register ─────────────────────────────────
    peer_url = args.url or f"http://{canonical}:8787"
    print_safe(f"[4/6] POST {args.gateway}/peers/register")
    status, body = _post_json(
        f"{args.gateway}/peers/register",
        {"name": canonical, "url": peer_url, "capabilities": capabilities},
        token,
    )
    if status != 200:
        # >>> #467b RUNG 3 — RE-RENDER THE BINDING 403 AT THE CALL SITE. <<<
        # The resolver's refusal (above) catches the common shape, but it CANNOT
        # catch every one: a raw token string does not self-identify as per-peer,
        # so when $MESH_GATEWAY_TOKEN holds a PER-PEER value — cards #332/#333's
        # exact misconfiguration, described sixty lines above the resolver — the
        # target check has nothing to test and the caller sails past it.
        #
        # drop-on-meta-edge's point (#24311): a target check cannot fix that, but
        # catching the 403 HERE covers every bypass path at once (--token-file,
        # env, secrets.toml), needs no knowledge of the token's regime, and makes
        # the friendly message UNCONDITIONAL rather than resolver-path-dependent.
        #
        # The bare gateway text names a binding rule and nothing else — not the
        # verb, not which credential to use, not that the CLIENT is what needs
        # changing. #468's house rule, applied client-side: a refusal must carry
        # its escape.
        if status == 403 and "caller-binding" in str(body):
            print_safe(
                f"swarph onboard: the gateway refused this credential for "
                f"{canonical!r}.\n\n"
                f"  {body}\n\n"
                f"  The gateway binds POST /peers/register's `name` to the "
                f"AUTHENTICATED caller,\n"
                f"  so the token presented can register only the peer it "
                f"authenticates as.\n"
                f"  Onboarding another cell is an OPERATOR action.\n\n"
                f"  Use an operator credential:\n"
                f"      swarph onboard {canonical} --token-file <operator-token>\n\n"
                f"  If $MESH_GATEWAY_TOKEN is set, CHECK ITS VALUE — a per-peer "
                f"token stored\n"
                f"  under that name authenticates as ONE cell and is the usual "
                f"cause here\n"
                f"  (cards #332/#333). To see which peer a token actually is:\n"
                f"      curl -sH \"Authorization: Bearer <token>\" "
                f"{args.gateway}/whoami\n",
                file=sys.stderr,
            )
            return 2
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
    elif body.get("token_status") == "existing":
        # >>> A RE-REGISTER IS NOT A MINT, AND THE LINE MUST SAY SO. <<<
        # Demonstrated 2026-08-21 (journey walkthrough): the cell-first order —
        # cell self-registers and captures its token, THEN the operator onboards —
        # lands here, and the old output printed the fresh-mint line
        # "ok (registered_unratified=true)" for a call that minted NOTHING. An
        # operator reading that believes a once-only token was just delivered to
        # somebody and goes looking for it. It does not exist. Success-shaped
        # silence about "nothing changed" is the failure family this verb's own
        # checklist (#464) was built to kill.
        print_safe(
            f"      ok (already registered — token_status=existing, NO token "
            f"minted on this call; the once-only token was delivered at first "
            f"registration)"
        )
    else:
        print_safe(f"      ok (registered_unratified=true)")

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
