"""#213 signal bench: signal:<module:callable> plus the robust date-grouped scorer.

Can-fail on current main: parse_models('signal:pkg.mod:fn') takes the
id:backend:label split, so the backend is 'pkg.mod' and this test's
backend=='signal' assertion fails. A signal pack whose regime axis has
one value fails validate.
"""
from __future__ import annotations

import json

from swarph_cli.bench.backends import SignalBackend
from swarph_cli.bench.runner import parse_models, run_pack
from swarph_cli.bench.validate import gate_signal_pack, tasks_sha256, validate_pack
from swarph_cli.commands.bench import _default_backends, _format_run_table


def always_flat(prompt: str) -> str:
    return "FLAT"


def calls_up_when_phi_high(prompt: str) -> str:
    setup = json.loads(prompt)
    return "UP" if float(setup["phi"]) >= 5 else "DOWN"


def _task(tid, phi, fwd, date, regime, sector, expected):
    return {
        "id": tid,
        "type": "categorical",
        "expected": expected,
        "prompt": json.dumps({"phi": phi, "ticker": tid}),
        "meta": {"class": f"{regime}|{sector}", "fwd": fwd, "date": date},
    }


def _pack(tasks):
    pack = {
        "theme": "omega_universe_v1",
        "version": "1",
        "system": (
            'Reply ONLY with JSON {"answer": "UP" or "FLAT" or "DOWN"}. '
            "The three labels are the whole choice."
        ),
        "description": "fixture",
        "tasks": tasks,
        "header": {
            "pack_kind": "signal",
            "data_window": {"first": "2026-01-01", "last": "2026-01-02"},
            "row_count": len(tasks),
            "universe_source_query": "select metadata from ai_memory where type=omega_structured_decision",
            "price_history_snapshot_date": "2026-01-02",
            "outcome": {"horizon_bars": 5, "up_gt": 0.01, "down_lt": -0.01},
            "sha256_tasks": tasks_sha256(tasks),
        },
    }
    return pack


def test_parse_models_keeps_signal_payload():
    specs = parse_models(
        "rule:pkg.mod:fn,"
        "signal:research.signal_audit.omega_universe_pack:gate_arm,"
        "signal:research.signal_audit.omega_universe_pack:baseline_flat"
    )
    assert [(s.id, s.backend) for s in specs] == [
        ("pkg.mod:fn", "rule"),
        ("research.signal_audit.omega_universe_pack:gate_arm", "signal"),
        ("research.signal_audit.omega_universe_pack:baseline_flat", "signal"),
    ]


def test_signal_run_reports_per_cell_then_pooled():
    tasks = [
        _task("a", 8, 0.04, "2026-01-01", "CALM", "Tech", "UP"),
        _task("b", 8, 0.03, "2026-01-02", "CALM", "Tech", "UP"),
        _task("c", 1, -0.05, "2026-01-01", "CRISIS", "Energy", "DOWN"),
        _task("d", 1, -0.02, "2026-01-02", "CRISIS", "Energy", "DOWN"),
    ]
    backends = _default_backends()
    assert isinstance(backends["signal"], SignalBackend)
    result = run_pack(
        parse_models(f"signal:{__name__}:calls_up_when_phi_high,signal:{__name__}:always_flat"),
        _pack(tasks),
        backends,
    )
    assert result["board"][0]["backend"] == "signal"
    gate = next(r for r in result["board"] if r["model_id"].endswith("calls_up_when_phi_high"))
    base = next(r for r in result["board"] if r["model_id"].endswith("always_flat"))
    cells = gate["robust"]["per_cell"]
    assert set(cells) == {"CALM|Tech", "CRISIS|Energy"}
    assert cells["CALM|Tech"]["n"] == 2
    assert cells["CALM|Tech"]["balanced_accuracy"] == 1.0
    assert cells["CALM|Tech"]["median_fwd_called_up"] == 0.035
    assert cells["CRISIS|Energy"]["median_fwd_called_down"] == -0.035
    assert cells["CALM|Tech"]["bootstrap_ci"]["n_dates"] == 2
    assert "pooled" in gate["robust"]
    assert gate["robust"]["pooled"]["n"] == 4
    assert base["robust"]["per_cell"]["CALM|Tech"]["n_called_up"] == 0
    assert base["robust"]["per_cell"]["CALM|Tech"]["median_fwd_called_up"] is None
    text = _format_run_table(result)
    per_at = text.index("per-cell (robust):")
    pooled_at = text.index("pooled (beside the per-cell table):")
    assert per_at < pooled_at


def test_constant_regime_is_not_a_valid_pack():
    tasks = [
        _task("a", 8, 0.04, "2026-01-01", "CALM", "Tech", "UP"),
        _task("b", 1, -0.04, "2026-01-02", "CALM", "Energy", "DOWN"),
    ]
    pack = _pack(tasks)
    errors = gate_signal_pack(pack)
    assert any("regime has cardinality 1" in e for e in errors)
    report = validate_pack(pack)
    assert report.ok is False


def test_sha_mismatch_fails_validate():
    tasks = [
        _task("a", 8, 0.04, "2026-01-01", "CALM", "Tech", "UP"),
        _task("b", 1, -0.04, "2026-01-02", "CRISIS", "Energy", "DOWN"),
    ]
    pack = _pack(tasks)
    pack["header"]["sha256_tasks"] = "0" * 64
    assert any("sha256" in e for e in gate_signal_pack(pack))
