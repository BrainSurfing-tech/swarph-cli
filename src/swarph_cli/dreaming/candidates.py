"""Corpus -> probeable claims. Deterministic: no model reads memory text here."""
from __future__ import annotations
import re
from pathlib import Path

# Each pattern names ONE capture group for the ref, and optionally a second for
# the asserted value. Anything not matched is unprobeable, which is a reported
# outcome (GC3), not a silent skip.
# `tmp` is in ROOTS deliberately. It is what lets the accept-check control test
# (an artifact that DOES exist, under pytest's tmp_path) extract a candidate at
# all -- without it the (b) check asserts against an empty list and passes for
# the wrong reason. On the real corpus it also means a memory naming a scratch
# path that has since gone reads `absent`, which is CORRECT: that is a claim the
# memory made and no longer holds.
ROOTS = "etc|home|usr|var|opt|srv|tmp"

PATTERNS = [
    ("path",          re.compile(r"(?<![\w/])(/(?:%s)/[\w./-]+)" % ROOTS)),
    ("pkg_version",   re.compile(r"\b([a-z][a-z0-9_-]{2,})\s+v?(\d+\.\d+(?:\.\d+)?)\b")),
    ("listen_addr",   re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}:\d{2,5})\b")),
    ("unit_state",    re.compile(r"\b([\w.@-]+\.service)\b")),
    ("http_endpoint", re.compile(r"(https?://[\w.:-]+(?:/[\w./-]*)?)")),
]
# Memory text is untrusted argv input. A ref must be a single safe token.
SAFE_REF = re.compile(r"^[\w./:@-]+$")


def _escalate_bindings(rows: list[dict]) -> list[dict]:  # noqa: D401 -- see docstring
    """GC4's producer. SAME-LINE ONLY, and that restriction is the whole design.

    REVIEWED AND REWRITTEN 2026-09-03 (a peer, DM 30278; specimens
    re-measured by a peer before this rewrite). THREE joining rules were
    proposed and all three FABRICATE against the real corpus:

      units[0]        -- 28 candidates across 14 files; 14 of those files carry
                         2+ units, so the pick is a coin flip.
      nearest-by-line -- AGREES WITH THE BUG on the specimen below. Two
                         heuristics agreeing is one heuristic.
      one-unit gate   -- "only join when the file names a single unit" feels
                         like rigour and changes nothing about whether the
                         surviving join is real (specimen B below).

    SPECIMEN A, verified on this box: project_deferred_decisions.md carries
    `127.0.0.1:8788` at L80 (a 2026-05-27 ufw note) and `lab-orchestrator.service`
    at L269 (a 2026-06-23 dispatcher note) -- 189 lines and a month apart, on
    unrelated subjects. Every file-level rule emits "lab-orchestrator.service
    binds 127.0.0.1:8788". Nobody wrote that. Ground truth: mesh-gateway.service,
    pid 66747, bound 10.0.0.1:8788 -- WRONG UNIT AND WRONG INTERFACE.

    SPECIMEN B: project_labovh_shared_dotfile_identity.md names exactly one unit
    and four addresses, two of which belong to a different service entirely.

    WHY A BAD JOIN IS WORSE THAN NO JOIN, and this is the reason for the
    strictness rather than a footnote to it: a fabricated join does not surface
    as an error. It surfaces as `surface_disagreement` -- the one verdict class
    GC4 declares trustworthy and routes as an INFRASTRUCTURE FINDING. So a
    joining bug is filed as an infra incident, at full confidence, against a
    memory that was correct.

    THE COST, STATED NOT FOOTNOTED: same-line yields 6 pairs in the whole
    299-file corpus, 4 of which carry a negation. AND IT PRODUCES NOTHING ON
    project_federation_c4_model.md -- units at L29/L39, address at L32 -- so
    GC4's own motivating case does not reproduce automatically. That is
    accepted deliberately. The honest route to covering c4 is to AUTHOR the
    edge (rewrite those lines onto one) rather than infer it: co-location in a
    file is not an assertion, and the corpus never recorded the relation.
    """
    # SAME LINE ONLY. Not units[0], not nearest-by-line, not "only one unit in
    # the file". All three were measured against the real corpus and all three
    # fabricate. See the docstring above for the specimens.
    out = list(rows)
    by_line: dict[int, dict] = {}
    for r in rows:
        by_line.setdefault(r["line"], {})[r["kind"]] = r
    for line, kinds in by_line.items():
        u, a = kinds.get("unit_state"), kinds.get("listen_addr")
        if not (u and a):
            continue
        if _is_negated(a["context"]):
            # A memory that says "localhost is REFUSED" is a CORRECT negative
            # claim. Probing it finds nothing, which agrees with the memory --
            # but a positive-only pipeline reads that as a contradiction and
            # "corrects" a right answer. Polarity is not implemented in v1, so
            # a negated line is refused with a reason, never joined.
            out.append({**a, "kind": "unprobeable", "reason": "negated_claim"})
            continue
        out.append({**a, "kind": "unit_bind", "ref": u["ref"], "asserted": a["ref"]})
    # Every unit/addr pair NOT on one line is refused WITH ITS REASON. One
    # "unprobeable" bucket would hide three different bugs.
    # The fallback must not fire when a pair EXISTED and was refused for another
    # reason -- "no unit_bind produced" cannot tell "never paired" from "paired
    # then refused", and reporting the wrong cause defeats the point of reasons.
    paired = any(("unit_state" in k and "listen_addr" in k) for k in by_line.values())
    if (not paired) and any(r["kind"] == "unit_state" for r in rows) and any(
            r["kind"] == "listen_addr" for r in rows):
        out.append({"file": rows[0]["file"], "line": 0, "kind": "unprobeable",
                    "ref": "", "asserted": None, "reason": "ambiguous_unit",
                    "context": "unit and address present in this file but never on one line"})
    return out


NEGATION = re.compile(r"\b(not|never|no longer|refused|cannot|can never|nothing|unset)\b", re.I)


def _is_negated(line: str) -> bool:
    return bool(NEGATION.search(line))


def extract_candidates(corpus: Path) -> list[dict]:
    out = []
    for p in sorted(corpus.glob("*.md")):
        if p.name in ("MEMORY.md", "MEMORY_FULL.md", "clone-manifest.json"):
            continue  # indexes are ORGANIZE's business, not VERIFY's
        rows: list[dict] = []
        for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            for kind, rx in PATTERNS:
                for m in rx.finditer(line):
                    ref = m.group(1)
                    if not SAFE_REF.match(ref):
                        continue
                    rows.append({"file": p.name, "line": n, "kind": kind, "ref": ref,
                                 "asserted": m.group(2) if m.lastindex and m.lastindex >= 2 else None,
                                 "context": line.strip()[:200]})
        # Escalation is PER FILE: a unit and an address only describe the same
        # claim when the same memory names both.
        out.extend(_escalate_bindings(rows))
    return out
