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
      where they fit (carrying trailing lines up to ``_CHUNK_OVERLAP``
      across line-batch boundaries) and cutting long lines with a
      ``_CHUNK_OVERLAP`` (512) character overlap, so a key straddling
      a cut is still seen in full in the neighbouring chunk (512 far
      exceeds the longest fixed credential-pattern context redact
      needs: the 21-char ``aws_secret_access_key`` key plus separator
      and its 20-char minimum value is 44 chars; Bearer needs 19).
      R1 (#5735): long-line cuts snap backward within
      ``_BOUNDARY_WINDOW`` (256) to non-token boundaries (with
      oversized-id pre-scrub) so generated ``toolu_``/``msg_``/``req_``
      and UUID identifiers never split mid-token into false
      high-entropy fragments.
      R2 (#5735): a ``-----BEGIN ... -----END-----`` PEM unit of at
      most ``_MAX_PEM_CHARS`` (8192) characters is kept whole; larger
      ``BEGIN..END`` spans are sliced like ordinary lines so slow lines
      between ``BEGIN`` and ``END`` obey chunking and the time budget.
      G3 (#5753) & #1678 (R1-R4, #5867): the PEM marker pre-pass and
      unit splitter each scan markers in one pass via
      ``_PEM_MARKER_SCAN_RE.finditer`` (with a ``(?=-----)`` lookahead
      so adjacent markers sharing five dashes both match) under the
      shared ``_SCAN_BUDGET_SECONDS`` deadline with no 64-marker cap.
      ``_detect_classes`` scans all chunks via ``_iter_chunks`` first
      before invoking ``_pem_envelope_classes`` for oversized
      ``> _MAX_PEM_CHARS`` PEM spans, and ``_pem_envelope_classes``
      evaluates single-label pairs up to ``_MAX_PEM_CHARS`` (R4),
      checks the smaller marker side first, tracks actually
      ``classified`` labels before filtering the second side, and
      classifies every label on a side without a 32-batch stop. The
      first hit blocks. Fail-open ONLY when a
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
      is one or more complete envelopes separated by whitespace, using
      bounded non-backtracking matchers (``_MAX_CHANNEL_TAG_CHARS = 4096``
      for ``<channel ...>`` opening tags, R3 #1678).
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
# most this many chars (a kept-whole PEM block may exceed it up to _MAX_PEM_CHARS).
_CHUNK_CHARS = 2048
# Overlap when a single long line must be cut: sized so a credential
# pattern straddling a cut is fully present in a neighbouring chunk.
# Longest fixed context redact needs is ~44 chars (see docstring).
_CHUNK_OVERLAP = 512
# Fail-open only when this budget runs out mid-scan (with systemMessage).
_SCAN_BUDGET_SECONDS = 3.0
# R2 (#5735): cap kept-whole PEM units at 8 KB (a 51-line PEM key is ~3.4 KB;
# an 8192-bit RSA key is ~6.4 KB). Larger BEGIN..END spans are sliced like
# ordinary lines so a slow line inside BEGIN..END cannot bypass chunking/budget.
_MAX_PEM_CHARS = 8192
# R1 (#5735): snap long-line cuts to non-token boundaries within this window
# so id-shaped tokens (toolu_/req_/msg_/UUID) are never cut mid-token.
_BOUNDARY_WINDOW = 256
_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/+-_.="
)


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
_MAX_CHANNEL_TAG_CHARS = 4096
_CHANNEL_SOURCE_RE = re.compile(r"\bsource\s*=")


class _SpanMatch:
    __slots__ = ("_end",)

    def __init__(self, end: int) -> None:
        self._end = end

    def end(self) -> int:
        return self._end


class _FixedEnvelopeMatcher:
    __slots__ = ("_open", "_close")

    def __init__(self, open_tag: str, close_tag: str) -> None:
        self._open = open_tag
        self._close = close_tag

    def match(self, prompt: str, pos: int) -> Optional[_SpanMatch]:
        if not prompt.startswith(self._open, pos):
            return None
        close = prompt.find(self._close, pos + len(self._open))
        if close == -1:
            return None
        return _SpanMatch(close + len(self._close))


class _ChannelEnvelopeMatcher:
    def match(self, prompt: str, pos: int) -> Optional[_SpanMatch]:
        if not prompt.startswith("<channel", pos):
            return None
        after = pos + 8
        if after >= len(prompt) or not prompt[after].isspace():
            return None
        gt = prompt.find(">", after + 1, after + 1 + _MAX_CHANNEL_TAG_CHARS)
        if gt == -1:
            return None
        if _CHANNEL_SOURCE_RE.search(prompt, after + 1, gt) is None:
            return None
        close = prompt.find("</channel>", gt + 1)
        if close == -1:
            return None
        return _SpanMatch(close + 10)


_ENVELOPE_RES = [
    _FixedEnvelopeMatcher("<task-notification>", "</task-notification>"),
    _FixedEnvelopeMatcher("<system-reminder>", "</system-reminder>"),
    _ChannelEnvelopeMatcher(),
]
_WS_RE = re.compile(r"\s*")
_PEM_MARKER_SCAN_RE = re.compile(r"-----(BEGIN|END) ([A-Z0-9 ]+)(?=-----)")
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
    n = len(prompt)
    pos = 0
    while pos < n and prompt[pos] == "\ufeff":
        pos += 1
    pos = _WS_RE.match(prompt, pos).end()
    if pos >= n:
        return False
    count = 0
    while pos < n:
        matched = False
        for pat in _ENVELOPE_RES:
            m = pat.match(prompt, pos)
            if m:
                pos = _WS_RE.match(prompt, m.end()).end()
                count += 1
                matched = True
                break
        if not matched:
            return False
    return count >= 1


def _pem_envelope_classes(
    text: str, deadline: Optional[float] = None
) -> tuple[list[str], bool]:
    """Scan for any BEGIN..END marker pair that redact's PEM layer flags,
    even when the body between them exceeds _MAX_PEM_CHARS (G3 #5753, R2/R4 #1678).
    """
    if deadline is None:
        deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    if "-----BEGIN " not in text or "-----END " not in text:
        return [], True

    first_begin: dict[str, int] = {}
    last_end: dict[str, int] = {}
    for step, m in enumerate(_PEM_MARKER_SCAN_RE.finditer(text)):
        if (step & 255) == 0 and time.monotonic() > deadline:
            return [], False
        kind = m.group(1)
        label = m.group(2)
        if kind == "BEGIN":
            if label not in first_begin:
                first_begin[label] = m.start()
        else:
            last_end[label] = m.start()

    if not first_begin or not last_end or min(first_begin.values()) >= max(last_end.values()):
        return [], True

    pk_labels: set[str] = set()
    classified: set[str] = set()
    batch: list[tuple[str, str]] = []
    batch_len = 0

    def _flush_batch() -> bool:
        nonlocal batch, batch_len
        if not batch:
            return True
        if time.monotonic() > deadline:
            return False
        if "PEM private key" in _classes_of("\n".join(pair for _, pair in batch)):
            for lbl, pair in batch:
                if time.monotonic() > deadline:
                    return False
                if "PEM private key" in _classes_of(pair):
                    pk_labels.add(lbl)
        classified.update(lbl for lbl, _ in batch)
        batch = []
        batch_len = 0
        return True

    def _classify_labels(labels_to_check: list[str]) -> bool:
        nonlocal batch, batch_len
        for lbl in labels_to_check:
            pair = f"-----BEGIN {lbl}-----\n-----END {lbl}-----"
            if len(pair) > _MAX_PEM_CHARS:
                continue
            added = len(pair) + (1 if batch else 0)
            if batch and batch_len + added > _CHUNK_CHARS:
                if not _flush_batch():
                    return False
                added = len(pair)
            batch.append((lbl, pair))
            batch_len += added
            if batch_len >= _CHUNK_CHARS:
                if not _flush_batch():
                    return False
        return _flush_batch()

    begin_labels = sorted(first_begin.keys())
    end_labels = sorted(last_end.keys())
    if len(begin_labels) <= len(end_labels):
        if not _classify_labels(begin_labels):
            return [], False
        if not (pk_labels & set(first_begin.keys())):
            return [], True
        if not _classify_labels([lbl for lbl in end_labels if lbl not in classified]):
            return [], False
    else:
        if not _classify_labels(end_labels):
            return [], False
        if not (pk_labels & set(last_end.keys())):
            return [], True
        if not _classify_labels([lbl for lbl in begin_labels if lbl not in classified]):
            return [], False

    b_hit = min(((pos, lbl) for lbl, pos in first_begin.items() if lbl in pk_labels), default=None)
    e_hit = max(((pos, lbl) for lbl, pos in last_end.items() if lbl in pk_labels), default=None)
    if b_hit is not None and e_hit is not None and b_hit[0] < e_hit[0]:
        if time.monotonic() > deadline:
            return [], False
        return [
            c for c in _classes_of(f"-----BEGIN {b_hit[1]}-----\n-----END {e_hit[1]}-----")
            if c == "PEM private key"
        ], True
    return [], True


def _atomic_units(text: str) -> Iterator[tuple[bool, str]]:
    """Yield ``(is_pem, unit)`` pieces lazily in a single linear pass (G3, #5753).

    A kept-whole PEM unit starts at the ``-----BEGIN`` marker itself (G2, #5744)
    and spans at most ``_MAX_PEM_CHARS`` chars through ``-----END...-----``.
    Plain text far behind the scan cursor flushes incrementally so early chunks
    are scanned without waiting for later markers.
    """
    n = len(text)
    plain_start = 0
    pending_begin: Optional[int] = None

    for m in _PEM_MARKER_SCAN_RE.finditer(text):
        m_start = m.start()
        if pending_begin is not None and (m_start - pending_begin) > _MAX_PEM_CHARS:
            pending_begin = None

        safe_end = pending_begin if pending_begin is not None else m_start
        if safe_end - plain_start >= _CHUNK_CHARS:
            nl = text.rfind("\n", plain_start, safe_end)
            flush_to = (nl + 1) if nl != -1 else safe_end
            if flush_to > plain_start:
                for line in text[plain_start:flush_to].splitlines(True):
                    yield False, line
                plain_start = flush_to

        if m.group(1) == "BEGIN":
            pending_begin = m_start
        else:
            m_end = m.end() + 5
            if pending_begin is not None and (m_end - pending_begin) <= _MAX_PEM_CHARS:
                b = pending_begin
                pending_begin = None
                pem_end = m_end
                if pem_end < n and text[pem_end] == "\r":
                    pem_end += 1
                if pem_end < n and text[pem_end] == "\n":
                    pem_end += 1
                if b > plain_start:
                    for line in text[plain_start:b].splitlines(True):
                        yield False, line
                yield True, text[b:pem_end]
                plain_start = pem_end
            else:
                pending_begin = None

    if plain_start < n:
        for line in text[plain_start:].splitlines(True):
            yield False, line


def _slice_long(unit: str) -> Iterator[str]:
    """Slice a >``_CHUNK_CHARS`` unit on non-token boundaries within
    ``_BOUNDARY_WINDOW`` (R1, #5735)."""
    n = len(unit)
    start = 0
    while start < n:
        end = min(start + _CHUNK_CHARS, n)
        if end < n and unit[end - 1] in _TOKEN_CHARS and unit[end] in _TOKEN_CHARS:
            lo = max(start + _CHUNK_CHARS - _BOUNDARY_WINDOW, start + _CHUNK_OVERLAP + 1)
            for k in range(end, lo - 1, -1):
                if unit[k - 1] not in _TOKEN_CHARS:
                    end = k
                    break
        yield unit[start:end]
        if end >= n:
            break
        next_start = end - _CHUNK_OVERLAP
        if next_start > 0 and unit[next_start - 1] in _TOKEN_CHARS and unit[next_start] in _TOKEN_CHARS:
            lo = max(start + 1, next_start - _BOUNDARY_WINDOW)
            for k in range(next_start, lo - 1, -1):
                if unit[k - 1] not in _TOKEN_CHARS:
                    next_start = k
                    break
        start = next_start


def _carry_tail(buf: str) -> str:
    """Carry the last ``_CHUNK_OVERLAP`` chars across a batch flush, snapped
    backward within ``_BOUNDARY_WINDOW`` so the carry never starts mid-token
    (R-2, #5744)."""
    if len(buf) <= _CHUNK_OVERLAP:
        return buf
    cut = len(buf) - _CHUNK_OVERLAP
    if cut > 0 and buf[cut - 1] in _TOKEN_CHARS and buf[cut] in _TOKEN_CHARS:
        lo = max(0, cut - _BOUNDARY_WINDOW)
        for k in range(cut, lo - 1, -1):
            if k == 0 or buf[k - 1] not in _TOKEN_CHARS:
                cut = k
                break
    return buf[cut:]


def _iter_chunks(text: str) -> Iterator[str]:
    """Yield chunks of at most ``_CHUNK_CHARS`` chars (S #5670, R1/R2 #5735):
    whole lines are kept together where they fit (carrying the last
    ``_CHUNK_OVERLAP`` chars across batch boundaries), a single long line is
    cut at non-token boundaries with at least ``_CHUNK_OVERLAP`` chars of
    overlap, and a PEM block of at most ``_MAX_PEM_CHARS`` is yielded whole."""
    buf = ""
    dirty = False
    for is_pem, unit in _atomic_units(text):
        if is_pem:
            if dirty and buf:
                yield buf
            buf = ""
            dirty = False
            yield unit
            continue
        combined = (buf + unit) if buf else unit
        if len(combined) <= _CHUNK_CHARS:
            buf = combined
            dirty = True
            continue
        if len(unit) <= _CHUNK_CHARS and dirty:
            yield buf
            carried = _carry_tail(buf) + unit
            if len(carried) <= _CHUNK_CHARS:
                buf = carried
                dirty = True
                continue
            combined = carried
        for piece in _slice_long(combined):
            yield piece
        buf = _carry_tail(combined)
        dirty = False
    if dirty and buf:
        yield buf


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
    if time.monotonic() > deadline:
        return [], False
    for chunk in _iter_chunks(prompt):
        if time.monotonic() > deadline:
            return [], False
        classes = _chunk_classes(chunk)
        if classes:
            return classes, True
    pem_classes, complete = _pem_envelope_classes(prompt, deadline)
    if not complete:
        return [], False
    if pem_classes:
        return pem_classes, True
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
