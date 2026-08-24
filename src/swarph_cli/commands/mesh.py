"""``swarph mesh`` — provider-agnostic mesh DM tools and the monitor engine.

This module also hosts the DELIVERY ENGINE behind ``swarph monitor`` (card #122).
It lives here, next to the gateway primitives it drives (``_http_get_json``,
``_tmux_wake``, ``_log_dm``), for two reasons:

  1. ``swarph mesh sidecar`` is the deprecated alias for ``swarph monitor start
     --deliver tmux:<target>``, and there must be exactly ONE implementation
     underneath. A second copy is how the deployed :8788 gateway and the CLI's
     forked gateway drifted ~1800 lines apart; we are not repeating that.
  2. The sidecar's regression suites monkeypatch ``mesh._tmux_wake`` /
     ``mesh._http_get_json`` / ``mesh._log_dm``. Those patches only bite if the
     engine's call sites resolve through THIS module's namespace.

``src/swarph_cli/commands/monitor.py`` is the CLI surface (start/status/stop) and
imports from here; the import is one-directional on purpose.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse

import swarph_cli
import urllib.request
from collections import deque
from collections.abc import MutableMapping
from pathlib import Path
from typing import Optional

from .. import tokens
from ._content import ContentError, add_content_args, resolve_content
from ._display import sanitize_terminal


def _format_inbox_line(dm: dict) -> str:
    """One inbox line, terminal-safe: peer-authored content/from_node/kind are
    sanitized so a hostile DM can't inject terminal escapes on display (0.29.2)."""
    read = "read" if dm.get("read_at") else "unread"
    content = sanitize_terminal((dm.get("content") or "").replace("\n", " "))[:160]
    who = sanitize_terminal(dm.get("from_node"))
    kind = sanitize_terminal(dm.get("kind"))
    return f"id={dm.get('id')} {read} from={who} kind={kind} {content}"


# TAILNET IP, NOT localhost and NOT a MagicDNS name (commander, 2026-08-21).
# The mesh-gateway binds HOST=100.107.222.72 ONLY — localhost has never been bound, so this
# default failed as a bare "Connection refused" with no cause named. It cost 792 silent
# card-export failures over 8 days, and it turned a WORKING hand-started monitor into a
# DEAF SUPERVISED one the moment it was moved to a systemd unit (the unit passes no
# --gateway, so it fell through to this constant).
# An IP over MagicDNS on purpose: a name needs MagicDNS enabled, the right search domain,
# and no local collision; the IP needs only that tailscale is up, which is the real
# precondition anyway. MESH_GATEWAY_URL overrides it — that env var is the escape hatch
# for anyone outside this mesh, and optional inside it.
_DEFAULT_GATEWAY = os.environ.get("MESH_GATEWAY_URL", "http://100.107.222.72:8788")
_DEFAULT_POLL_S = 30
_BACKOFF_EMPTY_THRESHOLD = 5
_BACKOFF_EMPTY_SECONDS = 60
_BACKOFF_5XX_THRESHOLD_SECONDS = 300
_BACKOFF_5XX_SECONDS = 300
_DEFAULT_WAKE_MIN_INTERVAL_S = 60


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph mesh",
        description="Provider-agnostic mesh DM commands.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="send a mesh DM")
    send.add_argument("to", help="recipient peer name")
    send.add_argument("--kind", required=True, help="message kind")
    add_content_args(send)
    _add_common(send)

    reply = sub.add_parser(
        "reply", help="reply to a DM IN ITS OWN THREAD (does NOT close the "
                      "obligation — the close act is `board obligations close`, #562)")
    reply.add_argument("message_id", type=int, help="the DM being replied to")
    reply.add_argument("--kind", default="answer",
                       help="message kind (default: answer). NOT gated — a reply is "
                            "universal across every DM kind.")
    reply.add_argument("--search-limit", type=int, default=200,
                       help="how far back in this inbox to look for the message")
    reply.add_argument("--json", action="store_true",
                       help="machine-readable result. Most callers here are automated "
                            "and both outcomes exit 0, so stdout prose is not a signal. "
                            "READ obligation_check, NOT closed_obligation: the latter is "
                            "a legacy projection of the first closed id and its null "
                            "means cannot-tell OR none OR never-looked. "
                            "obligation_check tells them apart (null = this gateway does "
                            "not report closures at all).")
    add_content_args(reply)
    _add_common(reply)

    inbox = sub.add_parser("inbox", help="read this peer's mesh inbox")
    inbox.add_argument("--unread", action="store_true", help="only unread DMs")
    inbox.add_argument("--limit", type=int, default=20, help="max messages")
    inbox.add_argument("--json", action="store_true", help="print raw JSON")
    inbox.add_argument(
        "--consume",
        action="store_true",
        help="allow the destructive read when the identity was NOT named with --as. "
             "Without it, an ambiently-resolved identity is refused rather than "
             "silently consuming somebody else's unread state.",
    )
    inbox.add_argument(
        "--peek",
        action="store_true",
        help="show the inbox WITHOUT marking anything read (default: reading CONSUMES -- "
             "it marks every DM shown as read for the resolved identity)",
    )
    _add_common(inbox)

    register = sub.add_parser("register", help="self-register this peer")
    register.add_argument(
        "--url",
        default=None,
        help="peer service URL (default: http://<self>:8787)",
    )
    register.add_argument(
        "--capability",
        action="append",
        default=[],
        help="capability as KEY=VALUE; VALUE parsed as JSON when possible",
    )
    register.add_argument(
        "--force",
        action="store_true",
        help="allow register when a local per-peer token file already exists",
    )
    register.add_argument(
        "--replace",
        action="store_true",
        help="REPLACE the advertised capability blob with exactly the "
             "--capability keys given (sends full=true). Default is MERGE: "
             "the peer's currently-registered keys are read first and "
             "re-submitted, so updating one field does not destroy the rest "
             "(#124).",
    )
    _add_common(register)

    peers = sub.add_parser(
        "peers",
        help="list registered peers with their reported swarph_cli_version; "
             "--stale-than X names only peers REPORTING older than X, and "
             "names the UNREPORTED separately (#535)",
    )
    peers.add_argument(
        "--stale-than", default=None, metavar="VERSION",
        help="list only peers whose reported swarph_cli_version is older "
             "than VERSION; peers with no reported version are named "
             "UNREPORTED, never counted as current",
    )
    peers.add_argument(
        "--as", dest="self_name", default=None,
        help="optional peer identity. IF OMITTED the ambient shared token "
             "(MESH_GATEWAY_TOKEN) is used — a registry read does not require "
             "a self identity. Pass --as to use a stored per-peer token instead.",
    )
    peers.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", _DEFAULT_GATEWAY),
        help="mesh-gateway base URL",
    )
    peers.add_argument("--token-file", default=None, help="explicit bearer token file")

    sidecar = sub.add_parser("sidecar", help="poll inbox and wake a tmux cell")
    sidecar.add_argument("--tmux-target", default=None, help="tmux target pane")
    sidecar.add_argument("--state-dir", default=None, help="state directory")
    sidecar.add_argument("--poll-seconds", type=int, default=_DEFAULT_POLL_S)
    sidecar.add_argument(
        "--wake-min-interval",
        type=int,
        default=_DEFAULT_WAKE_MIN_INTERVAL_S,
        help="minimum seconds between tmux wake prompts",
    )
    sidecar.add_argument("--once", action="store_true", help="poll once and exit")
    _add_common(sidecar)

    return p


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--as", dest="self_name", default=None,
        help="sender/self peer. IF OMITTED THIS FALLS BACK TO $SWARPH_SELF -- it does "
             "not fail. On a box where several cells share one environment that is "
             "the box owner's identity, so pass --as explicitly when you are not it.",
    )
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", _DEFAULT_GATEWAY),
        help="mesh-gateway base URL",
    )
    p.add_argument("--token-file", default=None, help="explicit bearer token file")


def _peer_token_path(self_name: str) -> Path:
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


# _TOKEN_KEYS moved to swarph_cli.tokens.TOKEN_KEYS (#332). Not re-exported:
# a second copy of a constant in a second module is precisely how the two token
# PARSERS diverged in the first place. One definition, one address.


def _read_token_file(path: Path) -> str:
    """Read a bearer token from a token file OR an env-style file.

    THE IMPLEMENTATION MOVED to `swarph_cli.tokens.read_token_file` (#332).
    droplet's "one parser behind one flag" work (2026-07-26) lived here, in a
    COMMAND module \u2014 so every other verb either reimplemented it or reached
    into a sibling command's private helper. gpt-ops flagged that during the
    #332 review: `onboard` must not depend on `mesh`'s internals. The parser is
    byte-for-byte unchanged; only its address is.

    This wrapper stays because it is a PATCH POINT: existing suites monkeypatch
    `mesh._read_token_file`, and deleting the name would make those patches
    silently miss (the same trap documented on `_tmux_wake` above).
    """
    from swarph_cli.tokens import read_token_file

    return read_token_file(path)


def _read_secrets_token(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MESH_GATEWAY_TOKEN":
            return value.strip().strip('"').strip("'")
    return ""


def _resolve_self_name(
    arg: Optional[str],
    *,
    state_dir: Optional[Path] = None,
) -> str:
    if arg:
        return arg
    env = os.environ.get("SWARPH_SELF")
    if env:
        return env
    if state_dir is not None:
        return state_dir.name
    raise RuntimeError("cannot resolve self identity; pass --as or set SWARPH_SELF")


def _resolve_self_with_source(arg: Optional[str]) -> tuple:
    """(name, source) — WHERE the identity came from, not just what it is.

    >>> A COMMAND THAT ACTS AS SOMEBODY MUST SAY WHO. <<< $SWARPH_SELF is set
    machine-wide on a co-resident box, so a cell that forgets --as silently
    inherits the box owner's identity. It is not an authz hole -- the token really
    is that peer's -- which is precisely why nothing alerts. The 2026-08-18
    fresh-eyes onboarding audit hit this on its FIRST command: a brand-new cell ran
    `swarph mesh inbox` and was shown 20 unread DMs belonging to four other peers.
    Reading consumes, so an unlucky first command marks another peer's queue read,
    silently, exit 0. Same family as board #360 (identity fails TOWARD lab-ovh).
    """
    if arg:
        return arg, "--as"
    env = os.environ.get("SWARPH_SELF")
    if env:
        return env, "$SWARPH_SELF"
    raise RuntimeError("cannot resolve self identity; pass --as or set SWARPH_SELF")


def _resolve_token(
    self_name: str,
    token_file_arg: Optional[str],
    *,
    allow_peer_token: bool = True,
    identity_is_explicit: bool = True,
) -> str:
    """Delegates to swarph_cli.tokens.resolve_token — see that module for why
    three separate resolvers was the defect, not three separate bugs.

    `identity_is_explicit` defaults True here because every mesh verb reaches
    this through _resolve_self_name, which either took --as, took SWARPH_SELF,
    or derived the name from the state dir the operator pointed at. In all three
    the caller has said which cell it is, so that cell's own credential outranks
    an ambient MESH_GATEWAY_TOKEN — which is the whole fix.

    Parsing goes through tokens.read_token_file — the same strict reader #332
    extracted, which validates latin-1 encodability and names the offending file
    and line. mesh._read_token_file remains as a delegating shim because the
    sidecar suites monkeypatch that name.
    """
    res = tokens.resolve_token(
        self_name,
        token_file_arg,
        env_keys=("MESH_GATEWAY_TOKEN",),
        identity_is_explicit=identity_is_explicit,
        allow_peer_token=allow_peer_token,
        secrets_path=Path.home() / ".swarph" / "secrets.toml",
    )
    if res is not None:
        return res.token
    raise RuntimeError(
        "cannot resolve mesh token; set MESH_GATEWAY_TOKEN or create "
        f"{_peer_token_path(self_name)}"
    )


def _post_json(
    url: str,
    body: dict,
    token: str,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def _http_get_json(
    url: str,
    token: str,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def _parse_capability(spec: str) -> tuple[str, object]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"capability {spec!r} not KEY=VALUE shape")
    key, value = spec.split("=", 1)
    try:
        return key.strip(), json.loads(value)
    except json.JSONDecodeError:
        return key.strip(), value


def _write_secret_file_mode_600(path: Path, value: str) -> None:
    # Home moved to swarph_cli.tokens.write_secret_file (#564-C) — a helper
    # in a COMMAND module is an import-closure trap (the daemon's AST walk
    # reaches function-body imports). Alias kept so call sites stay put.
    from swarph_cli.tokens import write_secret_file
    write_secret_file(path, value)



def _near_names(to: str, names: list) -> list:
    """Candidate peer names for a refused recipient. SEVERAL, deliberately.

    >>> A CONFIDENT SINGLE SUGGESTION CAN ROUTE MAIL TO THE WRONG CELL. <<< Measured
    against the real registry, `drop` scores 0.73 against `droplet` and 0.38 against
    `drop-on-meta-edge` — so plain difflib ranks a REAL, DIFFERENT PEER first while the
    intended one falls below any sane cutoff. `drop` had 17 undelivered messages on
    2026-08-18 and every one was meant for drop-on-meta-edge.

    So: union prefix matches, tail-token matches, and fuzzy matches, and show up to
    three WITHOUT ranking one as the answer. Being handed three names forces a choice;
    being handed one invites a reflex.

    Cutoff is 0.5, not difflib's 0.6 default, because the case that cost three hours —
    `ws-lc` vs `workstation-lc` — scores 0.53.
    """
    lowered = to.lower()
    tail = lowered.rsplit("-", 1)[-1]
    out = []
    for n in names:
        nl = n.lower()
        if nl.startswith(lowered) or lowered.startswith(nl.split("-", 1)[0]):
            out.append(n)                      # prefix either way: drop -> BOTH droplets
        elif tail and len(tail) > 1 and nl.endswith("-" + tail):
            out.append(n)                      # ws-LC -> workstation-LC
    for n in difflib.get_close_matches(to, names, n=3, cutoff=0.5):
        if n not in out:
            out.append(n)
    return out[:3]


def _check_recipient(to: str, gateway: str, token: str) -> str | None:
    """Refuse a send to a peer the LIVE registry does not contain.

    >>> THE LIST MUST BE LIVE, NOT CACHED. <<< A stale local copy refuses a peer that
    registered an hour ago, which turns a typo-guard into an outage for exactly the
    newest cells. The commander's wording when he asked for this: "compare it to the
    live peers list".

    Returns an error message to print, or None to proceed.

    WHY THIS EXISTS: on 2026-08-18 the mesh held 63 undelivered messages across 14
    recipient names that are not peers. The gateway accepts any string as `to_node`,
    stores it, and returns 200. Top two were `drop` (17) and `lab` (16) — ROLE names,
    where the mesh names are drop-on-meta-edge and lab-ovh. Nobody's client said a word.

    >>> IF THE REGISTRY CANNOT BE READ, PROCEED — DO NOT BLOCK. <<< A validator that
    fails closed on its OWN failure converts every gateway hiccup into total messaging
    loss, which is far worse than the bug it prevents. Warn, and send. This is the
    CANNOT-EVALUATE branch and it is deliberately the permissive one, unlike most in
    this codebase, because the cost asymmetry runs the other way.
    """
    status, payload = _http_get_json(f"{gateway.rstrip('/')}/peers", token)

    # >>> EVERY UNEXPECTED SHAPE IS A CANNOT-EVALUATE, NOT AN EXCEPTION. <<< (Copilot,
    # reviewing #263.) The first version guarded the STATUS and the EMPTY case but
    # assumed the BODY was a dict of dicts. A 2xx carrying a bare list or {detail: ...}
    # would raise inside payload.get()/p.get() and propagate — BLOCKING THE SEND, which
    # is the exact opposite of this function's stated contract and of the two tests
    # written to pin it. A guard that fails closed on its own confusion is the outage it
    # was meant to prevent.
    #
    # Same isinstance ladder monitor._verify_self_is_registered already uses
    # (monitor.py:149-156) — reused rather than reinvented.
    def _unchecked(why: str):
        print(f"swarph mesh send: {why} — sending WITHOUT a recipient check",
              file=sys.stderr)
        return None

    if status < 200 or status >= 300:
        return _unchecked(f"could not read the peer registry "
                          f"({status or 'unreachable'})")
    if not isinstance(payload, dict):
        return _unchecked("unexpected /peers shape (not an object)")
    peers = payload.get("peers")
    if not isinstance(peers, list):
        return _unchecked("unexpected /peers shape (no peer list)")
    names = [p.get("name") for p in peers
             if isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("name")]
    if not names:
        return _unchecked("peer registry returned no names")
    # Case-insensitive (droplet, reviewing #263). The registry is the authority on the
    # EXACT spelling, so a capitalised variant of a real peer is still that peer — it
    # would otherwise fall through to the suggestion path and be REFUSED, when
    # _near_names lowercases for its own comparison anyway and would have suggested the
    # very name the caller already typed. An unnecessary refusal is a small failure, but
    # it is a failure of THIS function's only job.
    if to in names or to.lower() in {n.lower() for n in names}:
        return None

    near = _near_names(to, names)
    msg = (f"swarph mesh send: REFUSED — {to!r} is not a registered peer.\n"
           f"  The gateway would accept it, store it, and return 200. Nobody would "
           f"read it.")
    if near:
        # SUGGEST, NEVER SUBSTITUTE. A mesh that silently reroutes a message to
        # whichever name it guessed is a much worse failure than refusing one.
        #
        # SORTED, ONE PER LINE (Copilot, #263): a single line in _near_names' own order
        # reads as a ranked answer with a best guess at the front. These are OPTIONS —
        # `drop` legitimately means either droplet or drop-on-meta-edge, and the whole
        # reason this returns several is that difflib ranks the WRONG one first.
        msg += "\n  Did you mean:\n" + "\n".join(f"    {n}" for n in sorted(near))
    return msg


def _run_send(args: argparse.Namespace) -> int:
    try:
        content = resolve_content(args.content, getattr(args, "content_file", None))
    except ContentError as exc:
        print(f"swarph mesh send: {exc}", file=sys.stderr)
        return 1
    self_name = _resolve_self_name(args.self_name)
    token = _resolve_token(self_name, args.token_file)
    body = {
        "from_node": self_name,
        "to_node": args.to,
        "kind": args.kind,
        "content": content,
    }
    refusal = _check_recipient(args.to, args.gateway, token)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1

    status, payload = _post_json(
        f"{args.gateway.rstrip('/')}/messages",
        body,
        token,
    )
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh send: gateway {status}: {detail}", file=sys.stderr)
        return 1
    print(
        f"sent id={payload.get('id')} from={payload.get('from_node')} "
        f"to={payload.get('to_node')} kind={payload.get('kind')}"
    )
    return 0


def _find_inbox_message(gateway: str, token: str, self_name: str,
                        message_id: int, limit: int):
    """The DM being replied to, or (None, reason). Never raises, never guesses.

    >>> THERE IS NO GET /messages/{id}. <<< The gateway exposes a filtered LIST and a
    per-message read-receipt, so a reply has to find its target by scanning this
    peer's own inbox. That is a BOUNDED search, and the bound is the interesting
    part: a message older than `limit` is INDISTINGUISHABLE from one that never
    existed unless the refusal says which was searched. "not found" alone would send
    the operator hunting for a delivery bug that is really a paging window.
    """
    st, d = _http_get_json(
        f"{gateway.rstrip('/')}/messages?to_node={self_name}&limit={int(limit)}", token)
    if not (st and 200 <= st < 300):
        return None, f"gateway {st or 'unreachable'}: {d.get('detail', d) if isinstance(d, dict) else d}"
    msgs = d if isinstance(d, list) else d.get("messages", [])
    for m in msgs:
        if int(m.get("id", -1)) == int(message_id):
            return m, None
    return None, (
        f"message {message_id} is not in {self_name}'s inbox "
        f"(searched the most recent {len(msgs)} of --search-limit {limit}). "
        f"It may be older than that window, or addressed to a different peer — "
        f"those are different problems and this cannot tell them apart."
    )


def _run_reply(args: argparse.Namespace) -> int:
    """Reply to a DM IN ITS THREAD. Since #562 this does NOT close the
    obligation — closing is the distinct act `board obligations close`
    (POST /board/obligations/{id}/close); the reply path only REMINDS via
    the open_obligations field.

    >>> A REPLY IS UNIVERSAL ACROSS KINDS, DELIBERATELY. <<< The spec's constraint:
    not gated by kind. Gating would mean a peer could owe you an answer on a DM whose
    kind nobody thought to allow, and the obligation would be unanswerable through
    the product — the exact "waiting on a seat" shape #307 exists to kill.

    >>> AND A THREADLESS REPLY MUST NOT REPORT THE SAME LINE AS A THREADED ONE. <<<
    Most DMs carry no thread. Refusing them would make this verb useless for the
    common case; sending them silently would let an operator believe they had closed
    an obligation when nothing could have closed. So both are SENT and the two
    outcomes PRINT DIFFERENTLY. One message for two causes hides which one happened.
    """
    try:
        content = resolve_content(args.content, getattr(args, "content_file", None))
    except ContentError as exc:
        print(f"swarph mesh reply: {exc}", file=sys.stderr)
        return 1
    self_name = _resolve_self_name(args.self_name)
    token = _resolve_token(self_name, args.token_file)
    gw = args.gateway.rstrip("/")

    original, why = _find_inbox_message(gw, token, self_name,
                                        args.message_id, args.search_limit)
    if original is None:
        print(f"swarph mesh reply: {why}", file=sys.stderr)
        return 1

    to_node = original.get("from_node")
    if not to_node:
        print(f"swarph mesh reply: message {args.message_id} has no sender recorded, "
              f"so there is nobody to reply TO.", file=sys.stderr)
        return 1

    body = {"from_node": self_name, "to_node": to_node,
            "kind": args.kind, "content": content}
    thread_id = original.get("thread_id")
    if thread_id:
        body["thread_id"] = thread_id

    status, payload = _post_json(f"{gw}/messages", body, token)
    if status < 200 or status >= 300:
        print(f"swarph mesh reply: gateway {status}: "
              f"{payload.get('detail', '<gateway error>')}", file=sys.stderr)
        return 1

    # >>> THIS COMMAND CANNOT KNOW WHETHER AN OBLIGATION CLOSED, AND THE FIRST VERSION
    # SAID IT DID. <<< POST /messages returns id/from/to/kind/thread_id/created_at —
    # no close fact. Closing is #307 Task 2's SIDE EFFECT and it fails OPEN for a
    # ghost holder, a non-holder, or no row at all. So "an open obligation is now
    # closed" was prose asserted on exit 0 — THE EXACT SHAPE OF "#91 is waiting on a
    # seat", written inside the tool built to kill it. (grok-researcher, blocking
    # review on PR #253.) The threadless branch was already honest; this one now
    # matches it. Report the fact that was returned; name the thing that was not.
    #
    # >>> #525 SHIPPED THE CLOSE FACT, SO THE DISCLAIMER IS NOW CONDITIONAL ON THE SERVER
    # THAT ANSWERED — NOT ON A VERSION, A DATE, OR ANYTHING ANYONE HAS TO REMEMBER. <<<
    # The gateway returns `obligation_check` (no_thread / checked / not_applicable /
    # not_checked) alongside `closed_obligations`. Gate on the FIELD'S PRESENCE:
    #
    #   present  -> report the server's fact, which is the whole point of the card
    #   absent   -> keep the disclaimer, because the server really did not say
    #
    # A merge is not a deploy. #123 merged at 09:54 while the live gateway had been up
    # since the previous morning, serving pre-#525 code with no signal anywhere that the
    # two had diverged (card #496, one layer over). Keying on a version would have made
    # this command assert a fact the running server was not returning. Keying on the field
    # means it corrects itself the moment the gateway restarts and nobody sequences
    # anything. (drop-on-meta-edge, who measured the pid and the mtime.)
    #
    # AND IT HONOURS grok's PR #253 REVIEW RATHER THAN REVERSING IT: the rule was never
    # assert what you do not know. When the server does not say, this still does not claim.
    # PRESENCE, not truthiness. `if check:` would be a PROXY for this -- correct today
    # only because all four values happen to be non-empty strings, and silently wrong the
    # day one of them becomes "" or null. The comment above states presence; this is the
    # line that implements it. (drop-on-meta-edge, PR #270.)
    has_check = "obligation_check" in payload
    check = payload.get("obligation_check")
    closed = payload.get("closed_obligations")
    if args.json:
        print(json.dumps({
            "id": payload.get("id"), "to_node": to_node, "kind": args.kind,
            "thread_id": thread_id,
            # attached_to_thread is what this command KNOWS on its own. Deliberately not
            # named closed_obligation: an automated caller must not read a delivery fact
            # as a closure fact. (gpu-wsl: both outcomes return 0 and most callers here
            # are automated, so stdout prose is not a signal.)
            "attached_to_thread": bool(thread_id),
            # `null` on BOTH keys when the server said nothing — which is what an older
            # gateway is. A caller distinguishes "no obligation closed" from "this server
            # cannot tell me" by obligation_check being null, exactly as the server-side
            # field distinguishes them for itself.
            "closed_obligations": closed if has_check else None,
            "obligation_check": check,
            # >>> KEPT AS A PRECAUTION, NOT BECAUSE A CALLER WAS MEASURED NEEDING IT
            # -- AND THOSE JUSTIFY DIFFERENT AMOUNTS OF PERMANENCE. <<< There is NO
            # in-tree consumer of this key outside this command and its tests; the
            # caller doing `.get("closed_obligation", False)` is hypothetical. What is
            # real is that deleting a published key turns "cannot tell" into a
            # confident False for any such caller on the very day closures started
            # being reported, which is a silent downgrade nobody would see.
            #
            # AND IT IS AMBIGUOUS BY DESIGN: null here means cannot-tell OR looked-and-
            # none OR never-looked -- the exact ambiguity #525 exists to kill,
            # surviving inside the PR that ships the fix. Read `obligation_check` to
            # tell them apart; this key cannot. Said in --json's help too, where a
            # caller looks before writing .get. (drop-on-meta-edge, PR #270.)
            "closed_obligation": (closed[0] if has_check and closed else None),
        }, indent=2))
        return 0
    if check:
        if closed:
            ids = ", ".join(f"#{i}" for i in closed)
            print(f"replied id={payload.get('id')} to={to_node} kind={args.kind} "
                  f"in thread {thread_id} — CLOSED obligation {ids} (reported by the "
                  f"gateway, not inferred here).")
        elif check == "checked":
            print(f"replied id={payload.get('id')} to={to_node} kind={args.kind} "
                  f"in thread {thread_id} — the gateway LOOKED and closed nothing: no "
                  f"obligation on this thread is held by {self_name}.")
        else:
            print(f"replied id={payload.get('id')} to={to_node} kind={args.kind} "
                  f"— no obligation lookup ran ({check}), so nothing closed.")
    elif thread_id:
        print(f"replied id={payload.get('id')} to={to_node} kind={args.kind} "
              f"in thread {thread_id} — if {self_name} holds an open obligation on "
              f"this thread the gateway closes it, and THIS COMMAND CANNOT CONFIRM "
              f"THAT: this gateway returns no close fact. Check the card.")
    else:
        print(f"replied id={payload.get('id')} to={to_node} kind={args.kind} "
              f"— NOT IN A THREAD: message {args.message_id} carries no thread_id, so "
              f"this closes no obligation. It was sent as an ordinary DM.")
    return 0


def _mark_read(gateway: str, token: str, messages: list) -> None:
    """Mark every unread DM we just surfaced as read. Best-effort.

    Reading your inbox consumes it — like any mail client. Without this, a peer's
    request stays "unread" no matter how many times it is answered, and an agent
    whose loop asks "is it still unread?" can never exit. That is not theoretical:
    on 2026-07-10 grok-researcher answered one request 67 times (~410k tokens)
    because the CLI never called the gateway's POST /messages/{id}/read.

    A mark-read failure must NEVER fail the listing — the caller has already seen
    the messages, and losing the read receipt is strictly less bad than losing them.
    """
    base = gateway.rstrip("/")
    failed = []
    for dm in messages:
        if dm.get("read_at"):
            continue
        msg_id = dm.get("id")
        if msg_id is None:
            continue
        try:
            status, _ = _post_json(f"{base}/messages/{msg_id}/read", {}, token)
            if status < 200 or status >= 300:
                failed.append(msg_id)
        except Exception:  # network, DNS, timeout — never break the listing
            failed.append(msg_id)
    if failed:
        print(
            f"swarph mesh inbox: mark-read failed for id(s) {failed} "
            "(messages shown above; they will be re-listed as unread)",
            file=sys.stderr,
        )


def _run_inbox(args: argparse.Namespace) -> int:
    self_name, id_source = _resolve_self_with_source(args.self_name)

    # >>> A DESTRUCTIVE READ UNDER AN UNNAMED IDENTITY IS REFUSED, NOT WARNED. <<<
    # Printing the identity was the first fix and it is not sufficient, for two
    # reasons drop established in review of PR #247:
    #   1. THE USERS ARE AGENTS. A line of output is not a control for a caller that
    #      pipes to head/tail -- demonstrated first-party the same night, when lab
    #      piped `swarph ratify` to tail, saw steps 3-4 of 6, and misread a SUCCESS
    #      as a failure. A warning nobody reads is not a warning.
    #   2. THE HARM IS ASYMMETRIC AND UNOBSERVABLE BY THE VICTIM. Reading consumes,
    #      the loss is irreversible, and the peers whose unread state is destroyed
    #      cannot see it happen. A warning informs the one actor who is NOT harmed.
    # So: refuse only where BOTH conditions hold -- the identity was inferred rather
    # than named, AND the operation is destructive. Naming --as, or opting in with
    # --consume, proceeds exactly as before. Every internal caller already passes
    # --as, so this refuses nothing the mesh does to itself.
    destructive = not getattr(args, "peek", False)
    if destructive and id_source != "--as" and not getattr(args, "consume", False):
        print(
            f"swarph mesh inbox: REFUSING a destructive read as {self_name!r}, an "
            f"identity taken from {id_source} rather than named with --as.\n"
            f"  Reading marks every DM shown as READ, and that is not reversible.\n"
            f"  If you meant {self_name}: swarph mesh inbox --as {self_name}\n"
            f"  To look without consuming:  swarph mesh inbox --peek\n"
            f"  To proceed anyway:          swarph mesh inbox --consume",
            file=sys.stderr,
        )
        return 1

    token = _resolve_token(self_name, args.token_file)
    params = {"to": self_name, "limit": str(args.limit)}
    if args.unread:
        params["unread_only"] = "true"
    url = f"{args.gateway.rstrip('/')}/messages?{urllib.parse.urlencode(params)}"
    status, payload = _http_get_json(url, token)
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh inbox: gateway {status}: {detail}", file=sys.stderr)
        return 1
    messages = payload.get("messages", [])
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not getattr(args, "peek", False):
            _mark_read(args.gateway, token, messages)
        return 0
    # The identity is printed BEFORE the mail, on every path including empty --
    # an inbox you cannot attribute is worse than no inbox, and the empty case is
    # exactly where a wrong identity looks like good news.
    print(f"inbox {self_name} (identity from {id_source})")
    if not messages:
        print("  empty")
        return 0
    for dm in messages:
        print(_format_inbox_line(dm))
    if not getattr(args, "peek", False):
        _mark_read(args.gateway, token, messages)
        # >>> CONSUMPTION ANNOUNCES ITSELF. <<< Marking read is destructive and was
        # previously silent, so consuming the WRONG peer's queue produced no signal
        # at all. Not inverted to peek-by-default: PullSink -- the default monitor
        # sink -- advances its ledger on exactly this ACK, so a peek default would
        # leave `monitor status` reporting DMs pending forever.
        print(f"  marked {len(messages)} read as {self_name}")
    return 0


def _run_register(args: argparse.Namespace) -> int:
    self_name = _resolve_self_name(args.self_name)
    token_path = _peer_token_path(self_name)
    if token_path.exists() and not args.force:
        print(
            f"swarph mesh register: token file already exists: {token_path}. "
            "Use --force to re-register without clobbering by accident.",
            file=sys.stderr,
        )
        return 1
    token = _resolve_token(self_name, args.token_file, allow_peer_token=False)
    caps = {}
    for spec in args.capability:
        key, value = _parse_capability(spec)
        caps[key] = value
    # >>> #124, CLIENT HALF: MERGE OVER THE STORED BLOB BY DEFAULT. <<< The
    # gateway replaces capabilities wholesale, so submitting only the keys
    # this invocation cares about silently destroys the rest (gemini-
    # researcher's role/agent_type, lost to a model_default update). Read
    # the peer's currently-registered blob and re-submit it merged:
    # submitted keys override, stored-only keys survive. --replace opts into
    # the deliberate wholesale replace (full=true, which the gateway's #124
    # guard accepts without its 409). A register that CANNOT READ the
    # registry — first registration, gateway unreachable — proceeds with
    # what it has: the read must never BLOCK the write; there is nothing to
    # destroy on a row that does not exist yet.
    gateway = args.gateway.rstrip("/")
    stored_caps: dict = {}
    found_stored_caps = False
    if not args.replace:
        gstatus, gpayload = _http_get_json(f"{gateway}/peers/{self_name}", token)
        if gstatus == 200 and isinstance(gpayload.get("capabilities"), dict):
            stored_caps = gpayload["capabilities"]
            found_stored_caps = True
    if args.replace:
        merged = caps or {"can_claim_tasks": True}
    elif found_stored_caps or caps:
        merged = {**stored_caps, **caps}
    else:
        merged = {"can_claim_tasks": True}
    # #535: report what version this cell RUNS, on every register — a
    # SUBMITTED key, so the merge refreshes it on re-register and preserves
    # it across partial updates. Without a stored version field, "who is
    # stale" is unanswerable and "the fix propagates" is unfalsifiable.
    merged["swarph_cli_version"] = swarph_cli.__version__
    body = {
        "name": self_name,
        "url": args.url or f"http://{self_name}:8787",
        "capabilities": merged,
    }
    if args.replace:
        body["full"] = True
    status, payload = _post_json(
        f"{args.gateway.rstrip('/')}/peers/register",
        body,
        token,
    )
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh register: gateway {status}: {detail}", file=sys.stderr)
        return 1
    peer_token = payload.get("peer_token")
    token_status = payload.get("token_status")
    if peer_token:
        _write_secret_file_mode_600(token_path, peer_token)
        print(
            f"registered {payload.get('name', self_name)} "
            f"token_status={token_status or 'minted'} token_file={token_path}"
        )
        return 0
    print(
        f"registered {payload.get('name', self_name)} "
        f"token_status={token_status or 'existing'}; no new token returned"
    )
    return 0


def _version_tuple(text: object) -> Optional[tuple[int, ...]]:
    """Parse "0.45.1" into (0, 45, 1). Returns None for anything that is not
    a dotted-numeric version — an unparseable version is UNMEASURABLE, not
    current (#535's falsifier clause)."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _version_is_stale(reported: object, cutoff: str) -> Optional[bool]:
    """True = reported is older than cutoff; False = current-or-newer;
    None = unmeasurable (missing/unparseable). Numeric tuple compare, so
    0.45.10 > 0.45.9 — lexicographic would call 0.9.0 newer than 0.10.0."""
    have, want = _version_tuple(reported), _version_tuple(cutoff)
    if have is None or want is None:
        return None
    width = max(len(have), len(want))
    return have + (0,) * (width - len(have)) < want + (0,) * (width - len(want))


def _run_peers(args: argparse.Namespace) -> int:
    """#535: answer "which peers run older than X" from the STORED field —
    never by asking each cell out of band, never by assuming.

    A registry READ does not require a self identity: an operator asking
    "who is stale" on any box with the shared token is not acting AS a
    cell. --as still works (its peer token outranks the ambient one)."""
    from swarph_cli.tokens import resolve_token
    resolution = resolve_token(args.self_name, args.token_file,
                               identity_is_explicit=bool(args.self_name))
    if resolution is None:
        print("swarph mesh peers: no token (set MESH_GATEWAY_TOKEN, pass "
              "--token-file, or --as a peer with a stored token)",
              file=sys.stderr)
        return 2
    token = resolution.token
    gateway = args.gateway.rstrip("/")
    status, payload = _http_get_json(f"{gateway}/peers", token)
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>") if isinstance(payload, dict) else "<gateway error>"
        print(f"swarph mesh peers: gateway {status}: {detail}", file=sys.stderr)
        return 1
    peers = payload.get("peers") if isinstance(payload, dict) else None
    if not isinstance(peers, list):
        print("swarph mesh peers: unexpected /peers shape (no peer list)",
              file=sys.stderr)
        return 1

    cutoff = args.stale_than
    if cutoff is not None and _version_tuple(cutoff) is None:
        print(f"swarph mesh peers: --stale-than {cutoff!r} is not a "
              f"dotted-numeric version", file=sys.stderr)
        return 2

    stale, unreported, current = [], [], []
    for p in peers:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        caps = p.get("capabilities")
        version = caps.get("swarph_cli_version") if isinstance(caps, dict) else None
        version_is_valid = _version_tuple(version) is not None
        verdict = _version_is_stale(version, cutoff) if cutoff else None
        if cutoff and verdict is True:
            stale.append((p["name"], version))
        elif not version_is_valid:
            unreported.append(p["name"])
        else:
            current.append((p["name"], version))

    if cutoff:
        for name, version in stale:
            print(f"STALE       {name}  reports {version} (< {cutoff})")
        for name in unreported:
            # Named, never silently counted as current: absence of the field
            # is unmeasurability, not health — the falsifier clause.
            print(f"UNREPORTED  {name}  (no swarph_cli_version — unmeasurable, "
                  f"NOT known-current)")
        print(f"{len(stale)} stale, {len(unreported)} unreported, "
              f"{len(current)} current (cutoff {cutoff})")
    else:
        for name, version in current:
            print(f"{name}  {version or 'UNREPORTED'}")
        for name in unreported:
            print(f"{name}  UNREPORTED")
    return 0


def _default_sidecar_state_dir(self_name: str) -> Path:
    # NOT renamed to .../monitor when `swarph monitor` shipped (card #122).
    # `swarph init` pins this exact path as the cell's cursor_path and the
    # watchdog reads it (see commands/init.py); renaming it would have silently
    # split every existing cell's cursor from its reader.
    return Path.home() / "swarph_state" / self_name / "mesh-sidecar"


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {"last_msg_id": 0, "last_wake_at": 0.0, "channel_cursors": {}, "pending_channel_posts": []}
    try:
        cursor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[monitor] ignoring unreadable cursor {path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return {"last_msg_id": 0, "last_wake_at": 0.0, "channel_cursors": {}, "pending_channel_posts": []}
    if not isinstance(cursor, dict):
        return {"last_msg_id": 0, "last_wake_at": 0.0, "channel_cursors": {}, "pending_channel_posts": []}
    cursor.setdefault("last_msg_id", 0)
    cursor.setdefault("last_wake_at", 0.0)
    cursor.setdefault("channel_cursors", {})
    cursor.setdefault("pending_channel_posts", [])
    return cursor


def _write_cursor_atomic(path: Path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────────────────
# card #122 — pluggable delivery
#
# TWO PIECES OF STATE. They answer different questions and must never share a
# variable:
#
#   OBSERVATION CURSOR (cursor.json, `last_msg_id`)
#       what this monitor has READ from the gateway. One per monitor. Advances
#       on observation, ALWAYS, gated on nothing downstream.
#
#   DELIVERY LEDGER (ledgers.json, `last_delivered_id`)
#       what has been DELIVERED to a GIVEN sink. One per sink. Advances only
#       when that sink is satisfied. May lag arbitrarily far.
#
# `pending_wake` (PR #138) was the degenerate one-sink form of the ledger; this
# is that generalized, not a special case bolted beside it. The bug class being
# retired is "one variable, two questions": `last_msg_id` was named a cursor and
# was ALSO a wake receipt, so a dead tmux pane froze it forever and every poll
# re-selected the same DMs with no backoff and no alarm (droplet, 2026-07-26).
#
# Consequence worth stating plainly: NOVELTY IS A PROPERTY OF A SINK'S LEDGER,
# not of the cursor. Attach a sink tomorrow and it replays from inbox.log — the
# data was never consumed, only that sink's pointer is new.
# ─────────────────────────────────────────────────────────────────────────────

_MONITOR_REPLAY_LIMIT = 50   # matches the gateway's 50-message window
_MONITOR_PIDFILE = "monitor.pid"


class MonitorSinkError(RuntimeError):
    """A --deliver spec that must fail LOUDLY (unknown sink, or a held gate)."""


class Sink:
    """A named delivery target with its own independent ledger.

    Subclasses override `deliver` (push) or `observe` (pull). Nothing in the
    engine branches on sink type: a sink that behaves differently says so
    through these flags, so adding one never means special-casing a leg.
    """

    keeps_ledger = True   # False => no ledger, and therefore no unread tracking
    is_push = False       # True => the engine calls deliver() when the ledger lags

    def __init__(self, name: str):
        self.name = name

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> Optional[bool]:
        """Push `dms` (may be a bounded/empty replay). Three outcomes:
        True == delivered (cursor advances); False == FAILED (loud, counted —
        the sink is probably gone); None == DEFERRED (still owed, but neither
        delivered nor failed — e.g. TmuxSink's politeness gate finding a
        human mid-write. A deferral never increments consecutive_failures:
        a busy composer is not a dead sink)."""
        raise NotImplementedError

    def observe(self, state: "MonitorState", ledger: dict, window: list) -> bool:
        """Called with every poll's raw window. True == this ledger changed."""
        return False

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} pending for {self.name}"


class PullSink(Sink):
    """The DEFAULT. Consumer-pulled: the monitor pushes nothing at all.

    Its ledger advances on ACK — a message being marked read by the gateway when
    the agent runs `swarph mesh inbox` — and NEVER on observation. That is what
    lets `status` answer "you have DMs" as cursor-minus-ledger without inventing
    any new state.

    Why this is the floor and not a nicety: every PUSH sink's liveness is a
    precondition for hearing anything, and the mesh has gone silently deaf twice
    that way (tmux crash kills the wake Monitor, SessionStart drains but does not
    re-arm, and "no wake" is indistinguishable from "no mail"). A pull check run
    BY the cell lives one layer above tmux and cannot die with it.
    """

    is_push = False

    def __init__(self):
        super().__init__("pull")

    def observe(self, state: "MonitorState", ledger: dict, window: list) -> bool:
        observed = int(state.observed.get("last_msg_id", 0))
        mine = sorted(
            (int(m.get("id", 0)), bool(m.get("read_at")))
            for m in window
            if m.get("from_node") != state.self_name
        )
        new = int(ledger["last_delivered_id"])
        for msg_id, is_read in mine:
            # Stop at the OLDEST unread: an ACK on id 62 says nothing about 61.
            if not is_read:
                break
            new = max(new, msg_id)
        else:
            # Nothing unread anywhere in the window -> the consumer is caught up
            # to the cursor, including DMs the window no longer carries.
            new = max(new, observed)
        if new == int(ledger["last_delivered_id"]):
            return False
        ledger["last_delivered_id"] = new
        ledger["last_delivery_at"] = time.time()
        return True

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} unread DM{plural}"


class NoneSink(Sink):
    """Genuinely nothing: observe, append inbox.log, no ledger, no unread track.

    `none` MEANS none. An earlier draft had it keeping a ledger so `status` could
    still report unread; droplet rejected the NAME rather than the mechanism
    (DM #8532), because a flag whose name and behaviour diverge is the bug class
    we spent two days digging out — `count` that was a decaying weight, `MID` that
    straddled a mode boundary, `last_msg_id` that was also a wake receipt. The
    mechanism became `pull`; the word `none` stayed true.
    """

    keeps_ledger = False
    is_push = False

    def __init__(self):
        super().__init__("none")


class TmuxSink(Sink):
    """Poke a tmux pane — the behaviour `mesh sidecar` has always had.

    WAKE IS EDGE-TRIGGERED ON THE DRAIN, NOT PER DM BATCH (commander,
    2026-08-24): a wake stands until the inbox is observed DRAINED (gateway
    unread == 0). While one stands, further DM batches do NOT re-inject —
    the old level-trigger stacked one wake per batch into the composer
    ('check meshcheck mesh…' ×7 observed live on cursor-lin). If the pane
    observably still HOLDS an unsubmitted wake, the gate sends a single
    verified Enter nudge instead of new text — the anti-stack retry.

    POLITENESS GATE (commander, 2026-08-24, same day): even a verified wake
    INJECTS BLIND — '-l' appends to whatever sits in the composer, so a wake
    landing mid-keystroke merges into the human's half-typed line. The gate:
    no inject and no nudge unless the composer is OBSERVED in a compatible
    state ("clear" to inject, "wake"-only to nudge). A BUSY composer DEFERS
    (deliver returns None): the wake stays owed, the cursor does not advance,
    and no failure is counted — a human typing is not a dead sink. An
    UNREADABLE composer FAILS without a keystroke: never type blind, and a
    dead pane must keep the failure loud.
    """

    is_push = True

    def __init__(self, target: str):
        super().__init__(f"tmux:{target}")
        self.target = target

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> Optional[bool]:
        led = state.ledger(self.name)
        if led.get("wake_outstanding"):
            # Lazy: watchdog imports mesh module-level, so the reverse must
            # not. None (gateway error) reads as NOT-drained — re-arming on an
            # unreadable drain signal would re-open the stack.
            from swarph_cli.commands.watchdog import _gateway_unread_count
            unread = _gateway_unread_count(state.gateway, state.self_name,
                                           state.token)
            if unread == 0:
                led["wake_outstanding"] = False  # drained since — re-arm
            else:
                composer = _composer_state(self.target)
                if composer == "wake":
                    _tmux_enter(self.target)  # one verified nudge, no new text
                    return True
                if composer == "clear":
                    # Wake submitted; the cell simply hasn't drained yet. The
                    # wake did its job — re-injecting would only stack.
                    return True
                if composer == "busy":
                    # Human text shares the composer (possibly merged into our
                    # wake) — an Enter here submits THEIR line (#403's shape).
                    return None
                return False  # pane unreadable: keep the failure loud
        # Fresh path, gated on the OBSERVED composer (gpt-ops REVISE, #312):
        # "wake" — wake text already sits ALONE in the composer (a monitor
        # restart loses wake_outstanding; the ledger flag is gone but the
        # keystrokes landed). Nudge it with one Enter — injecting here would
        # stack a second copy, the exact defect the gate exists to prevent.
        # "busy" DEFERS (None — a human mid-write is not a dead sink).
        # Unreadable FAILS (False) without a single keystroke: we never type
        # blind into a pane, and a dead pane must stay loud — deferring it
        # would silently freeze the dead-sink alarm the ledger exists to ring.
        composer = _composer_state(self.target)
        if composer == "wake":
            if _tmux_enter(self.target):
                led["wake_outstanding"] = True
                return True
            return False
        if composer == "busy":
            return None
        if composer != "clear":
            return False
        # Module-global lookup on purpose: the sidecar regression suites patch
        # `mesh._tmux_wake`, and a `from`-import here would silently bypass them.
        ok = _tmux_wake(self.target)
        if ok is None:
            # The human ADOPTED the composer mid-settle (gpt-ops, #312 round
            # 5): the wake text never submitted as ours. DEFER — the cursor
            # must NOT advance, or the wake is silently lost when the human
            # never sends their line.
            return None
        if ok:
            led["wake_outstanding"] = True
            return True
        # False splits by what the pane actually holds: text observably STUCK
        # in the composer -> mark outstanding so the next poll nudges instead
        # of stacking; nothing there / unreadable -> leave the flag clear so
        # the next poll retries the inject (and the failure stays loud).
        if _wake_still_pending(self.target) is True:
            led["wake_outstanding"] = True
        return False

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} not yet delivered to {self.name}"


class TmuxNotifySink(Sink):
    """Show a tmux status-line notice without modifying the pane input buffer."""

    is_push = True

    def __init__(self, target: str):
        super().__init__(f"tmux-notify:{target}")
        self.target = target

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        # A notification is deliberately content-free: it must not leak DM text
        # into a shared status line, and unlike TmuxSink it never submits input.
        return _tmux_notify(self.target, max(1, len(dms)))

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} not yet notified to {self.name}"


class StdoutSink(Sink):
    """Write the DM to stdout. Delivery always succeeds."""

    is_push = True

    def __init__(self):
        super().__init__("stdout")

    def _emit(self, line: str) -> None:
        print(line, flush=True)

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        if not dms:
            # The ledger says something is owed but inbox.log cannot produce it
            # (log rotated, or `_log_dm` was bypassed). Say so rather than
            # marking the gap delivered in silence.
            self._emit(
                f"[monitor] stdout: {up_to_id} observed but no body recoverable "
                "from inbox.log for the owed range"
            )
            return True
        for dm in dms:
            self._emit(_format_inbox_line(dm))
        return True


class CursorPrintSink(Sink):
    """Deliver DM CONTENT to a cursor cell via ``cursor-agent --print`` (#454).

    The Windows keystroke surface (psmux send-keys, ``-l`` literal bugs,
    capture→send races, pane-id ambiguity) is bypassed entirely: the DMs
    become the prompt of a headless invocation, with session continuity from
    cursor's own ``--continue``. Delivery blocks for the agent turn and
    returns True only on a CONFIRMED result envelope — launching is not
    success, and measured 2026-08-23 neither is exit 0 (#184's class).
    """

    is_push = True

    #: A real DM turn is minutes, not seconds. Sized generously; a timeout is
    #: a FAILED delivery (ledger stays, next iteration retries), never a
    #: silent drop.
    DEFAULT_TIMEOUT_S = 900.0

    def __init__(self, cell_name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        super().__init__(f"cursor-print:{cell_name}")
        self.cell_name = cell_name
        self.timeout_s = timeout_s

    def _prompt(self, dms: list) -> str:
        # NOT _format_inbox_line: that truncates content at 160 chars for
        # terminal display. This prompt IS the delivery — the full body must
        # cross, or the cell answers a message it never received.
        lines = [
            f"You have {len(dms)} new mesh DM(s). "
            "Read each and act per your standing instructions.",
            "",
        ]
        for dm in dms:
            lines.append(
                f"--- id={dm.get('id')} from={dm.get('from_node')} "
                f"kind={dm.get('kind')} ---"
            )
            lines.append(dm.get("content") or "")
            lines.append("")
        return "\n".join(lines)

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        if not dms:
            # Nothing recoverable to hand over (log rotated) — do NOT claim a
            # delivery; the ledger stays and the gap stays visible.
            return False
        # Lazy: mesh.py must not pay spawn.py's import cost at module load,
        # and the sink only exists on boxes that run a cursor cell anyway.
        from swarph_cli.cell import load_cell, resolve_cell_path
        from swarph_cli.commands.spawn import run_cursor_print

        try:
            cell = load_cell(resolve_cell_path(self.cell_name))
        except Exception as exc:
            print(f"[monitor] cursor-print: cannot load cell "
                  f"'{self.cell_name}': {exc}", file=sys.stderr, flush=True)
            return False
        rc = run_cursor_print(cell, self._prompt(dms), timeout=self.timeout_s)
        return rc == 0

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} not yet delivered to {self.name}"


def parse_sink(spec: str) -> Sink:
    """`--deliver SINK` -> Sink. Unknown or held specs RAISE; nothing no-ops."""
    if spec == "pull":
        return PullSink()
    if spec == "none":
        return NoneSink()
    if spec == "stdout":
        return StdoutSink()
    if spec.startswith("cursor-print:"):
        target = spec[len("cursor-print:"):]
        if not target:
            raise MonitorSinkError(
                "sink 'cursor-print:' needs a cell name, e.g. cursor-print:cursor-win"
            )
        return CursorPrintSink(target)
    if spec.startswith("tmux-notify:"):
        target = spec[len("tmux-notify:"):]
        if not target:
            raise MonitorSinkError(
                "sink 'tmux-notify:' needs a target, e.g. tmux-notify:lab:0.0"
            )
        return TmuxNotifySink(target)
    if spec.startswith("tmux:"):
        target = spec[len("tmux:"):]
        if not target:
            raise MonitorSinkError("sink 'tmux:' needs a target, e.g. tmux:lab:0.0")
        return TmuxSink(target)
    if spec.startswith("webhook:"):
        # HELD by the commander: outward-facing egress, and a build greenlight
        # does not clear an egress gate. It must EXIT, not silently no-op — a
        # held feature that no-ops rots into a phantom capability, where someone
        # configures it, sees no error, and believes it is delivering.
        raise MonitorSinkError(
            "sink 'webhook:' is HELD pending the commander's egress gate "
            "(card #122); it is not implemented and will not be silently "
            "ignored. Use --deliver pull / tmux:<target> / stdout / none."
        )
    raise MonitorSinkError(
        f"unknown sink {spec!r}; expected pull, none, stdout, tmux:<target>, "
        "or tmux-notify:<target>"
    )


def _new_ledger() -> dict:
    return {
        "last_delivered_id": 0,
        "last_delivery_at": 0.0,
        "consecutive_failures": 0,
        "created_at": time.time(),
    }


def _read_ledgers(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Losing a ledger costs at most a duplicate delivery; refusing to start
        # costs every delivery. Say it loudly and carry on.
        print(f"[monitor] ignoring unreadable ledgers {path}: {exc}",
              file=sys.stderr, flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, led in data.items():
        if not isinstance(led, dict):
            continue
        base = _new_ledger()
        base.update(led)
        out[str(name)] = base
    return out


def _write_ledgers_atomic(path: Path, ledgers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(ledgers, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _replay_from_inbox_log(
    path: Path, after_id: int, limit: int
) -> tuple[list, int]:
    """Owed DMs newer than `after_id`, newest `limit` kept. Returns (dms, skipped).

    This is BOTH the restart-retry path and the late-attached-sink replay path.
    They are the same question — "what does this ledger still owe" — so they get
    one mechanism; two would drift.
    """
    if not path.exists():
        return [], 0
    keep: deque = deque(maxlen=max(0, limit))
    total = 0
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    dm = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if int(dm.get("id", 0)) <= after_id:
                    continue
                total += 1
                keep.append(dm)
    except OSError as exc:
        print(f"[monitor] cannot replay {path}: {exc}", file=sys.stderr, flush=True)
        return [], 0
    return list(keep), total - len(keep)


class MonitorState:
    def __init__(
        self,
        *,
        self_name: str,
        state_dir: Path,
        gateway: str,
        token: str,
        sinks: list,
        poll_s: int = _DEFAULT_POLL_S,
        min_interval_s: int = _DEFAULT_WAKE_MIN_INTERVAL_S,
        log_prefix: str = "[monitor]",
        replay_limit: int = _MONITOR_REPLAY_LIMIT,
    ):
        self.self_name = self_name
        self.state_dir = Path(state_dir)
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.sinks = list(sinks)
        self.poll_s = poll_s
        self.min_interval_s = min_interval_s
        self.log_prefix = log_prefix
        self.replay_limit = replay_limit
        self.cursor_path = self.state_dir / "cursor.json"
        self.ledgers_path = self.state_dir / "ledgers.json"
        self.inbox_log_path = self.state_dir / "inbox.log"
        self.pidfile_path = self.state_dir / _MONITOR_PIDFILE
        self.heartbeat_path = self.state_dir / "drain_heartbeat.json"
        # `observed` is the real dict; `cursor` is what the engine touches. The
        # deprecated sidecar swaps `cursor` for a view (see _LegacyCursorView).
        self.observed = _read_cursor(self.cursor_path)
        self.cursor = self.observed
        self.ledgers = _read_ledgers(self.ledgers_path)
        if not self.ledgers_path.exists() and "pending_wake" in self.observed:
            self._migrate_pre_122_ledger()
        # Ledgers are keyed by the sink STRING, so renaming a pane creates a
        # fresh ledger that replays from zero. That is a deliberate trade (no ids
        # for an operator to manage) — `status` reports `ledger_missing` so the
        # surprise replay is visible rather than mysterious.
        self.new_ledgers = {s.name for s in self.sinks
                            if s.keeps_ledger and s.name not in self.ledgers}
        self.deliveries: dict = {}
        self.consecutive_empty = 0
        self.disconnect_since: Optional[float] = None
        self.shutdown_requested = False
        self.iterations = 0
        self.dms_seen = 0
        # #125 option c: channel polling state
        self.channel_cursors: dict = self.observed.get("channel_cursors", {})
        self.pending_channel_posts: list = self.observed.get("pending_channel_posts", [])

    def _migrate_pre_122_ledger(self) -> None:
        """Adopt a pre-card-#122 cursor's `pending_wake` as the initial ledger.

        A `pending_wake` key in cursor.json with no ledgers.json beside it means
        this state dir was last written by the one-sink sidecar, where the ledger
        WAS that boolean. Without this, every upgraded peer's first poll would
        see `last_delivered_id=0` against a cursor in the thousands, fire a
        redundant wake and report a capped replay of mail it already delivered.

        Applied uniformly to every ledger-keeping sink -- this is the degenerate
        one-sink ledger being generalized, not a per-sink special case.
        """
        observed = int(self.observed.get("last_msg_id", 0))
        seed = 0 if self.observed.get("pending_wake") else observed
        for sink in self.sinks:
            if not sink.keeps_ledger:
                continue
            led = _new_ledger()
            led["last_delivered_id"] = seed
            led["last_delivery_at"] = float(self.observed.get("last_wake_at", 0.0))
            self.ledgers[sink.name] = led

    def ledger(self, name: str) -> dict:
        led = self.ledgers.get(name)
        if led is None:
            led = _new_ledger()
            self.ledgers[name] = led
        return led


def _log_dm(state: MonitorState, dm: dict) -> None:
    """Append to inbox.log for EVERY observed DM, whatever the sinks do.

    This archive is the thing that makes a late-attached sink able to replay, and
    the thing that makes an owed delivery survive a restart. It is written before
    any delivery is attempted, on purpose.
    """
    state.inbox_log_path.parent.mkdir(parents=True, exist_ok=True)
    with state.inbox_log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(dm) + "\n")
    print(
        f"{state.log_prefix} id={dm.get('id')} from={dm.get('from_node')} "
        f"kind={dm.get('kind')}",
        flush=True,
    )


_WAKE_PROMPT = "check mesh"
# Submit-verify bounds (#533): the settle pause lets the -l literal LAND in
# the composer before Enter can submit it (the blind gesture raced this), and
# the attempt bound keeps a never-submitting pane from being Enter-spammed
# forever — the wake stays owed instead.
_WAKE_SETTLE_S = 0.6
_WAKE_SUBMIT_ATTEMPTS = 4


def _capture_pane_lines(target: str) -> Optional[list[str]]:
    """The pane's non-empty lines, or None when the pane is UNREADABLE
    (capture error / non-zero rc). Shared by the wake verifier and the
    politeness gate — None must always fail closed, never "probably fine".

    NOT a fixed tail window: cursor's TUI renders chrome BELOW the composer
    (task count, model/status bar, '~' — measured live on cursor-lin
    2026-08-24: the composer sits 4 non-empty lines from the bottom), so a
    tail-3 lands entirely in the chrome and reads every cursor pane as
    unknown. The composer is identified structurally instead — see
    _composer_line."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            timeout=5,
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()]


_COMPOSER_MARKERS = (">", "›", "→")


def _composer_line(lines: list[str]) -> Optional[str]:
    """The LAST non-empty line starting with a composer marker — by TUI
    layout the composer is always the bottom-most prompt line; everything
    below it is chrome (status bars never start with a marker), and
    everything above is history. Cursor echoes submitted prompts as
    '│ ○ text │' and claude/codex as '> text' ABOVE the composer, so the
    last-marker rule is what keeps history out of the reading. Markers:
    '>' claude, '›' codex (gpt-ops live capture), '→' cursor (measured live
    on cursor-lin 2026-08-24: the composer renders '→ Add a follow-up')."""
    composer = None
    for ln in lines:
        s = ln.strip()
        if s.startswith(_COMPOSER_MARKERS):
            composer = s
    return composer


def _wake_still_pending(target: str) -> Optional[bool]:
    """Projection of the four-way composer state (gpt-ops, PR #312 round 4):

    True = the composer holds ONLY wake text (one unsubmitted wake, or the
    pre-fix stacked concatenation 'check meshcheck mesh' — both are drained
    by the retry Enter). False = anything else OBSERVED: a clear composer
    (the wake submitted) OR human text in the line — including wake text
    MERGED with human text ('> check mesh half-typed', typed during the
    settle). Merged must stop the loop WITHOUT another Enter: that Enter
    would submit the human's half-typed line (#403's shape). The wake is
    then out of our hands — the human owns the line, and the busy-deferral
    in TmuxSink.deliver keeps the wake owed until they send it. None =
    UNKNOWN (unreadable / no composer line recognized) — and unknown must
    FAIL CLOSED (gpt-ops, PR #306): an unverified retry Enter is not a
    no-op, it is a keypress into a pane whose composer may hold a human's
    half-typed line. Unknown stops the loop and owes the wake; it never
    earns another Enter."""
    state = _composer_state(target)
    if state is None:
        return None
    return state == "wake"


#: cursor's EMPTY composer is not bare: it renders the marker plus a dimmed
#: placeholder ('→ Add a follow-up', measured live on cursor-lin 2026-08-24).
#: Treating any text-after-marker as busy would defer every wake forever on
#: cursor; treating the placeholder as human text is the only false positive
#: it can produce, and the failure direction there is a wake never sent —
#: visible in the log, never a keystroke into someone's line.
_CURSOR_COMPOSER_PLACEHOLDER = "Add a follow-up"


def _composer_state(target: str) -> Optional[str]:
    """What the pane's composer LINE holds, four-way (the politeness gate):

      "clear" — a composer prompt observed EMPTY (bare marker, or cursor's
                placeholder). Safe to inject.
      "wake"  — the composer holds ONLY wake text (one wake, or the pre-fix
                stacked concatenation 'check meshcheck mesh'). Safe to
                nudge with ONE Enter; injecting more text would stack.
      "busy"  — the composer holds anything else: a human mid-write, or
                human text merged into our wake. Neither inject nor Enter.
      None    — pane unreadable or no composer line recognized. UNKNOWN,
                and unknown fails closed: we never type blind into a pane.

    The composer line comes from _composer_line (last marker line in the
    capture — cursor renders chrome BELOW the composer, so no fixed tail
    window can find it).
    """
    lines = _capture_pane_lines(target)
    if lines is None:
        return None
    composer = _composer_line(lines)
    if composer is None:
        return None
    content = composer[1:].strip()
    # cursor right-aligns run-state hints ('ctrl+c to stop') on the composer
    # ROW, so the placeholder is a PREFIX of the line while the agent runs,
    # not the whole line. A human message beginning with the exact placeholder
    # text is the accepted false positive — the only failure it can produce
    # is an inject alongside contrived text, never a silent deferral.
    if not content or content.startswith(_CURSOR_COMPOSER_PLACEHOLDER):
        return "clear"
    # A human who types exactly 'check mesh' by hand is indistinguishable
    # from our own unsubmitted wake — and submitting it runs the same
    # command, so the collision is harmless in that direction only.
    if content.replace(_WAKE_PROMPT, "").strip() == "":
        return "wake"
    return "busy"


def _tmux_enter(target: str) -> bool:
    """One bare Enter — the drain-edge gate's nudge for a wake that is
    OBSERVED still sitting in the composer. Never text, never blind."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _tmux_wake(target: str) -> Optional[bool]:
    """Inject the wake prompt and confirm it SUBMITTED (#533).

    Was: Codex's blind double-Enter, on the assumption that a second Enter in
    a single-submit composer is a no-op. FALSIFIED on cursor's Linux TUI —
    the Enters race the composer, the prompt never submits, and the next wake
    CONCATENATES onto it: 'check meshcheck meshcheck mesh' arrived as ONE
    prompt on cursor-lin, 2026-08-24, three times in a row.

    Now: inject, settle, then Enter-and-verify in a bounded loop — re-Enter
    only while the composer is OBSERVED holding wake text alone. Codex's
    second Enter falls out naturally (it fires only when the first did not
    submit). Three outcomes: True on an observed-clear composer (submitted);
    None when the human ADOPTED the composer mid-settle (wake text merged
    with their typing — another Enter would submit their line, so the loop
    stops, and the sink DEFERS: the cursor must not advance on a wake that
    never submitted as ours — gpt-ops, #312 round 5); False when the pane
    could not be verified (the wake stays owed). An UNREADABLE pane (capture
    failure) fails closed — no unverified retries, no claimed success; the
    wake stays owed and the ledger retries the whole wake later.
    """
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", _WAKE_PROMPT],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        time.sleep(_WAKE_SETTLE_S)
        for _ in range(_WAKE_SUBMIT_ATTEMPTS):
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            time.sleep(_WAKE_SETTLE_S)
            state = _composer_state(target)
            if state is None:
                print(
                    "[monitor] tmux wake unverifiable (capture failed); "
                    "failing closed — wake stays owed",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            if state == "clear":
                return True
            if state == "busy":
                # The human typed into the wake mid-settle (gpt-ops, #312
                # round 5): the wake text never submitted as ours, so this
                # must NOT acknowledge delivery — return None and let the
                # sink DEFER, keeping the cursor back so a later poll
                # retries the wake when the composer clears.
                return None
            # "wake": still sitting unsubmitted — loop another Enter.
        print(
            f"[monitor] tmux wake still unsubmitted after "
            f"{_WAKE_SUBMIT_ATTEMPTS} Enters; wake stays owed",
            file=sys.stderr,
            flush=True,
        )
        return False
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"[monitor] tmux wake failed; pane may hold an unsubmitted wake: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def _tmux_notify(target: str, dm_count: int) -> bool:
    """Show a transient tmux notice without sending any pane keystrokes."""
    plural = "s" if dm_count != 1 else ""
    try:
        subprocess.run(
            ["tmux", "display-message", "-t", target,
             f"mesh: {dm_count} new DM{plural}; inbox pending"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[monitor] tmux notification failed: {exc}", file=sys.stderr, flush=True)
        return False


def _select_next_poll_seconds(state: MonitorState) -> int:
    if state.disconnect_since is not None:
        if time.time() - state.disconnect_since > _BACKOFF_5XX_THRESHOLD_SECONDS:
            return _BACKOFF_5XX_SECONDS
    if state.consecutive_empty >= _BACKOFF_EMPTY_THRESHOLD:
        return _BACKOFF_EMPTY_SECONDS
    return state.poll_s


def _wake_policy_admits(policy, msg: dict, self_name: str) -> bool:
    """#194 — does THIS cell's own wake_policy admit this channel post?

    `muted` is handled by the caller (it skips the fetch entirely). Here:
      · mentions_only -> only posts that name this cell
      · all / anything else / None -> admit

    >>> FAIL OPEN, DELIBERATELY. <<< An unknown policy value, or a gateway that
    does not send one at all, admits the post. Failing CLOSED would drop channel
    posts silently — which is card #125's ORIGINAL DEFECT, not a safe default:
    seventeen cells set a policy, nothing honoured it, and nobody could tell
    because the absence looked exactly like "no posts". A filter that errs toward
    delivering is recoverable by the reader; one that errs toward silence is not.
    The inert case is announced by the caller so it cannot pass for enforcement.
    """
    if policy != "mentions_only":
        return True
    raw = msg.get("mentions")
    # The gateway stores mentions as a JSON STRING ('[]'), not a list. Parse
    # defensively: a malformed value must not decide "not mentioned" and swallow
    # the post — on any doubt, admit it and let the reader judge.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return True
    if not isinstance(raw, (list, tuple)):
        return True
    return self_name in raw


def _poll_channel_subscriptions(state: MonitorState) -> None:
    """#125 option c: discover this peer's channel memberships and poll each
    for new posts, using the existing member-gated GET /messages?channel=.
    Additive only -- any failure here must never affect the DM poll above.

    >>> THAT LAST SENTENCE IS A GUARANTEE, SO IT IS ENFORCED HERE RATHER THAN
    ASSUMED. <<< The non-200 branches below cover the HTTP failures this code
    anticipated; they do not cover the ones it did not. A raised exception --
    a channel record with no "name", a non-numeric message id, a disk error in
    _write_cursor_atomic, a socket fault inside _http_get_json -- propagates
    out of the caller's tick, and the caller invokes this on EVERY poll right
    after _monitor_deliver. So a single persistently-malformed channel record
    would not merely skip channels once: it would kill DM delivery on every
    subsequent tick, silently and forever. Channels are a convenience; DM
    delivery is the product's promise, and the convenience must never be able
    to take the promise down with it.
    """
    try:
        _poll_channel_subscriptions_inner(state)
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        # Loud but non-fatal: a swallowed failure that nobody can see is how a
        # feature ends up "working" for weeks while delivering nothing (#125's
        # own defect). The DM poll continues; the operator learns.
        print(f"{state.log_prefix} channel poll failed (DM delivery unaffected): "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)


def _poll_channel_subscriptions_inner(state: MonitorState) -> None:
    """The channel poll proper. Never call directly -- go through
    _poll_channel_subscriptions, which owns the never-break-DMs guarantee."""
    # Track existing message IDs to avoid duplicates if this is called multiple times
    existing_ids = {int(m.get("id", 0)) for m in state.pending_channel_posts}

    url = f"{state.gateway}/channels?{urllib.parse.urlencode({'peer': state.self_name})}"
    status, body = _http_get_json(url, state.token)
    if status != 200:
        return  # fail silent for channels specifically -- DM delivery is the guarantee
    members = [c for c in body.get("channels", []) if c.get("is_member")]
    subscribed = [c["name"] for c in members]
    # #194: the caller's OWN wake_policy per channel. C1 added this to
    # GET /channels?peer=; a gateway that predates it omits the key entirely.
    policies = {c["name"]: c.get("wake_policy") for c in members}
    if subscribed and all(policies[n] is None for n in subscribed):
        # >>> AN UNENFORCEABLE POLICY MUST NOT LOOK ENFORCED (#184c). <<< If the
        # gateway never sends wake_policy, this filter is INERT — every post
        # surfaces regardless of what the cell asked for. That is precisely the
        # shape that convinced 17 cells they had subscribed to something, so it
        # is stated out loud rather than left to look like filtering.
        print(f"{state.log_prefix} wake_policy absent from GET /channels — "
              f"channel filtering INERT, surfacing all posts (gateway predates #125 C1)",
              file=sys.stderr, flush=True)

    for channel in subscribed:
        policy = policies.get(channel)
        if policy == "muted":
            continue  # asked for silence; honour it before spending a fetch
        last_id = int(state.channel_cursors.get(channel, 0))
        params = {"channel": channel, "limit": "50"}
        curl = f"{state.gateway}/messages?{urllib.parse.urlencode(params)}"
        cstatus, cbody = _http_get_json(curl, state.token)
        if cstatus != 200:
            continue
        new_posts = [
            m for m in cbody.get("messages", [])
            if int(m.get("id", 0)) > last_id and m.get("from_node") != state.self_name
            and int(m.get("id", 0)) not in existing_ids
            and _wake_policy_admits(policy, m, state.self_name)
        ]
        if new_posts:
            new_posts.sort(key=lambda m: int(m["id"]))
            state.channel_cursors[channel] = int(new_posts[-1]["id"])
            state.pending_channel_posts.extend(new_posts)
            existing_ids.update(int(m.get("id", 0)) for m in new_posts)

    if subscribed:
        # Persist channel polling state to disk so separate `monitor status` can read it
        state.cursor["pending_channel_posts"] = state.pending_channel_posts
        _write_cursor_atomic(state.cursor_path, dict(state.cursor))


def _monitor_deliver(state: MonitorState) -> None:
    """Best-effort delivery to every PUSH sink whose ledger lags the cursor.

    NEVER touches the observation cursor. A failed delivery (dead pane) and a
    deferred one (idle guard) both leave the ledger where it was, so the next
    iteration retries — re-selection is not the retry mechanism, the ledger is.
    That distinction is load-bearing: the gateway only returns the last 50
    messages, so a long-throttled delivery whose message rolled out of the window
    would otherwise be lost outright.
    """
    observed = int(state.observed.get("last_msg_id", 0))
    now = time.time()
    changed = False

    for sink in state.sinks:
        if not sink.keeps_ledger:
            continue
        led = state.ledger(sink.name)
        if sink.is_push:
            delivered = int(led["last_delivered_id"])
            if delivered >= observed:
                continue          # this sink owes nothing
            if now - float(led["last_delivery_at"]) < state.min_interval_s:
                print(f"{state.log_prefix} wake suppressed by idle guard for "
                      f"{sink.name} (cursor advanced; delivery still pending)",
                      flush=True)
                continue
            dms, skipped = _replay_from_inbox_log(
                state.inbox_log_path, delivered, state.replay_limit
            )
            if skipped:
                # A silent cap reads as "delivered everything" when it did not.
                print(f"{state.log_prefix} REPLAY CAPPED for {sink.name}: "
                      f"skipped {skipped} older DM(s) above id {delivered}; "
                      f"delivering the newest {len(dms)} (limit "
                      f"{state.replay_limit}) -- see {state.inbox_log_path}",
                      file=sys.stderr, flush=True)
            outcome = sink.deliver(state, dms, observed)
            if outcome is None:
                # DEFERRED (e.g. TmuxSink's politeness gate): the wake stays
                # owed — the cursor does NOT advance — but a deferral is not
                # a failure: no count, no alarm, no ledger write. Retried on
                # the next poll like any owed delivery.
                print(f"{state.log_prefix} delivery DEFERRED for {sink.name} "
                      f"(composer holds human text); wake stays owed, "
                      f"no failure counted",
                      flush=True)
                continue
            if outcome:
                led["last_delivered_id"] = observed
                led["last_delivery_at"] = now
                led["consecutive_failures"] = 0
                state.deliveries[sink.name] = state.deliveries.get(sink.name, 0) + 1
            else:
                # A dead sink is VISIBLE instead of silently freezing anything.
                led["consecutive_failures"] = int(led["consecutive_failures"]) + 1
                print(f"{state.log_prefix} DELIVERY FAILED to {sink.name} "
                      f"({led['consecutive_failures']} consecutive) -- DMs up to "
                      f"id {observed} were still observed and archived; the sink "
                      f"is probably gone (session restart / renamed target)",
                      file=sys.stderr, flush=True)
            changed = True

    if changed:
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)


def _monitor_iteration(state: MonitorState, *, poll_channels: bool = True) -> None:
    state.iterations += 1
    last_id = int(state.observed.get("last_msg_id", 0))
    # NO unread_only: novelty is the `id > last_msg_id` cursor below, not the read
    # flag. Since `mesh inbox` now marks read (INCIDENT 2026-07-10), a DM read before
    # the next poll would vanish from an unread-filtered query and the cell would
    # never be woken. The two mechanisms must stay orthogonal.
    params = {"to": state.self_name, "limit": "50"}
    url = f"{state.gateway}/messages?{urllib.parse.urlencode(params)}"
    status, body = _http_get_json(url, state.token)
    if status == 0:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        return
    if status >= 500:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        print(f"{state.log_prefix} gateway {status}: {body.get('detail', '?')}",
              file=sys.stderr)
        return
    if status >= 400:
        print(f"{state.log_prefix} gateway {status}: {body.get('detail', '?')}",
              file=sys.stderr)
        return

    state.disconnect_since = None
    window = body.get("messages", [])
    messages = [
        m
        for m in window
        if int(m.get("id", 0)) > last_id and m.get("from_node") != state.self_name
    ]

    if messages:
        messages.sort(key=lambda m: int(m["id"]))
        state.consecutive_empty = 0
        new_last_id = last_id
        for dm in messages:
            _log_dm(state, dm)
            state.dms_seen += 1
            new_last_id = max(new_last_id, int(dm["id"]))

        # ── BOOKKEEPING: advance the cursor because the DMs were OBSERVED ──
        # Unconditional, and BEFORE any sink is touched. This previously happened
        # inside `if _tmux_wake(...)`, so a gone pane froze it forever; that is
        # the same shape as the 2026-07-10 grok-researcher incident (read-marking
        # tied to listing; one request answered 67 times, ~410k tokens).
        # Delivery is delivery; the cursor is bookkeeping.
        state.observed["last_msg_id"] = new_last_id
        _write_cursor_atomic(state.cursor_path, dict(state.cursor))
    else:
        state.consecutive_empty += 1

    # Pull-shaped sinks read the raw window (ACKs live in `read_at`).
    acked = False
    for sink in state.sinks:
        if sink.keeps_ledger and sink.observe(state, state.ledger(sink.name), window):
            acked = True
    if acked:
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)

    # A delivery deferred by the idle guard (or failed against a dead sink) must
    # still land even though no NEW mail arrived, so this runs on every poll.
    _monitor_deliver(state)

    # MATERIALIZE new ledgers even if nothing moved them. Found by driving the
    # real CLI: a pure `--deliver pull` monitor never writes ledgers.json until
    # somebody ACKs, so `status` reported "no ledger on disk" -- the
    # late-attached-sink warning -- forever, on the DEFAULT path. A warning that
    # fires when nothing is wrong is a warning nobody reads when something is.
    if state.ledgers and not state.ledgers_path.exists():
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)
        state.new_ledgers = set()

    # Poll channel subscriptions (independent of DM delivery, additive only).
    # >>> NOT ON THE SIDECAR PATH. <<< _sidecar_iteration aliases this function, so
    # without the flag every sidecar tick would also fetch /channels and each
    # subscribed channel's messages. That is redundant AND costly there: since C1
    # fans a channel post out as a real claude_messages row addressed to each
    # member, the sidecar already sees channel traffic through its own DM poll. The
    # only thing this poll adds is `pending_channel_posts`, which exists for
    # `monitor status` — a surface the sidecar does not serve. So the sidecar would
    # pay extra latency and an extra HTTP call per tick, on the wake path, to
    # collect data nothing in it reads.
    if poll_channels:
        _poll_channel_subscriptions(state)

    # #544 Proposal A drain heartbeat: "I completed a drain iteration
    # successfully at T" -- unconditional, whether or not new DMs existed.
    # Every early `return` above (status==0, >=500, >=400) skips this on
    # purpose: those are failed iterations, not successful ones. A silent
    # hang inside _http_get_json also never reaches here, which is the
    # point -- staleness of this file is the signal, not its content.
    # `pid` is LOAD-BEARING, not diagnostic garnish: it is the only thing that
    # separates "the writer hung" from "the writer never had this feature".
    # Both present as a frozen file, and during any rollout the second is the
    # COMMON case -- see _classify_drain_failure's `writer_lacks_heartbeat`.
    _write_cursor_atomic(state.heartbeat_path, {
        "ts": time.time(), "iterations": state.iterations, "pid": os.getpid()})


def _monitor_loop(state: MonitorState) -> int:
    print(
        f"{state.log_prefix} starting self={state.self_name} "
        f"sinks={','.join(s.name for s in state.sinks)} "
        f"gateway={state.gateway} state={state.state_dir}",
        flush=True,
    )
    while not state.shutdown_requested:
        try:
            _monitor_iteration(state)
        except KeyboardInterrupt:
            state.shutdown_requested = True
            break
        except Exception as exc:
            print(f"{state.log_prefix} iteration error: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        time.sleep(_select_next_poll_seconds(state))
    return 0


# ── pidfile: `start` must be safe to call unconditionally from a hook ────────

def _proc_cmdline(pid: int) -> Optional[str]:
    """The process's argv, space-joined. None when it cannot be determined."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fp:
            raw = fp.read()
    except OSError:
        return None            # not Linux, or the process is gone
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip() or None


# Liveness is TRI-STATE. "I could not determine" is not "dead" — see _process_liveness.
LIVENESS_ALIVE = "alive"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"


def _windows_liveness(pid: int) -> str:
    """Ask Windows directly, via OpenProcess — never via os.kill.

    PROCESS_QUERY_LIMITED_INFORMATION (0x1000) is the minimum right that answers
    "does this pid exist", and it is grantable across sessions where the rights
    os.kill needs are not. ERROR_ACCESS_DENIED means the process EXISTS and we may
    not inspect it — that is ALIVE, not dead. ERROR_INVALID_PARAMETER means no such
    pid. Anything else is UNKNOWN, never dead.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                  # noqa: BLE001
        return LIVENESS_UNKNOWN
    ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, STILL_ACTIVE = 5, 87, 259
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                return LIVENESS_ALIVE                  # exists; we just cannot look
            if err == ERROR_INVALID_PARAMETER:
                return LIVENESS_DEAD                   # no such pid
            return LIVENESS_UNKNOWN
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return LIVENESS_UNKNOWN
            return LIVENESS_ALIVE if code.value == STILL_ACTIVE else LIVENESS_DEAD
        finally:
            k32.CloseHandle(handle)
    except Exception:                                  # noqa: BLE001
        return LIVENESS_UNKNOWN


def _process_liveness(pid: int) -> str:
    """'alive' | 'dead' | 'unknown'.

    >>> CARD #195 — `os.kill(pid, 0)` IS NOT A LIVENESS PROBE ON WINDOWS, AND ITS
    FAILURE WAS BEING SWALLOWED INTO "DEAD". <<< Measured by workstation-lc against
    a monitor that was demonstrably draining:

        pid 6120 alive per Windows ................. True
        os.kill(6120, 0) from another process ...... OSError [WinError 87]
                                                     -> `except OSError: return False`
        pid 6120 after the probe ................... STILL ALIVE

    The error appears when probing a process the caller did NOT spawn — the worker
    is a grandchild of a detached cmd.exe, a different session/handle-rights context.
    His first throwaway test passed because he probed his own CHILD: a child-process
    control cannot surface this, which is why it read safe.

    ONE LINE PRODUCED BOTH SYMPTOMS: `monitor status` reported "not running" about
    every healthy monitor, AND the single-instance guard believed nothing was
    running, so it never blocked a second start. False negative and duplicate
    spawning from the same swallow.

    >>> THE RULE: "COULD NOT DETERMINE" MUST NEVER BE REPORTED AS "DEAD" BY A GUARD
    WHOSE JOB IS TO PREVENT A SECOND START. <<< A blocked start is visible and
    recoverable; a duplicate is silent and produces two writers over one cursor.
    So callers treat UNKNOWN as occupied, and `status` says so rather than claiming
    the process is running.
    """
    if sys.platform == "win32":
        return _windows_liveness(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LIVENESS_DEAD
    except PermissionError:
        return LIVENESS_ALIVE       # exists, owned by another user
    except OSError:
        return LIVENESS_UNKNOWN     # NOT dead — we simply could not tell
    return LIVENESS_ALIVE


def _process_alive(pid: int) -> bool:
    """Back-compat bool. UNKNOWN counts as alive — see _process_liveness for why
    treating an undetermined answer as "dead" is the failure mode, not the safe one."""
    return _process_liveness(pid) in (LIVENESS_ALIVE, LIVENESS_UNKNOWN)


def _terminate(pid: int) -> None:
    """SIGTERM, isolated in its own function so `stop`'s one dangerous call is a
    single named seam -- easy to see in review, easy to pin in a test."""
    import signal

    os.kill(pid, signal.SIGTERM)


def read_pidfile(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("pid"), int) else None


def pidfile_status(path: Path) -> tuple[str, Optional[dict]]:
    """One of 'absent' | 'live_ours' | 'stale' | 'foreign', plus the record.

    'foreign' exists because adopting a live PID we cannot PROVE is ours is how
    `stop` ends up killing something unrelated. When ownership cannot be decided
    we return 'foreign', which means: reclaim the FILE, never signal the PROCESS.
    """
    rec = read_pidfile(path)
    if rec is None:
        return "absent", None
    pid = int(rec["pid"])
    if not _process_alive(pid):
        return "stale", rec
    recorded = rec.get("cmdline")
    current = _proc_cmdline(pid)
    if not recorded:
        # Written on a platform with no /proc. Fall back to the identity we did
        # record; this is weaker than a cmdline match and deliberately last.
        return ("live_ours", rec) if current is None else ("foreign", rec)
    if current is not None and current == recorded:
        return "live_ours", rec
    return "foreign", rec


def write_pidfile(path: Path, *, self_name: str, sinks: list, poll_s: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "pid": os.getpid(),
        "self": self_name,
        "sinks": [s.name if isinstance(s, Sink) else str(s) for s in sinks],
        "poll_s": poll_s,
        "started_at": time.time(),
        "cmdline": _proc_cmdline(os.getpid()),
        # DECLARED CAPABILITY, written by the writer itself at start (lab-ovh,
        # DM 25744): "capability has to be established from something OTHER than
        # the signal itself -- asking the artifact whether the artifact is
        # supported is circular." Inferring heartbeat support from the heartbeat
        # file is exactly that circle. A build without this key predates the
        # feature and CANNOT emit one, which is a different fact from a writer
        # that can and has stopped -- and the two must never share a cause.
        "emits_heartbeat": True,
    }
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ── the deprecated `swarph mesh sidecar` surface ─────────────────────────────

class _LegacyCursorView(MutableMapping):
    """`mesh sidecar`'s flat cursor dict, COMPUTED over the card-#122 split.

    Its callers (and three regression suites) read `cursor["last_msg_id"]`,
    `cursor["pending_wake"]` and `cursor["last_wake_at"]`. Under the split those
    are two different questions living in two files: `last_msg_id` is the
    OBSERVATION cursor, the other two are the tmux sink's LEDGER.

    They are computed here rather than mirrored, because keeping a second copy in
    sync is precisely the "one variable, two questions" trap the card removes.
    """

    _KEYS = ("last_msg_id", "last_wake_at", "pending_wake")

    def __init__(self, state: "MeshSidecarState", observed: dict, sink_name: str):
        self._state = state
        self._observed = observed
        self._sink = sink_name

    def _led(self) -> dict:
        return self._state.ledger(self._sink)

    def _observed_id(self) -> int:
        return int(self._observed.get("last_msg_id", 0))

    def __getitem__(self, key):
        if key == "last_msg_id":
            return self._observed.get("last_msg_id", 0)
        if key == "last_wake_at":
            return float(self._led()["last_delivery_at"])
        if key == "pending_wake":
            return int(self._led()["last_delivered_id"]) < self._observed_id()
        raise KeyError(key)

    def __setitem__(self, key, value):
        if key == "last_msg_id":
            was_pending = self["pending_wake"]
            self._observed["last_msg_id"] = value
            if not was_pending:
                # Legacy semantics: in the flat dict, assigning `last_msg_id`
                # never CREATED an owed wake -- `pending_wake` was a separate
                # field that stayed False. Keep the ledger in step so an outside
                # caller seeding the cursor does not conjure a phantom delivery.
                # (The engine writes `state.observed` directly and never lands
                # here, so a genuinely new observation still owes.)
                self._led()["last_delivered_id"] = int(value)
            return
        if key == "last_wake_at":
            self._led()["last_delivery_at"] = float(value)
            return
        if key == "pending_wake":
            self._led()["last_delivered_id"] = 0 if value else self._observed_id()
            return
        raise KeyError(key)

    def __delitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)


class MeshSidecarState(MonitorState):
    """DEPRECATED compat facade: a monitor with exactly one `tmux:` sink.

    Kept because droplet and gpu-wsl are running `swarph mesh sidecar` right now;
    breaking their command to rename a verb is not a trade worth making.
    """

    def __init__(
        self,
        *,
        self_name: str,
        state_dir: Path,
        gateway: str,
        token: str,
        tmux_target: str,
        poll_s: int,
        wake_min_interval_s: int,
    ):
        sink = TmuxSink(tmux_target)
        super().__init__(
            self_name=self_name,
            state_dir=Path(state_dir),
            gateway=gateway,
            token=token,
            sinks=[sink],
            poll_s=poll_s,
            min_interval_s=wake_min_interval_s,
            log_prefix="[mesh-sidecar]",
        )
        self.tmux_target = tmux_target
        self.wake_min_interval_s = wake_min_interval_s
        self._sink_name = sink.name
        self.cursor = _LegacyCursorView(self, self.observed, sink.name)

    @property
    def wakes_sent(self) -> int:
        return self.deliveries.get(self._sink_name, 0)

    @property
    def consecutive_wake_failures(self) -> int:
        return int(self.ledger(self._sink_name)["consecutive_failures"])


# Legacy aliases. One engine underneath, so the deprecated verb cannot drift.
# >>> THE SIDECAR IS NOT A BARE ALIAS ANY MORE, AND THE DIFFERENCE IS DELIBERATE. <<<
# It was `_sidecar_iteration = _monitor_iteration`, which meant a change aimed at the
# monitor landed on the wake path invisibly — #125's channel poll did exactly that and
# broke test_sidecar_wakes_on_new_mail_and_advances_cursor. Sharing the engine is still
# right (the verbs must not drift); what the alias hid was that they have DIFFERENT JOBS.
# The sidecar wakes a cell on mail; the monitor also maintains status state.
def _sidecar_iteration(state: MonitorState) -> None:
    """The wake path: same engine, no channel polling. See _monitor_iteration."""
    _monitor_iteration(state, poll_channels=False)
_sidecar_deliver_wake = _monitor_deliver


def _run_sidecar(args: argparse.Namespace) -> int:
    """DEPRECATED alias for `swarph monitor start --deliver tmux:<target>` (#122)."""
    state_dir_arg = Path(args.state_dir).expanduser() if args.state_dir else None
    self_name = _resolve_self_name(args.self_name, state_dir=state_dir_arg)
    state_dir = state_dir_arg or _default_sidecar_state_dir(self_name)
    tmux_target = args.tmux_target or os.environ.get("SWARPH_TMUX_TARGET")
    if not tmux_target:
        print(
            "swarph mesh sidecar: pass --tmux-target or set SWARPH_TMUX_TARGET",
            file=sys.stderr,
        )
        return 2
    # STDERR, never stdout -- stdout is a sink now.
    print(
        "swarph mesh sidecar: DEPRECATED (card #122). Use:  swarph monitor start "
        f"--deliver tmux:{tmux_target}   (or --deliver pull for a silent "
        "'you have DMs' check that survives a dead pane)",
        file=sys.stderr,
    )
    token = _resolve_token(self_name, args.token_file)
    state = MeshSidecarState(
        self_name=self_name,
        state_dir=state_dir,
        gateway=args.gateway,
        token=token,
        tmux_target=tmux_target,
        poll_s=args.poll_seconds,
        wake_min_interval_s=args.wake_min_interval,
    )
    if args.once:
        # Module-global call: `--once` is the seam the sidecar suites patch.
        _sidecar_iteration(state)
        return 0
    write_pidfile(
        state.pidfile_path,
        self_name=self_name,
        sinks=state.sinks,
        poll_s=args.poll_seconds,
    )
    try:
        return _monitor_loop(state)
    finally:
        if pidfile_status(state.pidfile_path)[0] == "live_ours":
            state.pidfile_path.unlink(missing_ok=True)


def run_mesh(argv: list[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "send":
            return _run_send(args)
        if args.command == "reply":
            return _run_reply(args)
        if args.command == "inbox":
            return _run_inbox(args)
        if args.command == "register":
            return _run_register(args)
        if args.command == "peers":
            return _run_peers(args)
        if args.command == "sidecar":
            return _run_sidecar(args)
        parser.error(f"unknown command: {args.command}")
    except RuntimeError as exc:
        print(f"swarph mesh: {exc}", file=sys.stderr)
        return 2
    return 2
