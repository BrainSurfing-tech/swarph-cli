"""Join candidates to probe results. R3: the comparison is against the world."""
from __future__ import annotations
import json
import re
from pathlib import Path
from swarph_cli.dreaming.candidates import extract_candidates
from swarph_cli.dreaming.probes import probe

AGREE, DISAGREE, INCOMPARABLE = "agree", "disagree", "incomparable"
# >>> A MENTION IS NOT A CLAIM. <<< A line that NAMES an artifact and asserts
# nothing about it ("crontab -l > /tmp/crontab.bak", "-> default becomes
# http://100.107.222.72:8788") was scored as the assertion "this exists NOW",
# and an absent artifact became `disagree` -- a confident correction against a
# memory that never claimed anything. MEASURED 2026-09-04 on the first real
# run: 108 of 149 disagreements (path 62/62, listen_addr 30/30, http 16/16)
# had asserted=None; the independent rater read 28 of 30 sampled as noise.
# `mention` reports the absence WITHOUT accusing: it is listed, it is never a
# finding, it never sets the exit code. The rule keys on `asserted is None`,
# which is what the EXTRACTOR produced, not what the sentence meant: path,
# listen_addr and http_endpoint candidates carry no value, so for those three
# kinds `disagree` is now unreachable by design -- an absent artifact of a
# valueless kind is always a mention, even when the prose says "lives at"
# (5 of 83 such rows on 2026-09-04's corpus; a verb detector is not worth its
# regex yet). A memory that ASSERTS a value (a unit state, a version, a
# unit_bind join) still disagrees exactly as before.
MENTION = "mention"
# Tokens Task 3 uses for a real nothing. "refused" is http_endpoint's
# existence-failure token; without it, asserted=None + refused -> agree.
_ABSENT = ("absent", "not-listening", "refused")
# Probe errors that are refusals, not "the memory is wrong".
_PROBE_REASONS = {"not_my_scope", "timeout", "not installed"}


def _compare(asserted, observed, error=None) -> tuple[str, str | None]:
    """THREE VALUES, NEVER TWO. Returns (verdict, reason).

    >>> A BOOLEAN CANNOT SAY "I COULD NOT COMPARE THESE", SO EVERY FAILURE
    COLLAPSES INTO THE ONE VALUE THAT ACCUSES THE MEMORY. <<< The previous
    two-valued version returned False -- i.e. `disagree`, a claim that the
    memory is WRONG -- for all of these, each executed and confirmed
    2026-09-03 (a peer, DM 30313):

      * a bare-string assertion against a dict observation. MEASURED: 16 of 20
        `unit_state` candidate lines assert ONE value, so the MAJORITY of that
        kind would have been falsely corrected, at scale, through the type
        system.
      * `observed is None` -- a probe that FAILED, filed as the memory's error.
      * an errored probe, because the verify loop never read `r["error"]`.
        `_pkg_version` returns error="not installed"; that became `disagree`.

    AND THE SUBSTRING FALLBACK IS GONE. The last line used to be
    `str(asserted) in observed`, which the docstring above it forbade for
    structured values while performing it on every unstructured one -- the fix
    had been scoped to the SHAPE that broke, not the OPERATION that broke it.
    Executed:
        asserted "0.12.0" vs observed "0.12.01" -> agree   (a DIFFERENT version)
        asserted "1.2"    vs observed "11.2"    -> agree   (a different major)
        asserted "active" vs observed "inactive"-> agree   <<< the documented case
    `pkg_version` was the live victim: a string kind, and version prefixes are
    the most substring-prone data in this corpus.
    """
    # >>> THE EXITS ARE ORDERED BY WHAT THEY INTERROGATE, AND THE ORDER IS THE
    # CONTRACT. <<< Sequential returns encode a PRECEDENCE, and an input matching
    # two conditions takes whichever is written first. Do not insert a new exit
    # where it reads nicely -- insert it in its group.
    #
    #   1. THE PROBE       did we get an observation at all?
    #   2. THE CLAIM       is there anything to test?      (KIND-INDEPENDENT)
    #   3. THE COMPARISON  do the two match?
    #
    # THE DEFECT THIS ORDERING FIXES, executed against the previous version
    # (a peer, DM 30319): `asserted is None` sat in group 3, BELOW the
    # dict branch. `not isinstance(asserted, dict)` is TRUE for None, so an
    # existence claim -- "the unit is swarph-brain.service", asserting no state --
    # returned `assertion_shape_mismatch` for dict kinds while returning `agree`
    # on every string kind. The rule written to handle it was UNREACHABLE, and
    # the reason given was WRONG BY THIS PLAN'S OWN TEST: the memory did not make
    # a malformed assertion, it made NO assertion, and that reason sends the next
    # debugger to an extractor that is behaving correctly.
    #
    #     path/pkg_version/listen_addr, asserted=None -> agree
    #     unit_state,                   asserted=None -> incomparable (WRONG)

    # --- 1. THE PROBE ---
    if error:
        return INCOMPARABLE, (error if error in _PROBE_REASONS else "probe_error")
    if observed is None:
        # DEAD BY CONSTRUCTION, kept as a boundary. Every probe in GC3's table
        # returns "absent"/"not-listening" for a real nothing, so reaching this
        # means a probe broke its contract. Task 3 asserts it is unreachable --
        # a defensive exit that never fires and one that fires correctly look
        # identical in every report, so it must be pinned, not assumed.
        return INCOMPARABLE, "no_observation"

    # --- 2. THE CLAIM (kind-independent; MUST precede any shape branch) ---
    if asserted is None:
        # No stated value: the memory only NAMES the artifact. Present -> agree
        # (true for a dict observation exactly as for a string one). Absent ->
        # MENTION, never DISAGREE: nothing was claimed, so nothing is refuted.
        absent = observed in _ABSENT if not isinstance(observed, dict) else False
        return (MENTION if absent else AGREE), None

    # --- 3. THE COMPARISON ---
    if isinstance(observed, dict):
        if not isinstance(asserted, dict) or not asserted:
            # NOT a disagreement: the memory stated one field, the world has
            # several. Nothing was compared, so nothing may be accused.
            return INCOMPARABLE, "assertion_shape_mismatch"
        missing = [k for k in asserted if k not in observed]
        if missing:
            return INCOMPARABLE, "unobserved_field:%s" % ",".join(sorted(missing))
        return (AGREE if all(observed[k] == v for k, v in asserted.items())
                else DISAGREE), None
    # EQUALITY, never containment. A kind needing looser matching (a version
    # range) gets its own comparator; it does not get `in`.
    return (AGREE if str(asserted).strip() == str(observed).strip() else DISAGREE), None


# >>> A CLAIM THAT CARRIES A DATE IS A RECORD OF THAT DATE, NOT AN ASSERTION
# ABOUT NOW. <<< The corpus is largely a historical ledger and the prober read
# every claim as present tense.
#
# MEASURED 2026-09-04, the single largest false-positive class on the first real
# run (~36 of 144 disagreements):
#     feedback_ambient_state_revelation.md:20
#     "GH Pages deploy blocked by repo visibility (swarph-cli v0.7.3 ship, 2026-05-09)"
#     -> asserted 0.7.3, observed 0.53.3, verdict DISAGREE
# That memory is CORRECT. It records an event on a named date.
#
# AND THE FIX WAS ALREADY IN THE SENTENCE: "2026-05-09" sits four words from
# "v0.7.3". The information needed to refuse was present and unused.
#
# DELIBERATELY OVER-REFUSING: a line may carry a date AND a live claim, and this
# rule declines both. That is the correct direction — over-refusing costs a
# missed finding, over-claiming files a confident correction against a right
# memory, and this card rates the second HIGH risk. The refusal is REPORTED with
# its reason, so a reader can look; a false correction is not visibly wrong.
_DATED = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|20\d{2}-\d{2}|"
                    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 20\d{2})\b")


# PROXIMITY, NOT LINE-WIDE. The first build refused ANY claim whose line carried a
# date anywhere. MEASURED on the 305-file corpus: that removed 43 false corrections
# and ALSO silenced 75 claims it had been verifying CORRECTLY — 1.7 good
# verifications lost per bad correction removed. A long line about a dated incident
# often also carries live, checkable facts.
#
# The sentence that motivated the rule shows the right shape:
#     "(swarph-cli v0.7.3 ship, 2026-05-09)"      13 chars apart -> a dated record
# versus a long line whose date belongs to a different clause entirely.
_DATE_PROXIMITY = 60   # chars between the asserted value and the nearest date


def _is_dated_record(context: str, asserted=None) -> bool:
    """True when a date sits NEAR the asserted value on the claim's own line.

    Falls back to line-wide when the asserted value is absent from the context
    (an existence claim carries no value to anchor on) — there is nothing to
    measure distance from, and refusing is the safe direction.
    """
    # >>> THE RULE IS ABOUT A **VALUE** GOING STALE, SO A CLAIM WITH NO VALUE
    # CANNOT BE A DATED RECORD. <<< An EXISTENCE claim (asserted is None) asks
    # "is this artifact there NOW" — a present-tense question about the artifact.
    # A date in the same sentence describes an EVENT, not the artifact's lifetime.
    #
    # MEASURED, and this is why the exemption exists rather than being assumed:
    # the first two builds refused line-wide and silenced 75 claims they had been
    # verifying CORRECTLY. >>> 74 OF THOSE 75 HAD asserted=None. <<< The blunt rule
    # was almost entirely mis-firing on existence claims, and a proximity window
    # recovered NONE of them, because with no value there is nothing to measure
    # distance from and it fell back to line-wide.
    if asserted is None:
        return False
    ctx = context or ""
    hits = [m.span() for m in _DATED.finditer(ctx)]
    if not hits:
        return False
    val = str(asserted)
    if val not in ctx:
        return True                     # value stated but not locatable -> refuse
    vs = ctx.index(val); ve = vs + len(val)
    return any(min(abs(ds - ve), abs(vs - de)) <= _DATE_PROXIMITY
               for ds, de in hits)


def verify(corpus: Path, manifest: dict) -> list[dict]:
    verdicts = []
    for c in extract_candidates(corpus):
        results = probe(c)
        base = {k: c[k] for k in ("file", "line", "kind", "ref", "asserted", "context")}
        base["source_sha256"] = manifest["files"].get(c["file"], {}).get("sha256")
        # GC4d: board_stage emits surface_disagreement ONLY, never disagree.
        # A card can sit at spec while the work is done; the corpus may be
        # the accurate side. Locked before any compare, including empty probe.
        if c.get("kind") == "board_stage":
            r = results[0] if results else {}
            verdicts.append({**base, "observed": r.get("observed"),
                             "surface": r.get("surface"),
                             "verdict": "surface_disagreement"})
            continue
        if not results:
            verdicts.append({**base, "observed": None, "surface": None,
                             "verdict": "unprobeable",
                             "reason": c.get("reason") or "no_probe_for_kind"})
            continue
        seen = {r["surface"]: r["observed"] for r in results}
        # A HASHABLE PROJECTION. `set(seen.values())` raised
        # `TypeError: unhashable type: 'dict'` on EVERY unit_state candidate
        # once _unit_state started returning two fields -- the two-field split
        # and the surface-disagreement check were each correct and mutually
        # exclusive as written. Confirmed by execution before this fix.
        distinct = {json.dumps(v, sort_keys=True, default=str) for v in seen.values()}
        if len(distinct) > 1:
            # GC4: the surfaces disagree with EACH OTHER. That is a fact about
            # the infrastructure, not about the memory, and it must not be
            # filed as a correction to the memory.
            verdicts.append({**base, "observed": str(seen),
                             "surface": "+".join(sorted(seen)),
                             "verdict": "surface_disagreement"})
            continue
        r = results[0]
        # GC4j: a dated claim is refused BEFORE comparison — it cannot be
        # scored against the present without a category error.
        if _is_dated_record(c.get("context", ""), c.get("asserted")):
            verdicts.append({**base, "observed": r["observed"],
                             "surface": r["surface"], "verdict": "unprobeable",
                             "reason": "dated_record"})
            continue
        verdict, reason = _compare(c["asserted"], r["observed"], r.get("error"))
        if verdict == INCOMPARABLE:
            # An incomparable claim is a REFUSAL, and a refusal must never be
            # rendered as an accusation.
            verdicts.append({**base, "observed": r["observed"], "surface": r["surface"],
                             "verdict": "unprobeable", "reason": reason})
            continue
        verdicts.append({**base, "observed": r["observed"], "surface": r["surface"],
                         "verdict": verdict})
    return verdicts
