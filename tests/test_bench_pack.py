"""Pack loader + normative JSON-Schema validation (spec §1). A valid pack
passes; type/expected mismatch fails; dup id fails; missing system warns."""
from __future__ import annotations

import json

import pytest

from swarph_cli.bench.pack import PackError, load_pack, slugify_theme, validate_schema

VALID_PACK = {
    "theme": "demo",
    "version": "1",
    "system": "context",
    "tasks": [
        {"id": "t1", "prompt": "2+2?", "type": "numeric", "expected": 4.0},
        {"id": "t2", "prompt": "prime or not?", "type": "categorical", "expected": "PRIME"},
        {"id": "t3", "prompt": "rank", "type": "ranking", "expected": ["a", "b"]},
        {"id": "t4", "prompt": "explain", "type": "text", "expected": "because"},
    ],
}


def _pack(**overrides):
    p = json.loads(json.dumps(VALID_PACK))  # deep copy
    p.update(overrides)
    return p


# ── load_pack ──────────────────────────────────────────────────────────────

def test_load_pack_reads_valid_json(tmp_path):
    f = tmp_path / "pack.json"
    f.write_text(json.dumps(VALID_PACK))
    pack = load_pack(f)
    assert pack["theme"] == "demo"


def test_load_pack_missing_file_raises_pack_error(tmp_path):
    with pytest.raises(PackError):
        load_pack(tmp_path / "nope.json")


def test_load_pack_invalid_json_raises_pack_error(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    with pytest.raises(PackError):
        load_pack(f)


def test_load_pack_non_object_top_level_raises(tmp_path):
    f = tmp_path / "arr.json"
    f.write_text("[1, 2, 3]")
    with pytest.raises(PackError):
        load_pack(f)


# ── validate_schema: happy path ──────────────────────────────────────────────

def test_valid_pack_passes_with_no_errors_or_warnings():
    errors, warnings = validate_schema(VALID_PACK)
    assert errors == []
    assert warnings == []


# ── validate_schema: required fields ─────────────────────────────────────────

def test_missing_theme_fails():
    p = _pack(theme="")
    errors, _ = validate_schema(p)
    assert any("theme" in e for e in errors)


def test_missing_tasks_fails():
    p = _pack(tasks=[])
    errors, _ = validate_schema(p)
    assert any("tasks" in e for e in errors)


def test_missing_system_warns_not_errors():
    p = _pack()
    del p["system"]
    errors, warnings = validate_schema(p)
    assert errors == []
    assert any("system" in w for w in warnings)


# ── validate_schema: task-level constraints ──────────────────────────────────

def test_duplicate_task_id_fails():
    p = _pack()
    p["tasks"].append({"id": "t1", "prompt": "dup", "type": "numeric", "expected": 1.0})
    errors, _ = validate_schema(p)
    assert any("duplicate" in e.lower() for e in errors)


def test_unknown_type_fails():
    p = _pack()
    p["tasks"][0]["type"] = "essay"
    errors, _ = validate_schema(p)
    assert any("type" in e for e in errors)


@pytest.mark.parametrize("ttype,bad_expected", [
    ("numeric", "not a number"),
    ("categorical", 42),
    ("ranking", "not a list"),
    ("text", ["not", "a", "string"]),
])
def test_expected_type_mismatch_fails(ttype, bad_expected):
    p = _pack()
    p["tasks"][0] = {"id": "bad", "prompt": "x", "type": ttype, "expected": bad_expected}
    errors, _ = validate_schema(p)
    assert any("expected" in e for e in errors), errors


def test_missing_expected_fails():
    p = _pack()
    del p["tasks"][0]["expected"]
    errors, _ = validate_schema(p)
    assert any("expected" in e for e in errors)


def test_missing_prompt_fails():
    p = _pack()
    del p["tasks"][0]["prompt"]
    errors, _ = validate_schema(p)
    assert any("prompt" in e for e in errors)


def test_empty_id_fails():
    p = _pack()
    p["tasks"][0]["id"] = ""
    errors, _ = validate_schema(p)
    assert any("id" in e for e in errors)


def test_non_positive_weight_fails():
    p = _pack()
    p["tasks"][0]["weight"] = 0
    errors, _ = validate_schema(p)
    assert any("weight" in e for e in errors)


def test_negative_weight_fails():
    p = _pack()
    p["tasks"][0]["weight"] = -1.0
    errors, _ = validate_schema(p)
    assert any("weight" in e for e in errors)


def test_positive_weight_passes():
    p = _pack()
    p["tasks"][0]["weight"] = 2.5
    errors, _ = validate_schema(p)
    assert errors == []


def test_task_meta_must_be_object():
    p = _pack()
    p["tasks"][0]["meta"] = "not an object"
    errors, _ = validate_schema(p)
    assert any("meta" in e for e in errors)


def test_non_dict_task_fails():
    p = _pack()
    p["tasks"].append("not a task object")
    errors, _ = validate_schema(p)
    assert any("tasks[4]" in e for e in errors)


def test_numeric_expected_bool_rejected():
    # bool is technically an int subclass in Python — must not slip through
    # as a "numeric" expected value.
    p = _pack()
    p["tasks"][0]["expected"] = True
    errors, _ = validate_schema(p)
    assert any("expected" in e for e in errors)


# ── slugify_theme ────────────────────────────────────────────────────────────

def test_slugify_basic():
    assert slugify_theme("Orchestrator Judgment") == "orchestrator_judgment"


def test_slugify_punctuation_and_case():
    assert slugify_theme("Arithmetic-Demo!! v2") == "arithmetic_demo_v2"


def test_slugify_empty_falls_back():
    assert slugify_theme("   ") == "pack"
