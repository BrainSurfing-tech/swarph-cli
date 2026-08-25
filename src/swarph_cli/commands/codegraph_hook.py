"""``swarph codegraph-hook`` — PostToolUse(Bash) structural-search companion.

WHEN A grep/rg SEARCHES CODE, ALSO ASK THE CODEGRAPH, and hand the structural
answer back as context. Card #194.

WHY IT EXISTS. Agents reach for grep where the symbol graph is the right
instrument — lab-ovh was corrected three times in one session for exactly that,
and the third correction landed INSIDE the fix for the first two. Instructions
demonstrably do not hold at the moment of typing, so this is mechanical rather
than a reminder. The token argument is the commander's: a grep that returns 200
matching lines costs far more context than the six symbols that actually answer
"where is this defined and who calls it".

>>> IT SUPPLEMENTS, IT NEVER BLOCKS. <<< grep is genuinely correct for config,
logs, /etc and string literals; the codegraph indexes SYMBOLS. The failure this
guards is "I only ever saw grep's answer", not "grep was used".

>>> AND IT MUST NEVER DEGRADE SILENTLY. <<< A missing index makes a structural
query return `[]` — indistinguishable from a real negative, which is the exact
shape of the (gbrain unreachable) incident and of card #200's drain. So an
unavailable backend is reported LOUDLY as an incident, never as "no matches".

PEER MODE. Unlike lab-ovh's original local hook, a peer has no local index — it
asks the gateway's ``POST /codegraph`` proxy, which applies the A8 visibility
gate to rows AND to caller counts (swarph-cli PR #165). The caller identity comes
from the bearer token, never from a field, so it cannot be self-asserted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from swarph_cli.gateway_default import env_gateway

TIMEOUT_S = 6
MAX_ROWS = 6

# ── trigger heuristics ────────────────────────────────────────────────────
# Ported from the hook that ran on lab-ovh for a day. Every one of these three
# patterns exists because a LOOSER version misfired in production.

# 1. grep/rg must be at COMMAND POSITION — line start, or right after a pipe /
#    ; / && / $( . Matching "grep" after arbitrary whitespace fires on any command
#    whose text merely MENTIONS the word, including a heredoc body: the first live
#    firing did exactly that and extracted the heredoc delimiter as the search term.
_AT_COMMAND_POS = re.compile(r"(?:^|[|;&]|\$\()\s*(?:sudo\s+)?(?:grep|rg)\s")

# 2. A heredoc means most of the command is DATA, not shell — too noisy to parse.
_HEREDOC = re.compile(r"<<-?['\"]?\w*(?:EOF|PY)")

# 3. It should look like a CODE search, not a config/log one.
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
    """The search term ADJACENT to grep — not the first quoted string anywhere.

    A `gh pr close --comment "...'dead'..." && grep foo bar` fired a query for
    'dead', lifted out of unrelated prose. So the scan starts AT the grep token.
    """
    m = _AT_COMMAND_POS.search(cmd)
    if not m:
        return None
    tail = cmd[m.end():]
    # Skip flags (-n, -R, --include=*.py, …) to reach the pattern itself.
    for tok in _tokenize(tail):
        if tok.startswith("-"):
            continue
        term = tok.strip("'\"")
        # A regex-metachar soup is not a useful symbol query.
        cleaned = re.sub(r"[^\w\s.]", " ", term).strip()
        return cleaned or None
    return None


def _tokenize(s: str) -> list:
    """Shell-ish split that keeps quoted strings together.

    NOTE: no early break. An earlier version stopped after the FIRST completed
    token, so a command whose first argument is a flag (`grep -rn 'pat' src/`)
    yielded only `-rn`, the caller skipped it as a flag, and extraction returned
    None — the hook silently never fired on the most common grep shape there is.
    Caught by test_skips_flags_to_reach_the_pattern.
    """
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


# ── gateway query ─────────────────────────────────────────────────────────

def _token_path(self_name: str) -> Path:
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


def query_gateway(term: str, gateway: str, token: str, limit: int = MAX_ROWS) -> dict:
    """POST /codegraph. Returns the envelope, or {"error": "..."} — NEVER {}.

    >>> AN UNREACHABLE OR REFUSING BACKEND IS AN ERROR, NOT AN EMPTY RESULT. <<<
    Collapsing them is the defect this hook exists to avoid teaching.
    """
    # #578: with no baked-in host, `gateway` can be "". Request() rejects a
    # relative URL at CONSTRUCTION (below, outside the try), so an unconfigured
    # backend would raise ValueError and take the whole turn down — against this
    # entry point's "ALWAYS exits 0, must never fail a turn" contract.
    # UNCONFIGURED is the same class as UNREACHABLE: an error, not empty.
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
    except Exception as e:  # noqa: BLE001 — transport/parse
        return {"error": f"{type(e).__name__}: {e}"}


def _match_quality(term: str, rows: list) -> tuple:
    """Did the results actually match what was ASKED, or only a common token?

    >>> THE EMPTY ANSWER WAS MADE HONEST; THE NON-EMPTY ONE WAS NOT. <<<
    (Reported first-hand by a peer, 2026-08-01.) The index's query sanitiser
    OR-JOINS tokens, so `def command_beta_executor` matches anything containing
    "command". He grepped a private-repo file and got six confident-looking
    swarph-cli symbols WITH CALLER COUNTS — plausible structure from the wrong
    repository, and caller counts make it read authoritative. His words: "the
    failure mode you engineered out of the empty case walked back in through the
    non-empty one."

    So: find the query's DISTINCTIVE token — the longest content token, which is
    the one carrying the intent (`command_beta_executor`, not `def`) — and check
    whether ANY returned symbol name actually contains it. If none does, every
    row is a common-token coincidence and must be labelled as such rather than
    served as an answer.

    Returns (distinctive_token, n_rows_matching_it).
    """
    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", (term or "").lower())
            if len(t) > 2 and t not in {"def", "class", "async", "the", "for"}]
    if not toks:
        return ("", len(rows))
    key = max(toks, key=len)
    hits = sum(1 for r in rows if key in str(r.get("name", "")).lower())
    return (key, hits)


def render(term: str, env: dict) -> str:
    """Format the envelope for injection. Errors are LOUD and named."""
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

    if not rows:
        body = ("  no structural matches (the index IS present and answered — "
                "a REAL negative)")
    else:
        body = "\n".join(
            f"  {r.get('repo')}/{r.get('file_path')}:{r.get('start_line')}  "
            f"{r.get('kind')} {r.get('name')}  callers={r.get('callers')}"
            for r in rows[:MAX_ROWS]
        )
    warn = ""
    if rows:
        key, hits = _match_quality(term, rows)
        if key and hits == 0:
            repos = sorted({str(r.get("repo")) for r in rows})
            warn = (f"\n  >>> FUZZY MATCH — NOT AN ANSWER TO YOUR QUERY. No returned symbol's "
                    f"name contains {key!r}. These matched a COMMON TOKEN only"
                    + (f", and all are from: {', '.join(repos)}" if repos else "")
                    + f". Treat them as unrelated: the symbol you grepped for is NOT in "
                    f"what this index can see. <<<")
    return (f"CODEGRAPH (structural{age}) for '{term}' — grep found text; this is "
            f"the symbol graph, incl. CALLER COUNTS grep cannot see:\n{body}{warn}\n\n"
            f"Use this for definitions/callers/blast-radius. grep remains correct "
            f"for config, logs and string literals the codegraph does not index.")


def run_codegraph_hook(argv: Optional[list] = None) -> int:
    """PostToolUse(Bash) entry point. ALWAYS exits 0 — it must never fail a turn."""
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
    cmd = ((payload.get("tool_input") or {}).get("command") or "").strip()
    if not cmd or not _looks_like_code_search(cmd):
        return 0
    term = extract_term(cmd)
    if not term:
        return 0

    if not self_name:
        _emit(f"CODEGRAPH SKIPPED for '{term}' — no cell identity (set SWARPH_SELF "
              f"or pass --as). Not a negative result: the graph was never asked.")
        return 0
    tp = _token_path(self_name)
    try:
        # encoding="utf-8": on Windows a bare read_text() uses the locale codec, and a
        # non-ASCII byte in the token file crashes the hook with a charmap traceback
        # that names a credential path.
        token = tp.read_text(encoding="utf-8").strip()
    except OSError as e:
        _emit(f"CODEGRAPH UNAVAILABLE for '{term}' — peer token unreadable at {tp}: "
              f"{e}. Not a negative result: the graph was never asked.")
        return 0

    _emit(render(term, query_gateway(term, gateway, token)))
    return 0


def _emit(text: str) -> None:
    """Hand context back to the model via the documented hook JSON shape."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }))
