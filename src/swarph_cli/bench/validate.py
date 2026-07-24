"""The four ``swarph bench validate`` disciplines (spec §5) — what makes the
registry trustworthy. A pack that only passes schema is not a valid pack;
these four gates exist because of concrete failures the reference lab hit
(``2026-07-24-model-showdown-findings.md``):

  a. schema + integrity
  b. answer-leak scan — a pack that leaks its answers is worthless
  c. discrimination check — an arithmetic pack scored 5 models 1.00 (all
     wrong, identically): rigorous-looking, useless
  d. context-calibration guidance — the pack's ``system`` is the DOMINANT
     variable; an uncalibrated pack measures the author's framing, not the
     model
"""
from __future__ import annotations

import re
from typing import Optional

from .backends import Backend
from .pack import validate_schema
from .quality import d_text
from .runner import ModelSpec, preflight, run_pack

DISCRIMINATION_MIN_SPREAD = 0.05  # min stdev of mean_distance across reference models
# Conservative: text prompts and their expected answers naturally share topical
# vocabulary (e.g. both mention "even numbers") without that being a leak — only
# flag a HIGH-overlap near-restatement, not ordinary topical overlap.
LEAK_PARAPHRASE_MAX_DISTANCE = 0.2  # d_text below this = "obvious paraphrase"


class ValidationReport:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "info": self.info}


# -- (a) schema + integrity ---------------------------------------------------


def gate_schema(pack: dict) -> tuple[list[str], list[str]]:
    return validate_schema(pack)


# -- (b) answer-leak scan -----------------------------------------------------


def _categorical_vocab(pack: dict) -> set[str]:
    """The implied categorical value vocabulary for a pack — the union of
    every ``expected`` used by a categorical task (e.g. {BUY, SKIP}). Needed
    to tell a genuine leak apart from the standard 'reply X or Y' instruction
    footer, which necessarily NAMES every option (see below)."""
    return {
        str(t.get("expected")).strip().upper()
        for t in pack.get("tasks", [])
        if isinstance(t, dict) and t.get("type") == "categorical" and t.get("expected") is not None
    }


def _mentions(vocab: set[str], haystacks: list[str]) -> set[str]:
    out = set()
    for v in vocab:
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", re.IGNORECASE)
        if any(pat.search(h) for h in haystacks if h):
            out.add(v)
    return out


def _leaks(task: dict, system: str, vocab: set[str]) -> bool:
    """True if ``task['expected']`` (or an obvious paraphrase) appears
    verbatim in that task's own prompt or in the pack's system.

    ``categorical`` is the tricky case: a task instruction that says 'Reply
    ONLY with "BUY" or "SKIP"' necessarily NAMES both options — that is NOT a
    leak, it's the answer format spec (the reference pack's own
    ``discipline_skip`` task does exactly this). A real leak is when the
    prompt/system mentions the CORRECT value's vocabulary term WITHOUT also
    offering >=1 alternative — i.e. it isn't presenting a genuine choice.
    ``numeric``/``ranking``/``text`` check for the literal value / order /
    an obvious paraphrase (Jaccard, same engine the scorer uses)."""
    haystacks = [task.get("prompt", "") or "", system or ""]
    ttype = task.get("type")
    expected = task.get("expected")

    if ttype == "categorical":
        needle = str(expected).strip().upper()
        if len(needle) < 2:
            return False
        mentioned = _mentions(vocab, haystacks)
        if needle not in mentioned:
            return False
        # >=2 distinct vocabulary terms mentioned = a genuine multi-choice
        # instruction, not a leak. Only the correct term mentioned (or the
        # pack never uses a 2nd categorical value at all) = a leak.
        return len(mentioned) < 2 or len(vocab) < 2

    if ttype == "numeric":
        needle = str(expected)
        pattern = re.compile(rf"(?<!\d){re.escape(needle)}(?!\d)")
        return any(pattern.search(h) for h in haystacks)

    if ttype == "ranking" and isinstance(expected, list) and expected:
        items = [str(x).lower() for x in expected]
        for h in haystacks:
            hl = h.lower()
            # obvious leak: the exact order appears, e.g. "C, A, B" / "C > A > B"
            joined_variants = [
                ", ".join(items), " > ".join(items), " -> ".join(items), "".join(items),
            ]
            if any(v in hl for v in joined_variants):
                return True
        return False

    if ttype == "text" and isinstance(expected, str) and expected.strip():
        return any(d_text(h, expected) < LEAK_PARAPHRASE_MAX_DISTANCE for h in haystacks if h)

    return False


def gate_answer_leak(pack: dict) -> list[str]:
    """-> a list of error strings, one per leaking task. Empty = clean."""
    errors = []
    system = pack.get("system") or ""
    vocab = _categorical_vocab(pack)
    for task in pack.get("tasks", []):
        if not isinstance(task, dict):
            continue
        if _leaks(task, system, vocab):
            errors.append(
                f"answer leak: task {task.get('id')!r} — its expected answer "
                f"{task.get('expected')!r} (or an obvious paraphrase) appears in "
                f"the prompt or system"
            )
    return errors


# -- (c) discrimination check -------------------------------------------------


def gate_discrimination(
    pack: dict, reference_models: list[str], backends: dict[str, Backend]
) -> tuple[list[str], Optional[dict]]:
    """Run ``reference_models`` against the pack and WARN if the pack fails
    to spread them (e.g. every model scores identically — the arithmetic
    finance-pack failure from findings.md). Returns ``(warnings, run_result)``
    — ``run_result`` is ``None`` if there weren't >=2 reference models to
    compare (nothing to discriminate)."""
    specs = [ModelSpec(id=m) for m in reference_models]
    runnable, warnings = preflight(specs, backends)
    if len(runnable) < 2:
        warnings.append(
            "discrimination check skipped: need >=2 --reference-models WITH valid "
            "credentials to check spread (a missing key doesn't crash validate — see "
            "the preflight warnings above, if any)"
        )
        return warnings, None

    result = run_pack(runnable, pack, backends)
    distances = [row["mean_distance"] for row in result["board"]]
    spread = max(distances) - min(distances) if distances else 0.0
    if spread < DISCRIMINATION_MIN_SPREAD:
        warnings.append(
            f"discrimination WARNING: reference models scored within {spread:.4f} of "
            f"each other (mean_distance range {min(distances):.4f}-{max(distances):.4f}) — "
            f"this pack may not discriminate between models (see findings.md: an "
            f"arithmetic finance pack scored all 5 models 1.00, rigorous-looking but useless)"
        )
    task_types = {t.get("type") for t in pack.get("tasks", [])}
    if "text" in task_types:
        warnings.append(
            "text-type tasks use Jaccard token-overlap distance (deliberately weak, no "
            "embedder wired in v1) — a text pack that PASSES discrimination on Jaccard "
            "alone is a weaker signal than a numeric/categorical pack; treat with more "
            "skepticism until the gbrain-cosine embedder seam (quality.d_text(embedder=)) "
            "is wired"
        )
    return warnings, result


# -- (d) context-calibration guidance -----------------------------------------

CALIBRATION_NOTICE = (
    "context-calibration guidance (spec §5d): this pack's 'system' is the DOMINANT "
    "variable in scoring — in the reference judgment pack, changing ONLY 'system' "
    "flipped every model from SKIP-heavy to BUY-heavy on identical tasks. A valid "
    "pack's 'system' MUST mirror REAL deployment context, not a hand-tuned framing. "
    "This cannot be fully automated; declare 'meta.calibration' "
    "({model, task_ids, expected_mean_distance_max}) so validate can check it."
)


def gate_calibration(pack: dict, backends: Optional[dict[str, Backend]] = None) -> list[str]:
    """ALWAYS emits the context-calibration requirement as a warning (§5d is
    guidance, not automatable in full). If the pack declares
    ``meta.calibration`` ({model, task_ids, expected_mean_distance_max}),
    additionally checks that the declared model+system reproduces it — full
    standardization of this format is DEFERRED (decision #4); this is the
    minimal v1 check."""
    warnings = [CALIBRATION_NOTICE]
    calib = (pack.get("meta") or {}).get("calibration")
    if not calib:
        return warnings

    model = calib.get("model")
    task_ids = calib.get("task_ids")
    expected_max = calib.get("expected_mean_distance_max")
    if not (model and task_ids and expected_max is not None):
        warnings.append(
            "meta.calibration is present but malformed (need model, task_ids, "
            "expected_mean_distance_max) — skipping the reproduction check"
        )
        return warnings

    if backends is None:
        warnings.append(
            f"meta.calibration declares model={model!r} over {len(task_ids)} task(s) "
            f"(expected_mean_distance_max={expected_max}) but no backend was supplied — "
            f"not checked this run"
        )
        return warnings

    runnable, preflight_warnings = preflight([ModelSpec(id=model)], backends)
    warnings.extend(preflight_warnings)
    if not runnable:
        warnings.append(f"meta.calibration check could not run: {model!r} lacks credentials")
        return warnings

    result = run_pack(runnable, pack, backends, task_ids=task_ids)
    if not result["board"]:
        warnings.append(f"meta.calibration check could not run for model={model!r}")
        return warnings
    actual = result["board"][0]["mean_distance"]
    if actual > expected_max:
        warnings.append(
            f"meta.calibration MISMATCH: model={model!r} scored mean_distance={actual} "
            f"on the declared slice, expected <= {expected_max} — the system/context may "
            f"not reproduce the calibration reference"
        )
    else:
        warnings.append(
            f"meta.calibration OK: model={model!r} reproduced mean_distance={actual} "
            f"(<= {expected_max})"
        )
    return warnings


# -- orchestration -------------------------------------------------------------


def validate_pack(
    pack: dict,
    *,
    reference_models: Optional[list[str]] = None,
    backends: Optional[dict[str, Backend]] = None,
) -> ValidationReport:
    report = ValidationReport()

    errors, warnings = gate_schema(pack)
    report.errors.extend(errors)
    report.warnings.extend(warnings)
    if not report.ok:
        # integrity errors make the other gates unreliable (e.g. missing
        # expected/type) — still run the leak scan (it degrades gracefully
        # on malformed tasks) but skip discrimination/calibration runs.
        report.errors.extend(gate_answer_leak(pack))
        report.warnings.append(CALIBRATION_NOTICE)
        return report

    report.errors.extend(gate_answer_leak(pack))

    if reference_models:
        disc_warnings, _ = gate_discrimination(pack, reference_models, backends or {})
        report.warnings.extend(disc_warnings)

    report.warnings.extend(gate_calibration(pack, backends))
    return report
