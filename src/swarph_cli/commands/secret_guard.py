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


def _detect_classes(prompt: str) -> list[str]:
    """Credential classes redact finds in ``prompt``, in redact's own order.

    Detection is forced with opt-out disabled: SWARPH_REDACT_OPTOUT and
    opt-out events are an emission feature of redact, not a guard bypass.
    Built ONLY on redact's layers — no second pattern set lives here.
    """
    saved_env = os.environ.pop("SWARPH_REDACT_OPTOUT", None)
    ctx = getattr(redact, "_OPT_OUT_CTX", None)
    token = ctx.set(False) if ctx is not None else None
    try:
        redacted = redact.redact_text(prompt, caller="secret-guard")
    finally:
        if token is not None:
            ctx.reset(token)
        if saved_env is not None:
            os.environ["SWARPH_REDACT_OPTOUT"] = saved_env
    if not isinstance(redacted, str) or redacted == prompt:
        return []
    classes: list[str] = []
    for marker in _MARKER_RE.findall(redacted):
        label = _CLASS_BY_MARKER.get(marker, "credential")
        if label not in classes:
            classes.append(label)
    # redact changed the text but emitted no marker we know: still a hit.
    return classes or ["credential"]


def _is_credential_path(arg: str) -> bool:
    base = arg.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return (
        fnmatch.fnmatchcase(base, "*.env")
        or fnmatch.fnmatchcase(base, "*.key")
        or base == "auth.json"
        or base == "hosts.yml"
    )


def _tokens(command: str) -> list[str]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return command.split()


_OPERATORS = {"|", "||", "&&", ";", "&", "(", ")", "<", ">", ">>"}


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


def _bash_denial(command: str) -> Optional[str]:
    """Credential path a reader command would open, else None."""
    tokens = _tokens(command)
    i, n = 0, len(tokens)
    at_command = True
    while i < n:
        tok = tokens[i]
        if tok in _OPERATORS:
            at_command = True
            i += 1
            continue
        if at_command:
            i = _skip_prefixes(tokens, i)
            if i >= n:
                break
            word = tokens[i].replace("\\", "/").rsplit("/", 1)[-1]
            i += 1
            at_command = False
            if word not in _READERS:
                continue
            # Reader: inspect its file arguments until the next operator.
            while i < n and tokens[i] not in _OPERATORS:
                arg = tokens[i]
                i += 1
                if arg == "--":
                    continue
                if arg.startswith("-"):
                    continue
                if _is_credential_path(arg):
                    return arg
            continue
        i += 1
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
    base = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "swarph secret-guard (#1642): reading a credential file "
                f"({base}) with a file-reading command is denied; "
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
