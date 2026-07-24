"""``swarph bench run|validate|add|prices`` — CLI parse + dispatch (spec §3/§5,
plus the community-submission `add` verb and the shared `prices` cache verb).
Fully offline: backends are injected via monkeypatching
``commands.bench._default_backends``, no network, no real keys."""
from __future__ import annotations

import json

import pytest

from swarph_cli.bench.backends import BackendResult
from swarph_cli.commands import bench

PACK_PATH = "packs/arithmetic_demo.json"

PERFECT_ANSWERS = {
    "17 multiplied": '{"answer": 68}',
    "rectangle": '{"answer": 28.0}',
    "97": '{"answer": "PRIME"}',
    "91": '{"answer": "COMPOSITE"}',
    "smallest to largest": '{"answer": ["7","19","42"]}',
    "multiples of 2": "The sum of two even numbers is always even because each even addend "
                       "is divisible by two, so their total is also divisible by two.",
}


class ScriptedBackend:
    def __init__(self, answers, estimated=False):
        self.answers = answers
        self.estimated = estimated

    def generate(self, model_id, prompt, system=""):
        for key, text in self.answers.items():
            if key in prompt:
                return BackendResult(text=text, tokens_in=10, tokens_thought=0, tokens_out=5,
                                      latency_s=0.1, estimated=self.estimated)
        return BackendResult(text="", tokens_in=1, tokens_thought=0, tokens_out=0,
                              latency_s=0.1, estimated=self.estimated)

    def missing_creds(self):
        return []


class NoCredsBackend:
    def generate(self, *a, **k):
        raise AssertionError("must not dispatch when creds are missing")

    def missing_creds(self):
        return ["GEMINI_API_KEY (or GOOGLE_API_KEY)"]


def _fake_backends(answers=None, estimated=False):
    return {"metered": ScriptedBackend(answers or PERFECT_ANSWERS, estimated=estimated),
            "subscription": ScriptedBackend(answers or PERFECT_ANSWERS, estimated=True)}


# ── bench run ──────────────────────────────────────────────────────────────

def test_run_table_report_shows_per_class_breakdown(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    rc = bench.run_bench(["run", "--models", "fake-model", "--pack", PACK_PATH])
    assert rc == 0
    out = capsys.readouterr().out
    assert "per-class" in out
    assert "prime" in out and "composite" in out
    assert "fake-model" in out


def test_run_json_report_is_valid_json_with_board_and_detail(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    rc = bench.run_bench(["run", "--models", "fake-model", "--pack", PACK_PATH, "--report", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tasks_total"] == 6
    assert data["board"][0]["per_class"]
    assert "fake-model" in data["detail"]


def test_run_missing_pack_file_errors_cleanly(capsys):
    rc = bench.run_bench(["run", "--models", "x", "--pack", "/no/such/pack.json"])
    assert rc != 0
    assert "swarph bench run" in capsys.readouterr().err


def test_run_schema_invalid_pack_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"theme": "x", "tasks": []}))
    rc = bench.run_bench(["run", "--models", "fake-model", "--pack", str(bad)])
    assert rc != 0
    assert "schema" in capsys.readouterr().err


def test_run_estimated_flag_is_loud_in_table_output(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends(estimated=True))
    rc = bench.run_bench(["run", "--models", "fake-model:subscription", "--pack", PACK_PATH])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ESTIMATED" in out or "(est)" in out


# ── bench run: credential preflight ──────────────────────────────────────

def test_run_skips_model_missing_creds_and_runs_the_rest(monkeypatch, capsys):
    def backends():
        return {"metered": ScriptedBackend(PERFECT_ANSWERS)}

    monkeypatch.setattr(bench, "_default_backends", backends)
    # two models sharing the metered backend name, but we monkeypatch preflight
    # semantics via a custom backend map: one "metered" backend that's fine.
    # To exercise a genuinely missing-creds model we register a distinct
    # backend name with no credentials.
    def mixed_backends():
        return {"metered": ScriptedBackend(PERFECT_ANSWERS), "subscription": NoCredsBackend()}

    monkeypatch.setattr(bench, "_default_backends", mixed_backends)
    rc = bench.run_bench([
        "run", "--models", "good-model:metered,bad-model:subscription", "--pack", PACK_PATH,
    ])
    assert rc == 0  # partial run still succeeds
    out = capsys.readouterr()
    assert "good-model" in out.out
    assert "bad-model" not in out.out  # skipped, never reached the board
    assert "GEMINI_API_KEY" in out.err
    assert "bad-model" in out.err  # named exactly which model was skipped


def test_run_strict_aborts_before_any_dispatch_when_any_model_lacks_creds(monkeypatch, capsys):
    def mixed_backends():
        return {"metered": ScriptedBackend(PERFECT_ANSWERS), "subscription": NoCredsBackend()}

    monkeypatch.setattr(bench, "_default_backends", mixed_backends)
    rc = bench.run_bench([
        "run", "--models", "good-model:metered,bad-model:subscription", "--pack", PACK_PATH,
        "--strict",
    ])
    assert rc != 0
    out = capsys.readouterr()
    assert out.out == ""  # no board printed — nothing dispatched at all


def test_run_all_models_missing_creds_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: {"metered": NoCredsBackend()})
    rc = bench.run_bench(["run", "--models", "x", "--pack", PACK_PATH])
    assert rc != 0
    assert "no runnable models" in capsys.readouterr().err


# ── bench validate ─────────────────────────────────────────────────────────

def test_validate_valid_pack_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    rc = bench.run_bench(["validate", PACK_PATH])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_validate_invalid_pack_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"theme": "x", "system": "the answer is SKIP",
                                "tasks": [{"id": "a", "type": "categorical",
                                           "expected": "SKIP", "prompt": "go"}]}))
    rc = bench.run_bench(["validate", str(bad)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "ERROR" in out


def test_validate_with_reference_models_runs_discrimination(monkeypatch, capsys):
    good = ScriptedBackend(PERFECT_ANSWERS)
    bad = ScriptedBackend({})

    class Router:
        def generate(self, model_id, prompt, system=""):
            return (good if model_id == "good" else bad).generate(model_id, prompt, system)

        def missing_creds(self):
            return []

    monkeypatch.setattr(bench, "_default_backends", lambda: {"metered": Router()})
    rc = bench.run_bench(["validate", PACK_PATH, "--reference-models", "good,bad"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "calibration" in out.lower()


def test_validate_json_report(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    rc = bench.run_bench(["validate", PACK_PATH, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert "warnings" in data and "errors" in data


# ── bench add ──────────────────────────────────────────────────────────────

def test_add_valid_pack_installs_named_from_header(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    src = tmp_path / "my_submission.json"  # deliberately NOT named after the theme
    src.write_text(json.dumps({
        "theme": "My Cool Theme!", "system": "ctx",
        "tasks": [{"id": "a", "type": "numeric", "expected": 1.0, "prompt": "1?"}],
    }))
    packs_dir = tmp_path / "packs"
    rc = bench.run_bench(["add", str(src), "--packs-dir", str(packs_dir)])
    assert rc == 0
    installed = packs_dir / "my_cool_theme.json"
    assert installed.exists()
    assert json.loads(installed.read_text())["theme"] == "My Cool Theme!"
    assert "my_cool_theme.json" in capsys.readouterr().out


def test_add_schema_invalid_pack_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    src = tmp_path / "bad.json"
    src.write_text(json.dumps({"theme": "bad_pack", "tasks": []}))
    packs_dir = tmp_path / "packs"
    rc = bench.run_bench(["add", str(src), "--packs-dir", str(packs_dir)])
    assert rc != 0
    assert not (packs_dir / "bad_pack.json").exists()
    assert not packs_dir.exists() or list(packs_dir.iterdir()) == []
    assert "REFUSED" in capsys.readouterr().err


def test_add_answer_leaking_pack_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    src = tmp_path / "leaky.json"
    src.write_text(json.dumps({
        "theme": "leaky_pack", "system": "The correct answer is always SKIP.",
        "tasks": [{"id": "a", "type": "categorical", "expected": "SKIP",
                   "prompt": 'Reply ONLY with JSON: {"answer": <str>}'}],
    }))
    packs_dir = tmp_path / "packs"
    rc = bench.run_bench(["add", str(src), "--packs-dir", str(packs_dir)])
    assert rc != 0
    assert not (packs_dir / "leaky_pack.json").exists()
    err = capsys.readouterr().err
    assert "REFUSED" in err and "leak" in err.lower()


def test_add_collision_refuses_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    existing = packs_dir / "demo_theme.json"
    existing.write_text(json.dumps({"theme": "demo_theme", "tasks": [
        {"id": "old", "type": "numeric", "expected": 1.0, "prompt": "old?"}]}))

    src = tmp_path / "new_submission.json"
    src.write_text(json.dumps({
        "theme": "demo_theme", "system": "ctx",
        "tasks": [{"id": "new", "type": "numeric", "expected": 2.0, "prompt": "new?"}],
    }))
    rc = bench.run_bench(["add", str(src), "--packs-dir", str(packs_dir)])
    assert rc != 0
    assert "already exists" in capsys.readouterr().err
    # untouched — the OLD content is still there
    assert json.loads(existing.read_text())["tasks"][0]["id"] == "old"


def test_add_collision_with_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_default_backends", lambda: _fake_backends())
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    existing = packs_dir / "demo_theme.json"
    existing.write_text(json.dumps({"theme": "demo_theme", "tasks": [
        {"id": "old", "type": "numeric", "expected": 1.0, "prompt": "old?"}]}))

    src = tmp_path / "new_submission.json"
    src.write_text(json.dumps({
        "theme": "demo_theme", "system": "ctx",
        "tasks": [{"id": "new", "type": "numeric", "expected": 2.0, "prompt": "new?"}],
    }))
    rc = bench.run_bench(["add", str(src), "--packs-dir", str(packs_dir), "--force"])
    assert rc == 0
    assert json.loads(existing.read_text())["tasks"][0]["id"] == "new"


def test_add_missing_source_file_errors_cleanly(tmp_path, capsys):
    rc = bench.run_bench(["add", str(tmp_path / "nope.json"), "--packs-dir", str(tmp_path / "packs")])
    assert rc != 0
    assert "swarph bench add" in capsys.readouterr().err


# ── bench prices ───────────────────────────────────────────────────────────

def test_prices_lists_cached_table(monkeypatch, capsys):
    from swarph_cli.bench import prices as prices_mod
    monkeypatch.setattr(prices_mod, "_MAP", {
        "gemini-2.5-flash": {"in": 0.30, "out": 2.50},
        "gemini-2.5-flash-lite": {"in": 0.10, "out": 0.40},
    })
    rc = bench.run_bench(["prices"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gemini-2.5-flash" in out and "gemini-2.5-flash-lite" in out


def test_prices_grep_filters(monkeypatch, capsys):
    from swarph_cli.bench import prices as prices_mod
    monkeypatch.setattr(prices_mod, "_MAP", {
        "gemini-2.5-flash": {"in": 0.30, "out": 2.50},
        "gpt-4o": {"in": 2.5, "out": 10.0},
    })
    rc = bench.run_bench(["prices", "--grep", "gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gemini-2.5-flash" in out
    assert "gpt-4o" not in out


def test_prices_json_report(monkeypatch, capsys):
    from swarph_cli.bench import prices as prices_mod
    monkeypatch.setattr(prices_mod, "_MAP", {"gemini-x": {"in": 1.0, "out": 2.0}})
    rc = bench.run_bench(["prices", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["gemini-x"] == {"in": 1.0, "out": 2.0}


def test_prices_refresh_calls_refresh_module(monkeypatch, capsys, tmp_path):
    from swarph_cli.bench import prices as prices_mod
    cache_file = tmp_path / "llm_prices.json"
    monkeypatch.setattr(prices_mod, "_CACHE_PATH", str(cache_file))
    monkeypatch.setattr(prices_mod, "_MAP", None)

    called = {}

    def fake_refresh_main():
        # simulate the real refresh: write the (patched) cache file, which
        # prices.load(force=True) then re-reads — no real network/disk.
        called["ran"] = True
        cache_file.write_text(json.dumps({"prices": {"new-model": {"in": 5.0, "out": 6.0}}}))
        return 0

    import swarph_cli.bench.refresh_prices as refresh_mod
    monkeypatch.setattr(refresh_mod, "main", fake_refresh_main)

    rc = bench.run_bench(["prices", "--refresh"])
    assert rc == 0
    assert called.get("ran") is True
    assert "new-model" in capsys.readouterr().out


def test_prices_empty_cache_message(monkeypatch, capsys):
    from swarph_cli.bench import prices as prices_mod
    monkeypatch.setattr(prices_mod, "_MAP", {})
    rc = bench.run_bench(["prices"])
    assert rc == 0
    assert "no cached prices" in capsys.readouterr().out


# ── dispatch from swarph main() verb table ──────────────────────────────────

def test_bench_is_registered_in_main_verb_table():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["bench"] == "swarph_cli.commands.bench.run_bench"


def test_run_requires_models_and_pack(capsys):
    with pytest.raises(SystemExit):
        bench.run_bench(["run", "--pack", PACK_PATH])
