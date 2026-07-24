"""The FIXED distance engine — ported VERBATIM (math unchanged) from the
reference ``bench_quality.py`` (``d_numeric/d_categorical/d_ranking/d_text``,
``parse_answer``, ``score``). See spec §2.

Every score is a DISTANCE in [0, 1] where 0 = perfect. The engine is fixed and
domain-agnostic — a pack never ships code, only data (``system`` + tasks +
expected answers); this module is the ONLY place distance math lives.

**Parse-fail -> distance = 1.0** is a load-bearing rule, not an edge case: a
model that can't follow the structured-output instruction gets the worst
possible score, because that IS a real quality signal (findings.md).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

# ---- distance functions (0 = exact, 1 = worst) -----------------------------


def d_numeric(provided: Any, expected: Any) -> float:
    """Capped relative error. ``e == 0`` is a special case: 0.0 if ``p == 0``
    else 1.0 (relative error is undefined at zero)."""
    try:
        p, e = float(provided), float(expected)
    except (TypeError, ValueError):
        return 1.0
    if e == 0:
        return 0.0 if p == 0 else 1.0
    return min(abs(p - e) / abs(e), 1.0)


def d_categorical(provided: Any, expected: Any) -> float:
    """0 if ``upper(strip(provided)) == upper(strip(expected))`` else 1."""
    return 0.0 if str(provided).strip().upper() == str(expected).strip().upper() else 1.0


def d_ranking(provided: Any, expected: Any) -> float:
    """Normalized Kendall-tau distance on the ranked list (0 = same order,
    1 = fully reversed). Wrong item SET (not a list, or items don't match the
    expected set) -> 1.0 (worst), per spec §2."""
    if not isinstance(provided, list) or sorted(map(str, provided)) != sorted(map(str, expected)):
        return 1.0
    pos = {str(x): i for i, x in enumerate(provided)}
    exp = [str(x) for x in expected]
    n = len(exp)
    if n < 2:
        return 0.0
    discord = sum(1 for i in range(n) for j in range(i + 1, n) if pos[exp[i]] > pos[exp[j]])
    return discord / (n * (n - 1) / 2)


def d_text(provided: Any, expected: Any, embedder: Optional[Callable[[str], Any]] = None) -> float:
    """Free-text distance: ``1 - Jaccard(tokens(p), tokens(e))`` — a cheap, $0,
    deterministic proxy with no external dependency (v1 default, spec §2 /
    decision #2).

    ``embedder`` is a clearly-marked SEAM for an optional embedding-cosine
    distance (e.g. gbrain's $0 embeddings) — NOT wired in v1. When provided it
    must be a ``str -> vector`` callable; cosine distance replaces Jaccard.
    Left unimplemented deliberately: gbrain is not coupled here yet.
    """
    if embedder is not None:
        raise NotImplementedError(
            "d_text(embedder=...) is a seam for a future gbrain-cosine embedder; "
            "not wired in v1 — call d_text(provided, expected) without embedder."
        )
    pa = set(str(provided).lower().split())
    ea = set(str(expected).lower().split())
    if not ea:
        return 0.0 if not pa else 1.0
    union = len(pa | ea)
    return 1.0 - (len(pa & ea) / union if union else 0.0)


DISTANCE: dict[str, Callable[[Any, Any], float]] = {
    "numeric": d_numeric,
    "categorical": d_categorical,
    "ranking": d_ranking,
    "text": d_text,
}


# ---- answer extraction ------------------------------------------------------


def parse_answer(text: Optional[str]) -> Any:
    """Pull the LAST ``{"answer": ...}`` JSON object from a model reply
    (tolerant of code fences / surrounding prose). Returns ``None`` on parse
    failure — the caller (``score``) turns that into distance=1.0."""
    if not text:
        return None
    last = None
    found = False
    for m in re.finditer(r'\{[^{}]*"answer"[^{}]*\}', text, re.DOTALL):
        try:
            last = json.loads(m.group(0)).get("answer")
            found = True
        except Exception:
            continue
    return last if found else None


def score(task: dict, text: str) -> dict:
    """-> ``{distance, parsed, parse_ok}``.

    ``numeric``/``categorical``/``ranking`` are parsed from the structured
    ``{"answer": ...}`` JSON; ``text`` scores the raw response verbatim.
    Unparseable structured answers are the worst score (1.0) — a real quality
    signal, not a bug (spec §2).
    """
    ttype = task["type"]
    if ttype == "text":
        ans = text or ""
        dist = d_text(ans, task["expected"])
        return {"distance": round(dist, 4), "parsed": ans, "parse_ok": True}
    ans = parse_answer(text)
    if ans is None:
        return {"distance": 1.0, "parsed": None, "parse_ok": False}
    dist = DISTANCE[ttype](ans, task["expected"])
    return {"distance": round(dist, 4), "parsed": ans, "parse_ok": True}
