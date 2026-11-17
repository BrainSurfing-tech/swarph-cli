"""``swarph secret-guard hook`` — Claude Code secret guard (card #1642).

Two events, one rule each:

  UserPromptSubmit  the prompt is run through ``swarph_cli.redact`` — the
                    ONLY pattern source. Any marker redact emits blocks the
                    prompt. The reason names the credential CLASS only
                    (provider derived from redact's marker type); no
                    character of the matched value is ever printed.
  PreToolUse/Bash   deny when a file-reading command word (cat/head/tail/
                    less/more/strings/xxd, incl. after sudo/env prefixes)
                    targets a credential path (*.env, *.key, auth.json,
                    gh hosts.yml). Everything else passes silently.

>>> FAIL OPEN, DELIBERATELY — THE OPPOSITE OF gh_route. <<< gh_route denies
on crash because a crashed router would run `gh` as the wrong identity.
Here a crashed guard would block EVERY prompt and tool call, so any crash
(bad JSON, unexpected payload, redact raising) warns on STDERR, emits
nothing that blocks, and exits 0. The prompt/command text and any secret
value are never printed to stdout, stderr, or logs on any path; crash
warnings name only the exception type for the same reason.

Wiring (Claude Code settings): register this verb as BOTH the
UserPromptSubmit and the PreToolUse/Bash hook with the fail-open form
``swarph secret-guard hook || exit 0`` and a hook timeout of at most 5
seconds, e.g. ``{"type": "command", "command": "swarph secret-guard hook
|| exit 0", "timeout": 5}``. The ``|| exit 0`` plus the in-code fail-open
above mean a missing/slow/crashed guard never blocks a turn.

Harness turns (G1, #5662): a UserPromptSubmit turn the harness itself
injects — a task-notification, a system-reminder, or a swarph channel DM
— must NEVER be blocked, and identifier values (tool-use / request /
message ids, UUIDs) must never cause a block on a typed turn either.
Measured on this box (Claude Code hook capture, 2026): the
UserPromptSubmit hook payload carries ONLY ``session_id``,
``transcript_path``, ``cwd``, ``permission_mode``, ``prompt_id``,
``hook_event_name`` and ``prompt`` — Claude Code provides NO
prompt-source/origin field to the hook, so there is no harness-guaranteed
signal to key on. The exemption therefore rests on the prompt TEXT
envelope alone (see ``_is_harness_turn``): an anchored, fully-framed
``<task-notification>…</task-notification>``,
``<system-reminder>…</system-reminder>`` or
``<channel source=…>…</channel>`` block, plus any payload source field
if a future/other harness supplies one. RESIDUAL RISK, stated honestly:
that envelope is attacker-controllable text — a user who wraps a secret
in a complete harness envelope typed by hand would bypass the prompt
check. That is the unavoidable cost of never blocking harness turns
when the only signal is the envelope; the framing is kept as structural
as the real payloads allow (anchored at the start, closing tag at the
end, ``source=`` required on channel) to keep the bypass deliberate
rather than accidental. Identifier scrubbing (``_strip_id_values``)
is fail-closed: a value redact classes as a KNOWN credential is never
scrubbed, so ``request_id: sk-ant-…`` still blocks.

Rework #5667 (C/T/I/E) and ruling A (#5690, drops P4):

  P4  REMOVED by commander ruling A (#5690): the guard-level value
      post-filter added in #5667/#5670 (``_strip_nonsecret_values`` /
      ``_is_nonsecret_value``) is dropped entirely. Redact's
      key=value and Bearer layers now block exactly as they did at
      head 83a05e6 (pre-P4): any value redact marks after a secret
      key blocks, including snake_case, ``None``/``true``/``false``
      and the key's own name. No value-shape filter remains.
  C   Bash tokenisation runs shlex with NO comment characters, so a
      ``#`` comment line, a trailing ``#`` comment, or a ``#`` inside a
      word on an earlier line can never swallow the newline that makes
      a following reader command a new command. A here-string
      (``cat <<< .env``) or an echo that only prints ``.env`` text is
      not a file read and is allowed.
  T   (superseded by S in #5670 — the 16 KB cap is removed.)

Rework #5670 (S chunked scanning; its P4' filter is removed above):

  S   The whole prompt is scanned in chunks of at most
      ``_CHUNK_CHARS`` (2048) characters, keeping whole lines together
      where they fit and cutting long lines with a
      ``_CHUNK_OVERLAP`` (512) character overlap, so a key straddling
      a cut is still seen in full in the neighbouring chunk (512 far
      exceeds the longest fixed credential-pattern context redact
      needs: the 21-char ``aws_secret_access_key`` key plus separator
      and its 20-char minimum value is 44 chars; Bearer needs 19).
      A ``-----BEGIN ... -----END-----`` PEM block is never split
      across chunks (it is yielded whole even if over the chunk
      size). The first hit blocks. Fail-open ONLY when a
      ``_SCAN_BUDGET_SECONDS`` (~3 s) time budget runs out mid-scan;
      then the hook allows the prompt but emits a ``systemMessage``
      saying the prompt was not fully scanned — never a silent pass.
      The old 16384-char silent cap (``_MAX_REDACT_CHARS``) is gone:
      prompts of any length are scanned, chunk by chunk.
  I   Identifier scrubbing is by SHAPE, wherever the shape appears:
      ``toolu_`` + >=24 chars, ``req_``/``msg_`` ids, and UUIDs. A
      generic high-entropy value after an id label (``session_id=…``,
      a JSON ``id`` field) is NOT scrubbed and still blocks.
  E   The text-envelope exemption applies only when the WHOLE prompt
      is one or more complete envelopes separated by whitespace.
      Text between envelopes is scanned normally.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
import time
from typing import Iterator, Optional

from swarph_cli import redact

# Marker type (from redact) -> credential class named in the block reason.
_CLASS_BY_MARKER = {
    "ANTHROPIC_KEY": "Anthropic",
    "GITHUB_TOKEN": "GitHub",
    "API_KEY": "API key",
    "GCP_API_KEY": "GCP (Google Cloud)",
    "SLACK_TOKEN": "Slack",
    "AWS_KEY": "AWS",
    "AWS_SECRET": "AWS",
    "JWT": "JWT",
    "BEARER_TOKEN": "bearer token",
    "PEM_PRIVATE_KEY": "PEM private key",
    "URI_CREDENTIAL": "URI credential",
    "DSN_CREDENTIAL": "DSN credential",
    "CLI_PASSWORD": "CLI password",
    "HIGH_ENTROPY": "high-entropy secret",
    "CUSTOM": "custom-rule credential",
}

_MARKER_RE = re.compile(r"\[REDACTED:([A-Z0-9_]+)(?::[A-Z0-9_]+)?\]")

_READERS = {"cat", "head", "tail", "less", "more", "strings", "xxd"}

# S (#5670): chunked scanning — no whole-prompt cap. Each chunk is at
# most this many chars (a PEM block yielded whole may exceed it).
_CHUNK_CHARS = 2048
# Overlap when a single long line must be cut: sized so a credential
# pattern straddling a cut is fully present in a neighbouring chunk.
# Longest fixed context redact needs is ~44 chars (see docstring).
_CHUNK_OVERLAP = 512
# Fail-open only when this budget runs out mid-scan (with systemMessage).
_SCAN_BUDGET_SECONDS = 3.0


def _classes_of(text: str) -> list[str]:
    """Credential classes redact finds in ``text``, in redact's own order.

    Detection is forced with opt-out disabled: SWARPH_REDACT_OPTOUT and
    opt-out events are an emission feature of redact, not a guard bypass.
    Built ONLY on redact's layers — no second pattern set lives here.
    Callers pass chunks (S, #5670); this helper itself has no size cap.
    """
    saved_env = os.environ.pop("SWARPH_REDACT_OPTOUT", None)
    ctx = getattr(redact, "_OPT_OUT_CTX", None)
    token = ctx.set(False) if ctx is not None else None
    try:
        redacted = redact.redact_text(text, caller="secret-guard")
    finally:
        if token is not None:
            ctx.reset(token)
        if saved_env is not None:
            os.environ["SWARPH_REDACT_OPTOUT"] = saved_env
    if not isinstance(redacted, str) or redacted == text:
        return []
    classes: list[str] = []
    for marker in _MARKER_RE.findall(redacted):
        label = _CLASS_BY_MARKER.get(marker, "credential")
        if label not in classes:
            classes.append(label)
    # redact changed the text but emitted no marker we know: still a hit.
    return classes or ["credential"]


# --- G1: harness-turn exemption + identifier scrubbing ---------------------

# I (#5667): identifier shapes, scrubbed wherever they appear.
_ID_SHAPE_RE = re.compile(
    r"\btoolu_[A-Za-z0-9_\-]{24,}\b"
    r"|\breq_[A-Za-z0-9_\-]{8,}\b"
    r"|\bmsg_[A-Za-z0-9_\-]{8,}\b"
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ENVELOPE_RES = [
    re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<channel\s[^>]*\bsource\s*=[^>]*>.*?</channel>", re.DOTALL),
]
_HARNESS_SOURCE_VALUES = {
    "system", "harness", "channel", "task_notification",
    "task-notification", "peer",
}


def _scrubbable(value: str) -> bool:
    """An id value may be scrubbed only if redact does NOT class it as a
    known credential — scrubbing is fail-closed (high-entropy-only or
    clean values scrub; Anthropic/GitHub/JWT/PEM/... never do)."""
    classes = _classes_of(value)
    return not classes or classes == ["high-entropy secret"]


def _strip_id_values(prompt: str) -> str:
    """Replace identifier-SHAPED values with ``ID`` wherever they appear
    (I, #5667): ``toolu_``/``req_``/``msg_`` ids and UUIDs. A generic
    high-entropy value after an id label is not id-shaped, so it is
    never touched here and still blocks. Fail-closed via _scrubbable."""

    def _sub(m: re.Match) -> str:
        return "ID" if _scrubbable(m.group(0)) else m.group(0)

    return _ID_SHAPE_RE.sub(_sub, prompt)


def _is_harness_turn(payload: dict, prompt: str) -> bool:
    """True for a harness-injected turn (see module docstring for the
    residual risk: Claude Code gives the hook no source field, so this
    keys on payload source fields IF present, else the anchored,
    fully-framed text envelope)."""
    for key in ("promptSource", "prompt_source", "promptsource"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip().lower() in _HARNESS_SOURCE_VALUES:
            return True
    origin = payload.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        if isinstance(kind, str) and kind.strip().lower() in _HARNESS_SOURCE_VALUES:
            return True
    elif isinstance(origin, str) and origin.strip().lower() in _HARNESS_SOURCE_VALUES:
        return True
    for key in ("turnOrigin", "turn_origin"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip().lower() in _HARNESS_SOURCE_VALUES:
            return True
    # E (#5667): only when the WHOLE prompt is one or more complete
    # envelopes separated by whitespace. Text between envelopes (or
    # around a single one) is not exempt and is scanned normally.
    rest = prompt.lstrip("\ufeff").lstrip()
    count = 0
    while rest.strip():
        rest = rest.lstrip()
        matched = False
        for pat in _ENVELOPE_RES:
            m = pat.match(rest)
            if m:
                rest = rest[m.end():]
                count += 1
                matched = True
                break
        if not matched:
            return False
    return count >= 1


def _atomic_units(text: str) -> Iterator[str]:
    """Yield lines, except a -----BEGIN ... -----END----- block is
    one atomic unit so a PEM block is never split (S, #5670)."""
    lines = text.splitlines(True)
    i, n = 0, len(lines)
    while i < n:
        if "-----BEGIN" in lines[i]:
            buf = [lines[i]]
            i += 1
            while i < n and "-----END" not in buf[-1]:
                buf.append(lines[i])
                i += 1
            yield "".join(buf)
        else:
            yield lines[i]
            i += 1


def _iter_chunks(text: str) -> Iterator[str]:
    """Yield chunks of at most ``_CHUNK_CHARS`` chars (S, #5670):
    whole lines are kept together where they fit, a single long line
    is cut with ``_CHUNK_OVERLAP`` chars of overlap, and a PEM block
    is yielded whole even when it exceeds the chunk size."""
    current = ""
    for unit in _atomic_units(text):
        if "-----BEGIN" in unit and "-----END" in unit:
            if current:
                yield current
                current = ""
            yield unit
            continue
        if len(unit) > _CHUNK_CHARS:
            if current:
                yield current
                current = ""
            start = 0
            while start < len(unit):
                end = min(start + _CHUNK_CHARS, len(unit))
                yield unit[start:end]
                if end >= len(unit):
                    break
                start = end - _CHUNK_OVERLAP
            continue
        if current and len(current) + len(unit) > _CHUNK_CHARS:
            yield current
            current = unit
        else:
            current += unit
    if current:
        yield current


def _chunk_classes(chunk: str) -> list[str]:
    classes = _classes_of(chunk)
    if not classes:
        return []
    scrubbed = _strip_id_values(chunk)
    if scrubbed == chunk:
        return classes
    # If every hit came from an id-shaped value, it was identifiers,
    # not credentials. (Ruling A: no value-shape post-filter remains.)
    return _classes_of(scrubbed)


def _detect_classes(prompt: str) -> tuple[list[str], bool]:
    """Scan ``prompt`` chunk by chunk (S, #5670). Returns
    ``(classes, complete)``: first hit wins; ``complete`` is False
    only when the time budget ran out before every chunk was scanned."""
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    for chunk in _iter_chunks(prompt):
        if time.monotonic() > deadline:
            return [], False
        classes = _chunk_classes(chunk)
        if classes:
            return classes, True
    return [], True


def _is_credential_path(arg: str) -> bool:
    base = arg.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return (
        fnmatch.fnmatchcase(base, "*.env")
        or fnmatch.fnmatchcase(base, "*.key")
        or base == "auth.json"
        or base == "hosts.yml"
    )


def _path_class(arg: str) -> str:
    """Path CLASS named in the deny reason (F3) — never the base name."""
    base = arg.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if fnmatch.fnmatchcase(base, "*.env"):
        return ".env file"
    if fnmatch.fnmatchcase(base, "*.key"):
        return "key file"
    if base == "auth.json":
        return "auth.json credentials file"
    return "gh hosts file"


def _newlines_to_separators(command: str) -> str:
    """Replace UNQUOTED newlines with `` ; `` so shlex (which treats a
    newline as plain whitespace) preserves command boundaries. Quote-
    and escape-aware: a newline inside quotes stays inside the token,
    so a quoted separator can never split a command (F1)."""
    out: list[str] = []
    quote: Optional[str] = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and quote != "'":
            out.append(ch)
            if i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            i += 1
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if quote is not None and ch == quote:
            quote = None
            out.append(ch)
            i += 1
            continue
        if quote is None and ch in ("\n", "\r"):
            out.append(" ; ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tokens(command: str) -> list[str]:
    try:
        lex = shlex.shlex(
            _newlines_to_separators(command), posix=True,
            punctuation_chars="|&;()<>")
        lex.whitespace_split = True
        lex.commenters = ""  # C (#5667): '#' never starts a comment
        return list(lex)
    except ValueError:
        return command.split()


_OPERATORS = {"|", "||", "&&", ";", "&", "(", ")", "<", ">", ">>"}
_SEPARATORS = {"|", "||", "&&", ";", "&", "(", ")"}
_REDIRECTS = {"<", ">", ">>"}


def _skip_prefixes(tokens: list[str], i: int) -> int:
    """Advance past sudo/env/assignment prefixes at a command position."""
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "sudo":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-u", "--user", "-g", "--group", "-h", "--host"):
                    i += 1  # flag consumes its argument
            continue
        if tok == "env":
            i += 1
            while i < n and (tokens[i].startswith("-") or _is_assignment(tokens[i])):
                i += 1
            continue
        if _is_assignment(tok):
            i += 1
            continue
        break
    return i


def _is_assignment(tok: str) -> bool:
    name, sep, _ = tok.partition("=")
    return bool(sep) and name.replace("_", "").isalnum() and not name[0].isdigit()


def _segment_denial(seg: list[str]) -> Optional[str]:
    """Credential path one command segment (between separators) opens.

    Command-position parsing (F1): prefixes (sudo/env/assignments) are
    skipped, a ``<`` input-redirection target counts as a file the
    command reads (``cat < x/.env``, incl. redirect-before-command
    ``< x/.env cat``), and ordinary reader arguments are inspected.
    A redirect for a NON-reader (``grep TOKEN < x/.env``) is not denied:
    only a listed reader triggers the deny."""
    i, n = 0, len(seg)
    pre_redirect: Optional[str] = None
    while i < n:
        if seg[i] in ("<<<", "<<"):
            i += 2  # here-string / heredoc: target is text, not a file
            continue
        if seg[i] in _REDIRECTS:
            if seg[i] == "<" and i + 1 < n:
                pre_redirect = seg[i + 1]
            i += 2
            continue
        i = _skip_prefixes(seg, i)
        if i >= n:
            return None
        if seg[i] in _REDIRECTS:
            continue
        break
    if i >= n:
        return None
    word = seg[i].replace("\\", "/").rsplit("/", 1)[-1]
    i += 1
    if word not in _READERS:
        return None
    if pre_redirect is not None and _is_credential_path(pre_redirect):
        return pre_redirect
    while i < n:
        tok = seg[i]
        if tok in ("<<<", "<<"):
            i += 2  # here-string / heredoc text, not a file argument
            continue
        if tok in _REDIRECTS:
            op = tok
            i += 1
            if i < n:
                target = seg[i]
                i += 1
                if op == "<" and _is_credential_path(target):
                    return target
            continue
        i += 1
        if tok == "--":
            continue
        if tok.startswith("-"):
            continue
        if _is_credential_path(tok):
            return tok
    return None


def _bash_denial(command: str) -> Optional[str]:
    """Credential path a reader command would open, else None.

    The command is split at command separators (newline, ``;``, ``&&``,
    ``||``, ``|``, ``&``) as TOKENS — quoted separators stay inside
    their token and can never create a command position (F1)."""
    segments: list[list[str]] = [[]]
    for tok in _tokens(command):
        if tok in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    for seg in segments:
        hit = _segment_denial(seg)
        if hit is not None:
            return hit
    return None


def _block_prompt(classes: list[str]) -> int:
    reason = (
        "swarph secret-guard (#1642): prompt appears to contain "
        + ", ".join(classes)
        + " credential material; remove it before submitting "
        "(the matched value is never shown)."
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _allow_not_fully_scanned() -> int:
    """Fail open on scan-budget expiry (S, #5670): allow, but never
    silently — the output carries a systemMessage saying so."""
    json.dump({
        "systemMessage": (
            "swarph secret-guard (#1642): prompt was not fully "
            "scanned (scan time budget exceeded); it was allowed "
            "without a complete secret check."
        )
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _deny_bash(path: str) -> int:
    # F3: name the path CLASS only — never the file's base name.
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "swarph secret-guard (#1642): reading a "
                f"{_path_class(path)} with a file-reading command is denied; "
                "use a redacted summary instead. File contents are never shown."
            ),
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_hook(stdin_text: Optional[str]) -> int:
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    raw = (raw or "").lstrip("\ufeff")
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("hook payload is not a JSON object")

    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""

    if event == "UserPromptSubmit" or (not event and "prompt" in payload):
        prompt = payload.get("prompt") or ""
        if not isinstance(prompt, str):
            raise ValueError("UserPromptSubmit prompt is not a string")
        if not prompt:
            return 0
        if _is_harness_turn(payload, prompt):
            return 0
        classes, complete = _detect_classes(prompt)
        if classes:
            return _block_prompt(classes)
        return 0 if complete else _allow_not_fully_scanned()

    if event == "PreToolUse":
        tool = payload.get("tool_name") or payload.get("toolName") or ""
        if tool != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not isinstance(command, str):
            raise ValueError("PreToolUse command is not a string")
        hit = _bash_denial(command)
        return _deny_bash(hit) if hit else 0

    return 0


def run_hook(stdin_text: Optional[str] = None) -> int:
    """Fail open on ANY crash: warn on STDERR (exception type only — the
    message could carry prompt text), emit nothing that blocks, exit 0."""
    try:
        return _run_hook(stdin_text)
    except Exception as exc:
        print(
            f"swarph secret-guard: warning: hook failed "
            f"({type(exc).__name__}); allowing (fail-open)",
            file=sys.stderr,
        )
        return 0


def run_secret_guard(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="swarph secret-guard",
        description="#1642 secret guard: block secrets in prompts and "
                    "credential-file reads in Bash (fails open on crash).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="Claude Code hook body (reads the payload on stdin)")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        return run_hook()
    return 0
