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

Rework #5667 (P4/C/T/I/E):

  P4  a key=value / Bearer hit whose VALUE is not secret-shaped (a bare
      Python identifier naming a variable, ``None``/``true``/``false``,
      or the literal word ``token`` after Bearer) does not block. This is
      a guard-level post-filter on redact's hits by value shape — redact
      stays the only credential pattern source, no second pattern set.
      A secret-shaped value after the same keys still blocks, and a
      value redact classes as a KNOWN credential is never filtered.
  C   Bash tokenisation runs shlex with NO comment characters, so a
      ``#`` comment line, a trailing ``#`` comment, or a ``#`` inside a
      word on an earlier line can never swallow the newline that makes
      a following reader command a new command. A here-string
      (``cat <<< .env``) or an echo that only prints ``.env`` text is
      not a file read and is allowed.
  T   Work is capped BEFORE redact runs: prompts longer than
      ``_MAX_REDACT_CHARS`` (16384) characters are NOT passed to redact
      at all and fail open (allowed, no block), consistent with this
      guard's crash policy. Redact's CLI patterns are quadratic on
      pathological repeated-word input, so an unbounded prompt could
      otherwise exceed the 5-second hook timeout.
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
from typing import Optional

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

# T (#5667): cap BEFORE redact. Prompts longer than this are not passed
# to redact at all and fail open (allowed) — see module docstring.
_MAX_REDACT_CHARS = 16384


def _classes_of(text: str) -> list[str]:
    """Credential classes redact finds in ``text``, in redact's own order.

    Detection is forced with opt-out disabled: SWARPH_REDACT_OPTOUT and
    opt-out events are an emission feature of redact, not a guard bypass.
    Built ONLY on redact's layers — no second pattern set lives here.
    """
    if len(text) > _MAX_REDACT_CHARS:
        return []
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
# P4 (#5667): value-shape post-filter for redact's key=value / Bearer hits.
_GENERIC_ASSIGN_RE = re.compile(
    r"(?P<prefix>\b[A-Za-z0-9_-]+\s*[:=]\s*[\"']?)"
    r"(?P<val>[A-Za-z0-9_][A-Za-z0-9_\-./+~=]*)"
    r"(?P<quote>[\"']?)"
)
_BEARER_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>\bBearer\s+)(?P<val>[A-Za-z0-9_\-._~+/]+={0,2})"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NONSECRET_LITERALS = {"none", "true", "false", "null", "undefined", "nil", "token"}

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


def _is_nonsecret_value(value: str) -> bool:
    """True when a key=value / Bearer value is not secret-shaped (P4):
    a literal None/true/false/token, or a bare Python identifier naming
    a variable (snake_case of any length that is not itself a standalone
    secret, or a short identifier). Known credentials never qualify."""
    v = value.strip("\"'")
    if v.lower() in _NONSECRET_LITERALS:
        return True
    if not _IDENTIFIER_RE.fullmatch(v):
        return False
    classes = _classes_of(v)
    if classes and classes != ["high-entropy secret"]:
        return False
    if "_" in v:
        standalone = getattr(redact, "_is_standalone_secret", None)
        return not (callable(standalone) and standalone(v))
    return len(v) < 16


def _strip_nonsecret_values(prompt: str) -> str:
    """Post-filter redact's generic key=value / Bearer hits by value
    shape (P4): replace only non-secret-shaped values with ``ID`` so
    redact no longer fires on them. Secret-shaped values are left
    untouched and still block. No credential patterns live here."""

    def _assign_sub(m: re.Match) -> str:
        key = re.split(r"[:=]", m.group("prefix"))[0].strip("\"' ").lower()
        if not any(word in key for word in (
                "token", "secret", "key", "pass", "cred", "auth",
                "private", "pwd")):
            return m.group(0)
        if _is_nonsecret_value(m.group("val")):
            # Drop the whole assignment: any replacement value after a
            # DSN key (api_key=ID) would itself be a redact DSN hit.
            return "ID"
        return m.group(0)

    def _bearer_sub(m: re.Match) -> str:
        if _is_nonsecret_value(m.group("val")):
            return f"{m.group('prefix')}ID"
        return m.group(0)

    return _GENERIC_ASSIGN_RE.sub(
        _assign_sub, _BEARER_VALUE_RE.sub(_bearer_sub, prompt))


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


def _detect_classes(prompt: str) -> list[str]:
    classes = _classes_of(prompt)
    if not classes:
        return []
    scrubbed = _strip_nonsecret_values(_strip_id_values(prompt))
    if scrubbed == prompt:
        return classes
    # If every hit came from an id-shaped or non-secret-shaped value,
    # it was identifiers / non-secret values, not credentials.
    return _classes_of(scrubbed)


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
        classes = _detect_classes(prompt)
        return _block_prompt(classes) if classes else 0

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
