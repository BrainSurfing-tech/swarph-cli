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
rather than accidental. Identifier scrubbing (``_strip_id_values``) is
likewise context-anchored (id-labelled or id-tagged values only) and
fail-closed: a value redact classes as a KNOWN credential is never
scrubbed, so ``request_id: sk-ant-…`` still blocks.
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


def _classes_of(text: str) -> list[str]:
    """Credential classes redact finds in ``text``, in redact's own order.

    Detection is forced with opt-out disabled: SWARPH_REDACT_OPTOUT and
    opt-out events are an emission feature of redact, not a guard bypass.
    Built ONLY on redact's layers — no second pattern set lives here.
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

_CHANNEL_OPEN_RE = re.compile(r"<channel\s[^>]*\bsource\s*=")
_ID_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]*-[Ii][Dd])>([^<]{1,200})</\1>")
_ID_LABELLED_RE = re.compile(
    r"(?P<prefix>[\"']?[A-Za-z0-9_-]+[\"']?\s*[:=]\s*[\"'])"
    r"(?P<val>[A-Za-z0-9_\-./+=]{8,})(?P<quote>[\"'])"
    r"|(?P<prefix2>[\"']?[A-Za-z0-9_-]+[\"']?\s*[:=]\s*)"
    r"(?P<val2>[A-Za-z0-9_\-./+=]{8,})"
    r"|(?P<prefix3>(?i:tool[- ]use[- ]id|request[- ]id|message[- ]id"
    r"|session[- ]id|prompt[- ]id)\s+[\"'])"
    r"(?P<val3>[A-Za-z0-9_\-./+=]{8,})(?P<quote3>[\"'])"
    r"|(?P<prefix4>(?i:tool[- ]use[- ]id|request[- ]id|message[- ]id"
    r"|session[- ]id|prompt[- ]id)\s+)"
    r"(?P<val4>[A-Za-z0-9_\-./+=]{8,})"
)

_HARNESS_SOURCE_VALUES = {
    "system", "harness", "channel", "task_notification",
    "task-notification", "peer",
}


def _is_id_key(key: str) -> bool:
    k = key.strip("\"'")
    low = k.lower()
    return low == "id" or low.endswith("_id") or low.endswith("-id") or k.endswith("Id")


def _scrubbable(value: str) -> bool:
    """An id value may be scrubbed only if redact does NOT class it as a
    known credential — scrubbing is fail-closed (high-entropy-only or
    clean values scrub; Anthropic/GitHub/JWT/PEM/... never do)."""
    classes = _classes_of(value)
    return not classes or classes == ["high-entropy secret"]


def _strip_id_values(prompt: str) -> str:
    """Replace id-context identifier values with ``ID`` (context-anchored:
    ``<…-id>…</…-id>`` tags and ``*_id[:=]`` / ``… id `` labels only).
    Bare tokens are never touched, so a bare secret still blocks."""

    def _tag_sub(m: re.Match) -> str:
        if _scrubbable(m.group(2)):
            return f"<{m.group(1)}>ID</{m.group(1)}>"
        return m.group(0)

    def _label_sub(m: re.Match) -> str:
        prefix = m.group("prefix") or m.group("prefix2") or ""
        val = m.group("val") or m.group("val2") or ""
        quote = m.group("quote") or ""
        if m.group("val3") is not None or m.group("val4") is not None:
            prefix = m.group("prefix3") or m.group("prefix4") or ""
            val = m.group("val3") or m.group("val4") or ""
            quote = m.group("quote3") or ""
        if m.group("prefix") is not None or m.group("prefix2") is not None:
            key = re.split(r"[:=]", prefix)[0]
            if not _is_id_key(key):
                return m.group(0)
        if _scrubbable(val):
            return f"{prefix}ID{quote}"
        return m.group(0)

    out = _ID_TAG_RE.sub(_tag_sub, prompt)
    return _ID_LABELLED_RE.sub(_label_sub, out)


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
    s = prompt.lstrip("\ufeff").lstrip()
    stripped = s.rstrip()
    if s.startswith("<task-notification>") and stripped.endswith("</task-notification>"):
        return True
    if s.startswith("<system-reminder>") and stripped.endswith("</system-reminder>"):
        return True
    if _CHANNEL_OPEN_RE.match(s) and stripped.endswith("</channel>"):
        return True
    return False


def _detect_classes(prompt: str) -> list[str]:
    classes = _classes_of(prompt)
    if not classes:
        return []
    scrubbed = _strip_id_values(prompt)
    if scrubbed == prompt:
        return classes
    # If every hit came from an id-context value, it was identifiers.
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
