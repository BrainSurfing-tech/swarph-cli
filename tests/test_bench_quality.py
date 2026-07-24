"""Pin the distance-engine formulas (spec §2) against known inputs — ported
verbatim from the reference ``bench_quality.py`` self-check, plus the exact
edge cases the brief calls out: numeric e==0, ranking item-mismatch=1,
parse-fail=1."""
from __future__ import annotations

import pytest

from swarph_cli.bench.quality import DISTANCE, d_categorical, d_numeric, d_ranking, d_text, parse_answer, score


# ── d_numeric ──────────────────────────────────────────────────────────────

def test_numeric_exact_match_is_zero():
    assert d_numeric(3.0, 3.0) == 0.0


def test_numeric_relative_error():
    assert d_numeric(6.0, 3.0) == 1.0  # 100% rel-err, capped at 1.0
    assert d_numeric(3.3, 3.0) == pytest.approx(0.1)


def test_numeric_capped_at_one_for_huge_error():
    assert d_numeric(1000.0, 3.0) == 1.0


def test_numeric_expected_zero_and_provided_zero_is_perfect():
    assert d_numeric(0, 0) == 0.0


def test_numeric_expected_zero_and_provided_nonzero_is_worst():
    assert d_numeric(0.001, 0) == 1.0
    assert d_numeric(5, 0) == 1.0


def test_numeric_unparseable_provided_is_worst():
    assert d_numeric("not a number", 3.0) == 1.0
    assert d_numeric(None, 3.0) == 1.0


# ── d_categorical ──────────────────────────────────────────────────────────

def test_categorical_case_and_whitespace_insensitive_match():
    assert d_categorical("skip", "SKIP") == 0.0
    assert d_categorical("  Skip  ", "SKIP") == 0.0


def test_categorical_mismatch_is_worst():
    assert d_categorical("BUY", "SKIP") == 1.0


# ── d_ranking ──────────────────────────────────────────────────────────────

def test_ranking_exact_order_is_zero():
    assert d_ranking(["C", "A", "B"], ["C", "A", "B"]) == 0.0


def test_ranking_fully_reversed_is_worst():
    assert d_ranking(["B", "A", "C"], ["C", "A", "B"]) == 1.0


def test_ranking_item_set_mismatch_is_worst():
    # extra/missing/wrong items entirely -> 1.0, not a partial credit
    assert d_ranking(["A", "B", "D"], ["C", "A", "B"]) == 1.0
    assert d_ranking(["A", "B"], ["C", "A", "B"]) == 1.0


def test_ranking_not_a_list_is_worst():
    assert d_ranking("C,A,B", ["C", "A", "B"]) == 1.0
    assert d_ranking(None, ["C", "A", "B"]) == 1.0


def test_ranking_single_item_is_perfect():
    assert d_ranking(["A"], ["A"]) == 0.0


def test_ranking_partial_disorder_is_between_zero_and_one():
    # one adjacent swap out of 3 items: 1 discordant pair / 3 total pairs
    d = d_ranking(["A", "C", "B"], ["C", "A", "B"])
    assert 0.0 < d < 1.0


# ── d_text ─────────────────────────────────────────────────────────────────

def test_text_identical_is_zero():
    assert d_text("the cat sat", "the cat sat") == 0.0


def test_text_disjoint_is_worst():
    assert d_text("apples oranges", "quantum entanglement") == 1.0


def test_text_partial_overlap_between_zero_and_one():
    d = d_text("the quick brown fox", "the slow brown dog")
    assert 0.0 < d < 1.0


def test_text_empty_expected_and_empty_provided_is_zero():
    assert d_text("", "") == 0.0


def test_text_empty_expected_nonempty_provided_is_worst():
    assert d_text("hello", "") == 1.0


def test_text_embedder_seam_raises_not_implemented():
    # decision #2: a clearly-marked seam for a future gbrain-cosine embedder,
    # NOT wired in v1 — calling with an embedder must fail loudly, not
    # silently fall back to Jaccard.
    with pytest.raises(NotImplementedError):
        d_text("a", "b", embedder=lambda s: [0.0])


# ── DISTANCE dispatch table ──────────────────────────────────────────────────

def test_distance_table_covers_all_four_types():
    assert set(DISTANCE) == {"numeric", "categorical", "ranking", "text"}


# ── parse_answer ───────────────────────────────────────────────────────────

def test_parse_answer_from_prose():
    assert parse_answer('sure! {"answer": 3.0} done') == 3.0


def test_parse_answer_from_code_fence():
    assert parse_answer('```json\n{"answer": ["C","A","B"]}\n```') == ["C", "A", "B"]


def test_parse_answer_none_on_empty():
    assert parse_answer("") is None
    assert parse_answer(None) is None


def test_parse_answer_none_when_absent():
    assert parse_answer("no json here at all") is None


def test_parse_answer_takes_the_last_match():
    text = '{"answer": "first"} ignore this {"answer": "second"}'
    assert parse_answer(text) == "second"


def test_parse_answer_tolerant_of_junk_json_fragment():
    # a syntactically-broken {"answer": ...} fragment is skipped, not fatal,
    # if a valid one exists elsewhere in the text
    text = '{"answer": broken,,} then {"answer": "ok"}'
    assert parse_answer(text) == "ok"


# ── score() — the parse-fail -> 1.0 rule ────────────────────────────────────

def test_score_parse_fail_is_worst_distance():
    result = score({"type": "numeric", "expected": 3.0}, "no json here")
    assert result == {"distance": 1.0, "parsed": None, "parse_ok": False}


def test_score_numeric_happy_path():
    result = score({"type": "numeric", "expected": 3.0}, '{"answer": 3.0}')
    assert result == {"distance": 0.0, "parsed": 3.0, "parse_ok": True}


def test_score_categorical_happy_path():
    result = score({"type": "categorical", "expected": "SKIP"}, '{"answer": "SKIP"}')
    assert result == {"distance": 0.0, "parsed": "SKIP", "parse_ok": True}


def test_score_ranking_happy_path():
    result = score({"type": "ranking", "expected": ["C", "A", "B"]}, '{"answer": ["C","A","B"]}')
    assert result == {"distance": 0.0, "parsed": ["C", "A", "B"], "parse_ok": True}


def test_score_text_uses_raw_response_not_structured_json():
    # spec §2: for `text`, the RAW response is scored — no {"answer": ...}
    # extraction, and a response with no such JSON is NOT a parse failure.
    task = {"type": "text", "expected": "the cat sat"}
    result = score(task, "the cat sat")
    assert result["parse_ok"] is True
    assert result["distance"] == 0.0
    assert result["parsed"] == "the cat sat"
