"""The four ``swarph bench validate`` gates (spec §5) — offline, with an
INJECTED fake backend for the discrimination/calibration checks (no
network)."""
from __future__ import annotations

import json

from swarph_cli.bench.backends import BackendResult
from swarph_cli.bench.pack import load_pack
from swarph_cli.bench.runner import ModelSpec
from swarph_cli.bench.validate import (
    CALIBRATION_NOTICE,
    gate_answer_leak,
    gate_calibration,
    gate_discrimination,
    gate_schema,
    validate_pack,
)

VALID_PACK = json.loads(open("packs/arithmetic_demo.json").read())


def _pack(**overrides):
    p = json.loads(json.dumps(VALID_PACK))
    p.update(overrides)
    return p


class ScriptedBackend:
    def __init__(self, answers: dict[str, str]):
        self.answers = answers

    def generate(self, model_id, prompt, system=""):
        for key, text in self.answers.items():
            if key in prompt:
                return BackendResult(text=text, tokens_in=1, tokens_thought=0, tokens_out=1,
                                      latency_s=0.01, estimated=False)
        return BackendResult(text="", tokens_in=1, tokens_thought=0, tokens_out=0,
                              latency_s=0.01, estimated=False)

    def missing_creds(self):
        return []


PERFECT_ANSWERS = {
    "17 multiplied": '{"answer": 68}',
    "rectangle": '{"answer": 28.0}',
    "97": '{"answer": "PRIME"}',
    "91": '{"answer": "COMPOSITE"}',
    "smallest to largest": '{"answer": ["7","19","42"]}',
    "multiples of 2": "The sum of two even numbers is always even because each even addend "
                       "is divisible by two, so their total is also divisible by two.",
}


# ── (a) schema + integrity ────────────────────────────────────────────────

def test_gate_schema_valid_pack_passes():
    errors, warnings = gate_schema(VALID_PACK)
    assert errors == [] and warnings == []


def test_gate_schema_invalid_pack_fails():
    bad = _pack()
    del bad["theme"]
    errors, _ = gate_schema(bad)
    assert errors


def test_validate_pack_fails_fast_on_schema_error_no_network_calls():
    bad = _pack()
    bad["tasks"] = []
    report = validate_pack(bad)  # no backends passed at all — must not need them
    assert not report.ok
    assert any("tasks" in e for e in report.errors)


# ── (b) answer-leak scan ─────────────────────────────────────────────────

def test_seed_pack_has_no_leaks():
    assert gate_answer_leak(VALID_PACK) == []


def test_categorical_naming_both_options_is_not_a_leak():
    # "Reply ONLY with JSON: {"answer": "PRIME" or "COMPOSITE"}" names BOTH
    # options — that's the answer-format spec, not a leak (mirrors the
    # reference pack's own discipline_skip task).
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "categorical", "expected": "SKIP",
             "prompt": 'Reply ONLY with JSON: {"answer": "BUY" or "SKIP"}'},
            {"id": "b", "type": "categorical", "expected": "BUY",
             "prompt": 'Reply ONLY with JSON: {"answer": "BUY" or "SKIP"}'},
        ],
    }
    assert gate_answer_leak(p) == []


def test_categorical_leak_detected_when_only_correct_value_named():
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "categorical", "expected": "SKIP",
             "prompt": "Given the risk profile, the disciplined call is SKIP. Confirm: "
                       'Reply ONLY with JSON: {"answer": <str>}'},
        ],
    }
    errors = gate_answer_leak(p)
    assert errors and "a" in errors[0]


def test_categorical_leak_detected_in_system_not_just_prompt():
    p = {
        "theme": "t", "system": "The correct answer here is always SKIP.",
        "tasks": [
            {"id": "a", "type": "categorical", "expected": "SKIP",
             "prompt": 'Reply ONLY with JSON: {"answer": <str>}'},
        ],
    }
    assert gate_answer_leak(p)


def test_numeric_leak_detected():
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "numeric", "expected": 68,
             "prompt": 'The answer is 68. Confirm: {"answer": <number>}'},
        ],
    }
    errors = gate_answer_leak(p)
    assert errors


def test_numeric_leak_not_falsely_triggered_by_operands():
    # 68 is the ANSWER; operands 17 and 4 legitimately appear in the prompt
    # and must not be confused with the answer itself.
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "numeric", "expected": 68,
             "prompt": '17 * 4 = ? {"answer": <number>}'},
        ],
    }
    assert gate_answer_leak(p) == []


def test_ranking_leak_detected_when_order_stated():
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "ranking", "expected": ["C", "A", "B"],
             "prompt": 'The order is C, A, B. Confirm: {"answer": [...]}'},
        ],
    }
    assert gate_answer_leak(p)


def test_text_leak_detected_on_near_verbatim_restatement():
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "text",
             "expected": "the sum of two even numbers is always even",
             "prompt": "the sum of two even numbers is always even why"},
        ],
    }
    assert gate_answer_leak(p)


def test_text_topical_overlap_without_restatement_is_not_a_leak():
    p = {
        "theme": "t", "system": "s",
        "tasks": [
            {"id": "a", "type": "text",
             "expected": "The sum of two even numbers is always even because each even "
                         "addend is divisible by two, so their total is also divisible by two.",
             "prompt": "In one short sentence, explain why adding two multiples of 2 always "
                       "produces another multiple of 2."},
        ],
    }
    assert gate_answer_leak(p) == []


# ── (c) discrimination check ──────────────────────────────────────────────

def test_discrimination_needs_at_least_two_reference_models():
    warnings, result = gate_discrimination(VALID_PACK, ["only-one"], {"metered": ScriptedBackend({})})
    assert result is None
    assert any("skipped" in w for w in warnings)


def test_discrimination_warns_on_identical_scores():
    # both reference models answer identically wrong -> zero spread
    identical = ScriptedBackend({})  # empty answers -> every task parse-fails identically
    warnings, result = gate_discrimination(
        VALID_PACK, ["model-a", "model-b"], {"metered": identical}
    )
    assert result is not None
    assert any("discrimination WARNING" in w for w in warnings)


def test_discrimination_no_warning_when_models_actually_spread():
    good = ScriptedBackend(PERFECT_ANSWERS)
    bad = ScriptedBackend({})  # everything parse-fails -> worst score

    class Router:
        def generate(self, model_id, prompt, system=""):
            return (good if model_id == "good" else bad).generate(model_id, prompt, system)

        def missing_creds(self):
            return []

    warnings, result = gate_discrimination(VALID_PACK, ["good", "bad"], {"metered": Router()})
    assert not any("discrimination WARNING" in w for w in warnings)
    distances = [row["mean_distance"] for row in result["board"]]
    assert max(distances) - min(distances) > 0.5


def test_discrimination_flags_text_type_jaccard_weakness():
    warnings, _ = gate_discrimination(VALID_PACK, ["a", "b"], {"metered": ScriptedBackend({})})
    assert any("Jaccard" in w for w in warnings)


def test_discrimination_skips_gracefully_on_missing_creds():
    class NoCreds:
        def generate(self, *a, **k):
            raise AssertionError("must not dispatch")

        def missing_creds(self):
            return ["SOME_KEY"]

    warnings, result = gate_discrimination(VALID_PACK, ["a", "b"], {"metered": NoCreds()})
    assert result is None
    assert any("SOME_KEY" in w for w in warnings)
    assert any("skipped" in w for w in warnings)


# ── (d) context-calibration guidance ──────────────────────────────────────

def test_calibration_always_emits_the_notice():
    warnings = gate_calibration(VALID_PACK, backends=None)
    assert CALIBRATION_NOTICE in warnings


def test_calibration_no_meta_declared_only_the_notice():
    warnings = gate_calibration(VALID_PACK, backends=None)
    assert len(warnings) == 1


def test_calibration_malformed_meta_flagged():
    p = _pack(meta={"calibration": {"model": "x"}})  # missing task_ids/expected_mean_distance_max
    warnings = gate_calibration(p, backends=None)
    assert any("malformed" in w for w in warnings)


def test_calibration_declared_but_no_backend_flags_not_checked():
    p = _pack(meta={"calibration": {
        "model": "fake", "task_ids": ["arith_mult"], "expected_mean_distance_max": 0.1,
    }})
    warnings = gate_calibration(p, backends=None)
    assert any("not checked" in w for w in warnings)


def test_calibration_reproduced_within_bound_is_ok():
    p = _pack(meta={"calibration": {
        "model": "fake", "task_ids": ["arith_mult"], "expected_mean_distance_max": 0.1,
    }})
    backend = ScriptedBackend(PERFECT_ANSWERS)
    warnings = gate_calibration(p, backends={"metered": backend})
    assert any("meta.calibration OK" in w for w in warnings)


def test_calibration_mismatch_flagged():
    p = _pack(meta={"calibration": {
        "model": "fake", "task_ids": ["arith_mult"], "expected_mean_distance_max": 0.01,
    }})
    backend = ScriptedBackend({})  # parse-fail -> distance 1.0, way over the 0.01 bound
    warnings = gate_calibration(p, backends={"metered": backend})
    assert any("MISMATCH" in w for w in warnings)


def test_calibration_missing_creds_flagged_gracefully():
    p = _pack(meta={"calibration": {
        "model": "fake", "task_ids": ["arith_mult"], "expected_mean_distance_max": 0.1,
    }})

    class NoCreds:
        def generate(self, *a, **k):
            raise AssertionError("must not dispatch")

        def missing_creds(self):
            return ["SOME_KEY"]

    warnings = gate_calibration(p, backends={"metered": NoCreds()})
    assert any("lacks credentials" in w for w in warnings)


# ── orchestration: validate_pack ───────────────────────────────────────────

def test_validate_pack_ok_true_when_only_warnings():
    report = validate_pack(VALID_PACK)
    assert report.ok is True  # calibration notice is a warning, doesn't fail the pack
    assert report.warnings


def test_validate_pack_errors_include_leak_even_with_reference_models_skipped():
    p = {
        "theme": "leaky", "system": "The answer is SKIP.",
        "tasks": [
            {"id": "a", "type": "categorical", "expected": "SKIP",
             "prompt": 'Reply: {"answer": <str>}'},
        ],
    }
    report = validate_pack(p)
    assert not report.ok
    assert any("leak" in e for e in report.errors)
