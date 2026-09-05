"""ENRICH. The only stage that uses a model, and it may not write.

R4/GC5: the client resolves its own endpoint (env -> storage_hub -> default).
Measured 2026-09-03: reachable, model nemotron-mini:4b, local, $0 per run.
Never hardcode the address -- #578.
"""
from __future__ import annotations
import json
from pathlib import Path

PROMPT = ("Here is what one agent session was recently DOING:\n%s\n\n"
          "Here are the memory files that exist:\n%s\n\n"
          "Propose links between files that record the SAME underlying lesson, "
          "weighting what the session above was actually working on. Reply with a "
          "JSON array of {\"file\":..., \"link\":..., \"why\":...} and nothing else.")


def _client():
    from workers.slm_client import SLMClient
    return SLMClient()


def enrich(clone: Path, records: list[dict], client=None) -> list[dict]:
    # `generate`, NOT `generate_json`. Both exist on SLMClient (lines 78 and 118,
    # read 2026-09-03). generate_json coerces its result to a DICT: given a JSON
    # array it returns only the FIRST element, because small models wrap single
    # objects in arrays and that coercion was added to fix a 2026-07-13 crash.
    # Enrichment legitimately returns MANY proposals, so generate_json would
    # silently drop all but one -- a partial result wearing a success.
    # >>> `records` IS THE ENRICH SIGNAL. <<< An earlier draft took this parameter
    # and never read it, building its prompt from `clone.glob("*.md")` -- filenames
    # only. That made Task 5 a producer with ZERO CONSUMERS: a 1.26 GB reader whose
    # output went to a function that discarded it, and it would have passed every
    # test in its own task because the cursor arithmetic was correct.
    #
    # WHY THE TRANSCRIPT AND NOT THE CORPUS: filenames say what EXISTS; the
    # transcript says what the cell was DOING when it wrote them. Two memories are
    # related because ONE SESSION PRODUCED BOTH while working one problem -- and
    # that fact lives only in the transcript. The card's own ENRICH specimen is
    # exactly this: three memories cross-referenced each other only because a cell
    # happened to notice while writing the third.
    manifest = json.loads((clone / "clone-manifest.json").read_text())["files"]
    names = sorted(p.name for p in clone.glob("*.md"))
    sessions = _sessions_from(records)
    if not sessions:
        # No transcript delta -> nothing was DONE since the last pass -> nothing to
        # enrich. This [] is a RESULT, not a failure, and the report must say which
        # (GC4g): "no new work" and "the reader is broken" are different states.
        # Resolve the client AFTER this exit so an empty delta never constructs
        # an SLMClient (and never calls generate).
        return []
    client = client or _client()
    proposals = []
    for sid, s in sessions.items():
        try:
            raw = client.generate(PROMPT % (s["digest"], "\n".join(names)))
            items = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        except Exception:
            continue       # a model that did not answer in the contract proposes NOTHING
        for i in items:
            if not (isinstance(i, dict) and "file" in i and "link" in i):
                continue
            proposals.append({
                "file": i["file"], "proposed_link": i["link"],
                "rationale": i.get("why", ""), "derived": True,
                # TWO COORDINATES, per GC2 and R2: source_sha256 says which FILE
                # the proposal is against; session_id says which CONTEXT it came
                # from. The hash alone answers only the first, and the talk's
                # VERSIONING guardrail asks for both.
                "source_sha256": manifest.get(i["file"], {}).get("sha256"),
                "session_id": sid,
                "transcript_range": s["range"],
            })
    return proposals


def _sessions_from(records: list[dict]) -> dict:
    """Group transcript records by source session, with a byte range and a digest.

    Reads the `_src` key Task 5's `read_new` attaches. A record WITHOUT `_src` is
    unattributable and is DROPPED rather than guessed at -- an enrichment whose
    provenance is invented is worse than no enrichment (card #656's MED risk).
    """
    out: dict = {}
    for r in records:
        src = r.get("_src") or {}
        sid = src.get("session_id")
        if not sid:
            continue
        s = out.setdefault(sid, {"text": [], "lo": src.get("offset", 0),
                                 "hi": src.get("end", 0)})
        s["lo"] = min(s["lo"], src.get("offset", 0))
        s["hi"] = max(s["hi"], src.get("end", 0))
        body = r.get("message") or {}
        if isinstance(body, dict) and isinstance(body.get("content"), str):
            s["text"].append(body["content"][:400])
    for s in out.values():
        s["digest"] = "\n".join(s["text"][-40:])   # the TAIL: what it did most recently
        s["range"] = [s["lo"], s["hi"]]
    return {k: v for k, v in out.items() if v["digest"].strip()}


def render_proposal(p: dict) -> str:
    return "<!-- derived by dreaming, UNCONFIRMED -->\nSee also [[%s]] - %s" % (
        p["proposed_link"].removesuffix(".md"), p["rationale"])
