"""``swarph codegraph-hook`` — structural-search companion for Claude Code.

Card #194 (PostToolUse/Bash annotate) + card #825 (commander redirect 2026-09-11):
UserPromptSubmit fires WHILE tool choice is still open — PostToolUse can only
comment on a decision already made. Lab measured ~20 greps annotated and 0
redirects in one session; the trigger move is the load-bearing half of
"work more like gbrain hook".

>>> IT SUPPLEMENTS, IT NEVER BLOCKS. <<< grep is genuinely correct for config,
logs, /etc and string literals; the codegraph indexes SYMBOLS.

>>> AND IT MUST NEVER DEGRADE SILENTLY. <<< A missing index makes a structural
query return `[]` — indistinguishable from a real negative. An unavailable
backend is reported LOUDLY as an incident, never as "no matches".

#825 order: (0) UserPromptSubmit on coding keywords, (1) audit JSONL including
the counterfactual (did the session later grep / call codegraph / neither),
(2/3) skip + relevance floor on prompt-derived terms — the 60% regex-retention
rule is DELETED for prompts (kept only for the PostToolUse shred path),
(4) no score constant. PostToolUse/Bash stays initially so both triggers share
one audit series; drop it later if audit shows zero influence.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from swarph_cli.gateway_default import env_gateway

TIMEOUT_S = 6
MAX_ROWS = 6

# Identifier token — used for prompt terms and for the Bash shred skip (no
# useful symbol-shaped token left after cleaning).
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Coding-keyword gate for UserPromptSubmit (#825 NEW 0). Deliberately broad;
# audit settles whether it over/under-fires.
_CODING_KW = re.compile(
    r"(?i)\b("
    r"def|class|function|method|caller|callers|symbol|implement|refactor|"
    r"bug|stack|traceback|exception|import|module|interface|struct|typedef|"
    r"codegraph|blast[- ]?radius|who\s+calls|where\s+is|src/|\.py\b"
    r")\b"
)

_PROMPT_STOP = {
    "def", "class", "async", "the", "for", "and", "with", "from", "this",
    "that", "what", "where", "when", "how", "who", "why", "please", "need",
    "want", "just", "into", "about", "have", "does", "code", "file", "line",
}

# ── PostToolUse/Bash trigger heuristics (#194) ────────────────────────────

_AT_COMMAND_POS = re.compile(r"(?:^|[|;&]|\$\()\s*(?:sudo\s+)?(?:grep|rg)\s")
_HEREDOC = re.compile(r"<<-?['\"]?\w*(?:EOF|PY)")
_CODEISH = re.compile(
    r"\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|c|h|cpp)\b|--include|(?:^|\s)-r\s|src/"
)


def _looks_like_code_search(cmd: str) -> bool:
    if not _AT_COMMAND_POS.search(cmd):
        return False
    if _HEREDOC.search(cmd):
        return False
    return bool(_CODEISH.search(cmd))


def extract_term(cmd: str) -> Optional[str]:
    pair = extract_raw_and_cleaned(cmd)
    return pair[1] if pair else None


def extract_raw_and_cleaned(cmd: str) -> Optional[tuple[str, str]]:
    m = _AT_COMMAND_POS.search(cmd)
    if not m:
        return None
    tail = cmd[m.end():]
    for tok in _tokenize(tail):
        if tok.startswith("-"):
            continue
        raw = tok.strip("'\"")
        cleaned = re.sub(r"[^\w\s.]", " ", raw).strip()
        if not cleaned:
            return None
        return (raw, cleaned)
    return None


def term_is_shredded(raw: str, cleaned: str) -> bool:
    """PostToolUse shred skip — regex debris with no identifier-shaped token.

    The 60%-retention guess from the pre-redirect spec is NOT used here either
    for prompts (#825: deleted unless audit says otherwise). Bash path skips
    when no token >=3 matches ``[A-Za-z_][A-Za-z0-9_]*``.
    """
    if not (raw or "").strip():
        return True
    toks = [t for t in _IDENT_TOKEN.findall(cleaned or "") if len(t) >= 3]
    return not toks


def prompt_has_coding_keywords(prompt: str) -> bool:
    return bool(_CODING_KW.search(prompt or ""))


def extract_prompt_terms(prompt: str, limit: int = 3) -> list[str]:
    """Identifier-shaped tokens from the prompt, longest first — no score constant."""
    found = []
    seen = set()
    for t in _IDENT_TOKEN.findall(prompt or ""):
        low = t.lower()
        if len(t) < 3 or low in _PROMPT_STOP or low in seen:
            continue
        seen.add(low)
        found.append(t)
    found.sort(key=len, reverse=True)
    return found[:limit]


def _tokenize(s: str) -> list:
    out, cur, quote, started = [], "", None, False
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "'\"":
            quote, started = ch, True
        elif ch.isspace():
            if cur or started:
                out.append(cur)
                cur, started = "", False
        else:
            cur += ch
            started = True
    if cur or started:
        out.append(cur)
    return out


# ── paths / audit / pending counterfactual ────────────────────────────────

def _token_path(self_name: str) -> Path:
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


def _audit_path(self_name: str) -> Path:
    return Path.home() / "swarph_state" / self_name / "codegraph-hook-audit.jsonl"


def _pending_path(self_name: str) -> Path:
    """Append-only JSONL (#829) — unlocked RMW on a shared JSON file lost rows."""
    return Path.home() / "swarph_state" / self_name / "codegraph-hook-pending.jsonl"


# Compact when this many closed rows have accumulated (#829 follow-up from lab):
# Stop re-scans the whole file every turn; dead closed lines are unbounded otherwise.
# Rewrite to open rows only — never a tail bound (that was the lossy shape we removed).
_PENDING_CLOSED_COMPACT = 200


def write_audit(self_name: str, record: dict) -> None:
    if not self_name:
        return
    path = _audit_path(self_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               **record}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _append_pending(self_name: str, record: dict) -> None:
    path = _pending_path(self_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               **record}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _rewrite_pending_opens(self_name: str, opens: list) -> None:
    """Drop closed dead weight — keep only still-open rows (append-only safe)."""
    path = _pending_path(self_name)
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            for r in opens:
                row = {
                    "ts": r.get("ts") or datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                    "kind": "open",
                    "firing_id": r.get("firing_id"),
                    "session_id": r.get("session_id") or "",
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _open_pendings(self_name: str) -> list:
    """Open firings not yet closed — scan append-only JSONL (#829).

    When closed entries exceed ``_PENDING_CLOSED_COMPACT``, rewrite the file to
    its live open rows so Stop's per-turn scan stays O(open), not O(history).
    """
    path = _pending_path(self_name)
    closed: set = set()
    opens: list = []
    n_closed = 0
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = r.get("kind")
                if kind == "closed":
                    n_closed += 1
                    closed.add(r.get("firing_id"))
                elif kind == "open":
                    opens.append(r)
    except OSError:
        return []
    live = [o for o in opens if o.get("firing_id") not in closed]
    if n_closed >= _PENDING_CLOSED_COMPACT:
        _rewrite_pending_opens(self_name, live)
    return live


def register_pending_firing(self_name: str, firing_id: str, session_id: str) -> None:
    if not self_name:
        return
    _append_pending(self_name, {
        "kind": "open",
        "firing_id": firing_id,
        "session_id": session_id or "",
    })


def resolve_pending_outcome(self_name: str, subsequent: str,
                            session_id: str = "") -> None:
    """Append outcome rows for open prompt firings (#825 counterfactual).

    subsequent: grep | codegraph | neither

    #829: an empty incoming session_id must match NOTHING (not every row).
    Outcomes are labelled ``subsequent_window=turn`` because Stop fires per
    assistant turn — ``neither`` means no grep/codegraph before end of THAT
    turn, not "the session never used the graph".
    """
    if not self_name:
        return
    if not (session_id or "").strip():
        return
    wrote: set = set()
    for r in _open_pendings(self_name):
        if r.get("session_id") != session_id:
            continue
        fid = r.get("firing_id")
        if not fid or fid in wrote:
            continue
        wrote.add(fid)
        write_audit(self_name, {
            "kind": "outcome",
            "firing_id": fid,
            "subsequent": subsequent,
            "subsequent_window": "turn",
            "subsequent_means": (
                "no grep/codegraph before end of this assistant turn "
                "(Stop is per-turn, not per-session)"
            ),
            "session_id": session_id,
        })
        _append_pending(self_name, {
            "kind": "closed",
            "firing_id": fid,
            "subsequent": subsequent,
            "session_id": session_id,
        })


# ── gateway query ─────────────────────────────────────────────────────────

def query_gateway(term: str, gateway: str, token: str, limit: int = MAX_ROWS) -> dict:
    if not (gateway or "").strip():
        return {"error": "MESH_GATEWAY_URL is not set and swarph ships no default "
                         "gateway host (#578) — the graph was never asked"}
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/codegraph",
        data=json.dumps({"query": term, "limit": limit}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"HTTP {e.code}: {detail or e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _match_quality(term: str, rows: list) -> tuple:
    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", (term or "").lower())
            if len(t) > 2 and t not in {"def", "class", "async", "the", "for"}]
    if not toks:
        return ("", len(rows))
    key = max(toks, key=len)
    hits = sum(1 for r in rows if key in str(r.get("name", "")).lower())
    return (key, hits)


def any_symbol_name_contains_term(term: str, rows: list) -> bool:
    key, hits = _match_quality(term, rows)
    if not key:
        return bool(rows)
    return hits > 0


def render(term: str, env: dict) -> str:
    if "error" in env:
        return (f"CODEGRAPH UNAVAILABLE for '{term}' — {env['error']}\n"
                f"  >>> THIS IS NOT 'no matches'. The structural index could not be "
                f"consulted, so grep's answer is the ONLY answer you have. Treat "
                f"definitions/callers as UNVERIFIED.")

    rows = env.get("results") or []
    fresh = env.get("freshness") or []
    stale = [f for f in fresh if f.get("stale")]
    age = ""
    if fresh:
        hrs = max((f.get("index_age_hours") or 0) for f in fresh)
        age = f", index {hrs:.1f}h old"
        if stale:
            age += " ⚠ STALE — verify line numbers against the file"

    if rows and not any_symbol_name_contains_term(term, rows):
        return ""

    if not rows:
        body = ("  no structural matches (the index IS present and answered — "
                "a REAL negative)")
    else:
        body = "\n".join(
            f"  {r.get('repo')}/{r.get('file_path')}:{r.get('start_line')}  "
            f"{r.get('kind')} {r.get('name')}  callers={r.get('callers')}"
            for r in rows[:MAX_ROWS]
        )
    return (f"CODEGRAPH (structural{age}) for '{term}' — grep found text; this is "
            f"the symbol graph, incl. CALLER COUNTS grep cannot see:\n{body}\n\n"
            f"Use this for definitions/callers/blast-radius BEFORE reaching for "
            f"grep when the question is structural. grep remains correct for "
            f"config, logs and string literals the codegraph does not index.")


def _read_token(self_name: str, term: str, event_name: str) -> Optional[str]:
    if not self_name:
        _emit(f"CODEGRAPH SKIPPED for '{term}' — no cell identity (set SWARPH_SELF "
              f"or pass --as). Not a negative result: the graph was never asked.",
              event_name)
        return None
    tp = _token_path(self_name)
    try:
        return tp.read_text(encoding="utf-8").strip()
    except OSError as e:
        _emit(f"CODEGRAPH UNAVAILABLE for '{term}' — peer token unreadable at {tp}: "
              f"{e}. Not a negative result: the graph was never asked.",
              event_name)
        return None


def _query_and_emit(self_name: str, gateway: str, term: str, *,
                    trigger: str, event_name: str, raw_command: str = "",
                    raw_term: str = "", prompt: str = "",
                    session_id: str = "", firing_id: str = "") -> None:
    token = _read_token(self_name, term, event_name)
    if token is None:
        return
    env = query_gateway(term, gateway, token)
    rows = env.get("results") or [] if "error" not in env else []
    name_hit = any_symbol_name_contains_term(term, rows) if rows else False
    fid = firing_id or str(uuid.uuid4())
    write_audit(self_name, {
        "kind": "fire",
        "firing_id": fid,
        "trigger": trigger,
        "session_id": session_id,
        "raw_command": raw_command,
        "prompt_excerpt": (prompt or "")[:240],
        "extracted_term": term,
        "raw_term": raw_term or term,
        "skipped": False,
        "match_count": len(rows),
        "any_name_contains_term": name_hit,
        "error": env.get("error"),
    })
    if trigger == "prompt":
        register_pending_firing(self_name, fid, session_id)
    text = render(term, env)
    if text:
        _emit(text, event_name)


# ── entry ─────────────────────────────────────────────────────────────────

# Events this handler knows how to process. Anything else must be LOUD (#830):
# a silent fallthrough to the Bash path made binding-ahead-of-code invisible.
_SUPPORTED_HOOK_EVENTS = frozenset({
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "StopFailure",
})


def supported_hook_events() -> frozenset:
    """Advertise the event set this handler can process (#830 install gate)."""
    return _SUPPORTED_HOOK_EVENTS


def run_codegraph_hook(argv: Optional[list] = None) -> int:
    """ALWAYS exits 0 — must never fail a turn."""
    argv = list(argv or [])
    self_name = os.environ.get("SWARPH_SELF", "").strip()
    gateway = env_gateway()
    for i, a in enumerate(argv):
        if a == "--as" and i + 1 < len(argv):
            self_name = argv[i + 1]
        elif a == "--gateway" and i + 1 < len(argv):
            gateway = argv[i + 1]

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:  # noqa: BLE001
        return 0

    event = (payload.get("hook_event_name")
             or payload.get("hookEventName")
             or "")
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")

    # #830: unknown non-empty event → report, never silent default to Bash.
    supported = supported_hook_events()
    if event and event not in supported:
        known = ", ".join(sorted(supported))
        _emit(
            f"CODEGRAPH HOOK: unknown hook_event_name={event!r} — this running "
            f"swarph does not handle it (known: {known}). Likely a settings "
            f"binding ahead of its code (#830). Re-install after upgrading "
            f"swarph-cli, or remove the stale binding.",
            event,
        )
        return 0

    if event in ("Stop", "StopFailure"):
        resolve_pending_outcome(self_name or "_unknown", "neither", session_id)
        return 0

    if event == "UserPromptSubmit" or (
            not event and (payload.get("prompt") or payload.get("user_prompt"))):
        return _run_prompt_path(payload, self_name, gateway, session_id)

    # PostToolUse (or empty event with tool_input): Bash annotate path (#194).
    return _run_bash_path(payload, self_name, gateway, session_id)


def _run_prompt_path(payload: dict, self_name: str, gateway: str,
                     session_id: str) -> int:
    prompt = (payload.get("prompt")
              or payload.get("user_prompt")
              or payload.get("userPrompt")
              or "").strip()
    if not prompt:
        return 0
    if not prompt_has_coding_keywords(prompt):
        write_audit(self_name or "_unknown", {
            "kind": "fire",
            "trigger": "prompt",
            "session_id": session_id,
            "skipped": True,
            "skip_reason": "no_coding_keywords",
            "prompt_excerpt": prompt[:240],
            "match_count": 0,
            "any_name_contains_term": False,
        })
        return 0
    terms = extract_prompt_terms(prompt)
    if not terms:
        write_audit(self_name or "_unknown", {
            "kind": "fire",
            "trigger": "prompt",
            "session_id": session_id,
            "skipped": True,
            "skip_reason": "no_identifier_term",
            "prompt_excerpt": prompt[:240],
            "match_count": 0,
            "any_name_contains_term": False,
        })
        return 0
    # One query — longest identifier. No score constant; walk stays source order.
    term = terms[0]
    _query_and_emit(
        self_name, gateway, term,
        trigger="prompt", event_name="UserPromptSubmit",
        prompt=prompt, session_id=session_id,
    )
    return 0


def _run_bash_path(payload: dict, self_name: str, gateway: str,
                   session_id: str) -> int:
    cmd = ((payload.get("tool_input") or {}).get("command") or "").strip()
    if not cmd:
        return 0

    # Counterfactual: a later grep / codegraph call closes open prompt firings.
    low = cmd.lower()
    if "codegraph" in low and "swarph" in low:
        resolve_pending_outcome(self_name or "_unknown", "codegraph", session_id)
    elif _AT_COMMAND_POS.search(cmd):
        resolve_pending_outcome(self_name or "_unknown", "grep", session_id)

    if not _looks_like_code_search(cmd):
        return 0
    pair = extract_raw_and_cleaned(cmd)
    if not pair:
        return 0
    raw, term = pair

    if term_is_shredded(raw, term):
        write_audit(self_name or "_unknown", {
            "kind": "fire",
            "trigger": "bash",
            "session_id": session_id,
            "raw_command": cmd,
            "extracted_term": term,
            "raw_term": raw,
            "skipped": True,
            "skip_reason": "shredded_term",
            "match_count": 0,
            "any_name_contains_term": False,
        })
        return 0

    _query_and_emit(
        self_name, gateway, term,
        trigger="bash", event_name="PostToolUse",
        raw_command=cmd, raw_term=raw, session_id=session_id,
    )
    return 0


def _emit(text: str, event_name: str = "PostToolUse") -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }))
