"""Card #1083: the OpenAI Decisions bench arm.

The double rejects a body the documented endpoint would reject. A choice
keeps its probabilities. A predicate uses the laya noul cut. A refusal is
a parse failure, not a guessed label. A response with no usage object is
estimated.
"""
from __future__ import annotations

import json

from swarph_cli.bench.backends import (
    DecisionsBackend,
    StrictDecisionsDouble,
    decisions_shape_error,
    estimate_tokens,
)
from swarph_cli.bench.providers import load_registry
from swarph_cli.bench.quality import parse_answer, score
from swarph_cli.bench.runner import ModelSpec, preflight, run_pack
from swarph_cli.commands.bench import _provider_arm

TOKEN = "openai-test-token-not-a-real-key"
EXAMPLE = "docs/examples/bench_providers.toml"


def _choice_prompt():
    return json.dumps({
        "state": {"text": "I was charged twice"},
        "questions": {
            "answer": {
                "type": "choice",
                "instructions": "Which department should handle this complaint?",
                "criteria": {
                    "billing": "Payments, invoices, and refunds.",
                    "technical": "Problems using the product.",
                },
            }
        },
    })


def _noul_prompt(qtype="noul"):
    return json.dumps({
        "state": {"text": "thanks"},
        "questions": {
            "answer": {
                "type": qtype,
                "instructions": "is this a code question?",
                "labels": {"true": "CODE", "false": "CHAT"},
            }
        },
    })


def _arm(response, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", TOKEN)
    double = StrictDecisionsDouble(response)
    arm = DecisionsBackend(
        name="openai",
        base_url="https://api.openai.com",
        path="/v1/decisions",
        auth={"env": "OPENAI_API_KEY", "header": "Authorization", "scheme": "Bearer"},
        price={
            "in_per_mtok": 0.10,
            "out_per_mtok": 0,
            "source": "https://developers.openai.com/api/docs/guides/decisions",
        },
        egress="external",
        transport=double,
    )
    return arm, double


def _post(body: dict):
    status, _reason, raw = StrictDecisionsDouble()(
        "https://api.openai.com/v1/decisions", {}, json.dumps(body).encode())
    return status, json.loads(raw.decode())


def test_double_rejects_a_non_list_questions_field():
    status, payload = _post({
        "model": "gpt-6-luna",
        "input": "{}",
        "questions": {"answer": {"type": "predicate", "name": "answer",
                                  "instructions": "is it?"}},
    })
    assert status == 400
    assert payload["error"] == "questions is not a list"
    assert decisions_shape_error({"questions": {"answer": {}}}) == "questions is not a list"


def test_double_rejects_a_question_missing_name_type_or_instructions():
    base = {"type": "predicate", "name": "answer", "instructions": "is it?"}
    for field in ("name", "type", "instructions"):
        question = dict(base)
        del question[field]
        status, payload = _post({
            "model": "gpt-6-luna",
            "input": "{}",
            "questions": [question],
        })
        assert status == 400, field
        assert payload["error"] == f"questions[0] lacks {field}"


def test_double_rejects_a_choice_without_choices():
    missing = {
        "type": "choice", "name": "answer",
        "instructions": "which?",
    }
    bare = dict(missing, choices=[{"value": "billing"}])
    not_a_list = dict(missing, choices="billing")
    for body_q in (missing, bare, not_a_list):
        status, payload = _post({
            "model": "gpt-6-luna",
            "input": "{}",
            "questions": [body_q],
        })
        assert status == 400
        assert "choice" in payload["error"]


def test_choice_keeps_probabilities_and_the_double_accepts_the_translation(monkeypatch):
    probs = [
        {"value": "billing", "probability": 0.95},
        {"value": "technical", "probability": 0.05},
    ]
    arm, double = _arm({
        "answers": [{
            "type": "choice",
            "name": "answer",
            "choice": "billing",
            "probabilities": probs,
            "confidence": 0.93,
        }],
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }, monkeypatch)
    result = arm.generate("openai", _choice_prompt(), "")
    assert result.error is None, result.error
    sent = double.bodies[0]
    assert sent["model"] == "gpt-6-luna"
    assert sent["input"] == json.dumps({"text": "I was charged twice"})
    assert sent["questions"] == [{
        "name": "answer",
        "type": "choice",
        "instructions": "Which department should handle this complaint?",
        "choices": [
            {"value": "billing", "description": "Payments, invoices, and refunds."},
            {"value": "technical", "description": "Problems using the product."},
        ],
    }]
    assert decisions_shape_error(sent) is None
    assert parse_answer(result.text) == "billing"
    kept = json.loads(result.text.splitlines()[1])
    assert kept["probabilities"] == probs
    assert kept["confidence"] == 0.93
    assert result.estimated is False
    assert result.tokens_in == 12
    assert result.tokens_out == 0
    assert TOKEN not in result.text


def test_predicate_maps_probability_the_way_noul_does(monkeypatch):
    seen = {}
    for qtype in ("noul", "yes", "yes-no"):
        for p_true, label in ((0.49, "CHAT"), (0.50, "CODE"), (0.51, "CODE")):
            arm, _double = _arm({
                "answers": [{
                    "type": "predicate",
                    "name": "answer",
                    "probability": p_true,
                }],
                "usage": {"input_tokens": 4, "output_tokens": 0},
            }, monkeypatch)
            result = arm.generate("openai", _noul_prompt(qtype), "")
            assert result.error is None, result.error
            body = json.loads(result.text)
            assert body["answer"] == label
            assert body["p_true"] == p_true
            assert _double.bodies[0]["questions"][0]["type"] == "predicate"
            assert "choices" not in _double.bodies[0]["questions"][0]
            seen[(qtype, p_true)] = body["answer"]
    assert seen[("noul", 0.49)] == "CHAT"
    assert seen[("yes", 0.50)] == "CODE"


def test_refusal_is_a_parse_fail_and_not_a_guessed_label(monkeypatch):
    arm, _double = _arm({
        "answers": [{
            "type": "refusal",
            "name": "answer",
            "reason": "not enough evidence",
        }],
    }, monkeypatch)
    prompt = _choice_prompt()
    result = arm.generate("openai", prompt, "")
    assert result.error is None, result.error
    assert parse_answer(result.text) is None
    assert "not enough evidence" in result.text
    assert "billing" not in result.text
    scored = score(
        {"type": "categorical", "expected": "billing"}, result.text)
    assert scored["parse_ok"] is False
    pack = {
        "theme": "decisions",
        "request_kind": "typed",
        "tasks": [{
            "id": "r1",
            "type": "categorical",
            "prompt": prompt,
            "expected": "billing",
        }],
    }
    spec = ModelSpec(
        id="openai", backend="provider", provider="openai",
        label="provider:openai", arm=arm)
    board = run_pack([spec], pack, {})
    row = board["board"][0]
    assert row["parse_fail"] == 1
    assert row["errors"] == 0
    assert board["detail"]["provider:openai"][0]["parse_ok"] is False


def test_missing_usage_is_estimated_from_the_input(monkeypatch):
    arm, _double = _arm({
        "answers": [{
            "type": "predicate",
            "name": "answer",
            "probability": 0.2,
        }],
    }, monkeypatch)
    prompt = _noul_prompt()
    result = arm.generate("openai", prompt, "")
    assert result.error is None, result.error
    assert result.estimated is True
    assert result.tokens_in == estimate_tokens(json.dumps({"text": "thanks"}))
    assert result.tokens_out == 0
    assert json.loads(result.text)["answer"] == "CHAT"


def test_example_registry_selects_the_decisions_arm(monkeypatch):
    registry = load_registry(EXAMPLE)
    entry = registry["openai"]
    assert entry["kind"] == "decisions"
    assert entry["path"] == "/v1/decisions"
    assert entry["egress"] == "external"
    assert entry["auth"]["env"] == "OPENAI_API_KEY"
    assert entry["price"]["in_per_mtok"] == 0.10
    assert entry["price"]["out_per_mtok"] == 0
    assert entry["price"]["source"] == (
        "https://developers.openai.com/api/docs/guides/decisions; "
        "base rate; regional and long-context premiums not modelled")
    arm = _provider_arm("openai", entry, None)
    assert isinstance(arm, DecisionsBackend)
    assert arm.KIND == "typed"
    monkeypatch.setenv("OPENAI_API_KEY", TOKEN)
    spec = ModelSpec(
        id="openai", backend="provider", provider="openai",
        label="provider:openai", arm=arm)
    runnable, warnings = preflight(
        [spec], {}, pack={"request_kind": "typed", "tasks": []})
    assert runnable == [spec]
    assert warnings == []


def test_double_rejects_a_predicate_that_carries_choices_or_levels():
    predicate = {
        "type": "predicate", "name": "answer", "instructions": "is it?",
    }
    for extra in ({"choices": [{"value": "a", "description": "b"}]},
                  {"levels": [{"label": "low", "description": "small"}]}):
        status, payload = _post({
            "model": "gpt-6-luna",
            "input": "{}",
            "questions": [dict(predicate, **extra)],
        })
        assert status == 400
        assert payload["error"] == "questions[0] is a predicate carrying choices or levels"


def test_double_rejects_a_score_without_levels_and_a_choice_lacking_value():
    status, payload = _post({
        "model": "gpt-6-luna",
        "input": "{}",
        "questions": [{
            "type": "score", "name": "answer", "instructions": "how severe?",
        }],
    })
    assert status == 400
    assert payload["error"] == "questions[0] is a score without levels"
    status, payload = _post({
        "model": "gpt-6-luna",
        "input": "{}",
        "questions": [{
            "type": "choice", "name": "answer", "instructions": "which?",
            "choices": [{"description": "Payments, invoices, and refunds."}],
        }],
    })
    assert status == 400
    assert "value, description" in payload["error"]


def test_score_answer_is_a_parse_fail_and_not_a_label(monkeypatch):
    arm, double = _arm({
        "answers": [{
            "type": "score",
            "name": "answer",
            "score": 1.1,
            "confidence": 0.55,
        }],
    }, monkeypatch)
    prompt = json.dumps({
        "state": {"text": "export fails in Safari"},
        "questions": {
            "answer": {
                "type": "score",
                "instructions": "How severe is this issue?",
                "levels": [
                    {"label": "Cosmetic", "description": "Appearance only."},
                    {"label": "Fully blocked", "description": "No workaround."},
                ],
            }
        },
    })
    result = arm.generate("openai", prompt, "")
    assert result.error is None, result.error
    assert decisions_shape_error(double.bodies[0]) is None
    assert parse_answer(result.text) is None
    assert result.text == json.dumps({"refusal": "unsupported answer type: score"})
    assert "Cosmetic" not in result.text
    scored = score({"type": "categorical", "expected": "Cosmetic"}, result.text)
    assert scored["parse_ok"] is False


def test_an_image_part_is_refused_and_not_sent(monkeypatch):
    arm, double = _arm({"answers": []}, monkeypatch)
    prompt = json.dumps({
        "state": {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Inspect this."},
                {"type": "input_image", "image_url": "data:image/png;base64,aaaa"},
            ],
        },
        "questions": {
            "answer": {
                "type": "predicate",
                "instructions": "is it damaged?",
            }
        },
    })
    result = arm.generate("openai", prompt, "")
    assert result.error is not None
    assert "image" in result.error
    assert double.bodies == []
    assert arm.calls == 0
