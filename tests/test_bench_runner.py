"""``run_pack`` — the N-way showdown loop -> the CONFUSION VIEW (spec §3),
end-to-end over the seed pack with an INJECTED fake backend (fully offline:
no network, no real keys). Asserts the confusion-matrix structure is present
(per-class breakdown, not just a scalar), plus token/cost/latency fields and
parse-fail counting."""
from __future__ import annotations

import pytest

from swarph_cli.bench.backends import BackendResult
from swarph_cli.bench.pack import load_pack
from swarph_cli.bench.runner import ModelSpec, class_of, parse_models, preflight, run_pack

PACK_PATH = "packs/arithmetic_demo.json"


class ScriptedBackend:
    """Maps a substring of the prompt -> a canned response text. Deterministic,
    offline, no network — the injected seam every bench test uses."""

    def __init__(self, answers: dict[str, str], estimated: bool = False, latency_s: float = 0.1):
        self.answers = answers
        self.estimated = estimated
        self.latency_s = latency_s
        self.calls: list[tuple[str, str, str]] = []

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        self.calls.append((model_id, prompt, system))
        for key, text in self.answers.items():
            if key in prompt:
                return BackendResult(
                    text=text, tokens_in=10, tokens_thought=0, tokens_out=5,
                    latency_s=self.latency_s, estimated=self.estimated,
                )
        return BackendResult(text="", tokens_in=1, tokens_thought=0, tokens_out=0,
                              latency_s=self.latency_s, estimated=self.estimated)

    def missing_creds(self) -> list[str]:
        return []


PERFECT_ANSWERS = {
    "17 multiplied": '{"answer": 68}',
    "rectangle": '{"answer": 28.0}',
    "97": '{"answer": "PRIME"}',
    "91": '{"answer": "COMPOSITE"}',
    "smallest to largest": '{"answer": ["7","19","42"]}',
    "multiples of 2": "The sum of two even numbers is always even because each even addend is divisible by two, so their total is also divisible by two.",
}


def _pack():
    return load_pack(PACK_PATH)


# ── parse_models ─────────────────────────────────────────────────────────────

def test_parse_models_default_backend_metered():
    specs = parse_models("gemini-2.5-flash")
    assert specs == [ModelSpec(id="gemini-2.5-flash", backend="metered", label="gemini-2.5-flash")]


def test_parse_models_explicit_backend_and_label():
    specs = parse_models("gemini-2.5-flash:subscription:fast-gem")
    assert specs[0].backend == "subscription"
    assert specs[0].label == "fast-gem"


def test_parse_models_csv_multiple():
    specs = parse_models("a, b:subscription, c::c-label")
    assert [s.id for s in specs] == ["a", "b", "c"]
    assert specs[2].label == "c-label"


def test_parse_models_skips_blank_tokens():
    assert parse_models("a,,b") == [ModelSpec(id="a"), ModelSpec(id="b")]


# ── class_of ──────────────────────────────────────────────────────────────

def test_class_of_prefers_meta_class():
    assert class_of({"type": "categorical", "expected": "BUY", "meta": {"class": "trap"}}) == "trap"


def test_class_of_falls_back_to_expected_for_categorical():
    assert class_of({"type": "categorical", "expected": "SKIP"}) == "SKIP"


def test_class_of_falls_back_to_type_otherwise():
    assert class_of({"type": "numeric", "expected": 3.0}) == "numeric"


# ── run_pack: confusion-matrix structure ─────────────────────────────────────

def test_run_pack_returns_confusion_view_not_a_bare_scalar():
    pack = _pack()
    backend = ScriptedBackend(PERFECT_ANSWERS)
    result = run_pack([ModelSpec(id="fake")], pack, {"metered": backend})

    assert result["tasks_total"] == 6
    board = result["board"]
    assert len(board) == 1
    row = board[0]
    # the confusion view: per-class breakdown, not just an aggregate scalar
    assert "per_class" in row and isinstance(row["per_class"], dict)
    assert set(row["per_class"]) == {"numeric", "prime", "composite", "ranking", "text"}
    for cls_row in row["per_class"].values():
        assert {"n", "hits", "hit_rate", "mean_distance"} <= set(cls_row)
    # aggregate fields are ALSO present (both, not either/or)
    for key in ("mean_distance", "parse_fail", "total_tokens", "cost_usd", "mean_latency_s"):
        assert key in row


def test_run_pack_perfect_answers_zero_distance_every_class():
    pack = _pack()
    backend = ScriptedBackend(PERFECT_ANSWERS)
    result = run_pack([ModelSpec(id="fake")], pack, {"metered": backend})
    row = result["board"][0]
    assert row["mean_distance"] == 0.0
    assert row["parse_fail"] == 0
    for cls_row in row["per_class"].values():
        assert cls_row["mean_distance"] == 0.0
        assert cls_row["hit_rate"] == 1.0


def test_run_pack_per_class_isolates_a_wrong_class_not_diluted_into_scalar():
    # deliberately wrong on the "composite" class only (91 is COMPOSITE,
    # answer PRIME) — a scalar mean would hide this; the per-class view must not.
    answers = dict(PERFECT_ANSWERS)
    answers["91"] = '{"answer": "PRIME"}'  # wrong
    backend = ScriptedBackend(answers)
    result = run_pack([ModelSpec(id="fake")], _pack(), {"metered": backend})
    per_class = result["board"][0]["per_class"]
    assert per_class["composite"]["hit_rate"] == 0.0
    assert per_class["composite"]["mean_distance"] == 1.0
    assert per_class["prime"]["hit_rate"] == 1.0  # the OTHER class is unaffected


def test_run_pack_parse_fail_counted_and_scored_worst():
    backend = ScriptedBackend({})  # no matches -> every structured task returns "" -> parse fail
    pack = load_pack(PACK_PATH)
    # keep only structured (non-text) tasks so every one is a genuine parse-fail
    pack["tasks"] = [t for t in pack["tasks"] if t["type"] != "text"]
    result = run_pack([ModelSpec(id="fake")], pack, {"metered": backend})
    row = result["board"][0]
    assert row["parse_fail"] == len(pack["tasks"])
    assert row["mean_distance"] == 1.0


def test_run_pack_tokens_cost_latency_fields_present():
    backend = ScriptedBackend(PERFECT_ANSWERS, latency_s=0.25)
    result = run_pack([ModelSpec(id="gemini-2.5-flash-lite")], _pack(), {"metered": backend})
    row = result["board"][0]
    assert row["total_tokens"] == 6 * 15  # 10 in + 5 out per task, 6 tasks
    assert row["cost_usd"] >= 0.0
    assert row["mean_latency_s"] == 0.25


def test_run_pack_estimated_flag_propagates_when_backend_estimates():
    backend = ScriptedBackend(PERFECT_ANSWERS, estimated=True)
    result = run_pack([ModelSpec(id="fake")], _pack(), {"metered": backend})
    assert result["board"][0]["estimated"] is True


def test_run_pack_ranks_by_mean_distance_then_cost():
    good = ScriptedBackend(PERFECT_ANSWERS)
    bad_answers = {k: '{"answer": "nonsense"}' for k in PERFECT_ANSWERS}
    bad = ScriptedBackend(bad_answers)

    class Router:
        """Routes by model id to a different scripted backend, so 'good-model'
        and 'bad-model' get materially different quality in one run."""
        def __init__(self, good, bad):
            self.good, self.bad = good, bad

        def generate(self, model_id, prompt, system=""):
            return (self.good if model_id == "good-model" else self.bad).generate(model_id, prompt, system)

        def missing_creds(self):
            return []

    router = Router(good, bad)
    result = run_pack(
        [ModelSpec(id="bad-model"), ModelSpec(id="good-model")],
        _pack(), {"metered": router},
    )
    labels_in_rank_order = [row["label"] for row in result["board"]]
    assert labels_in_rank_order[0] == "good-model"  # lower mean_distance ranks first


def test_run_pack_task_ids_subset():
    backend = ScriptedBackend(PERFECT_ANSWERS)
    result = run_pack([ModelSpec(id="fake")], _pack(), {"metered": backend}, task_ids=["arith_mult"])
    assert result["tasks_total"] == 1
    assert result["board"][0]["ran"] == 1


def test_run_pack_backend_error_counts_as_error_not_silent_zero():
    class ErroringBackend:
        def generate(self, model_id, prompt, system=""):
            return BackendResult(text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                                  latency_s=0.0, estimated=False, error="503 unavailable")

        def missing_creds(self):
            return []

    result = run_pack([ModelSpec(id="fake")], _pack(), {"metered": ErroringBackend()})
    row = result["board"][0]
    assert row["errors"] == 6
    assert row["ran"] == 0


def test_run_pack_unknown_backend_raises_keyerror():
    with pytest.raises(KeyError):
        run_pack([ModelSpec(id="x", backend="ghost")], _pack(), {})


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_passes_backend_with_no_missing_creds():
    backend = ScriptedBackend({})
    runnable, warnings = preflight([ModelSpec(id="a")], {"metered": backend})
    assert runnable == [ModelSpec(id="a")]
    assert warnings == []


def test_preflight_skips_and_warns_on_missing_backend():
    runnable, warnings = preflight([ModelSpec(id="a", backend="ghost")], {})
    assert runnable == []
    assert warnings and "ghost" in warnings[0]


def test_preflight_skips_and_warns_on_missing_creds():
    class NoCredsBackend:
        def generate(self, *a, **k):
            raise AssertionError("must not be dispatched to")

        def missing_creds(self):
            return ["SOME_API_KEY"]

    runnable, warnings = preflight([ModelSpec(id="a", label="model-a")], {"metered": NoCredsBackend()})
    assert runnable == []
    assert warnings and "model-a" in warnings[0] and "SOME_API_KEY" in warnings[0]
