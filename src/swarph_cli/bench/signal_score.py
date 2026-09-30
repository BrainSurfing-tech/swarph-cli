"""Robust scorer for a signal pack (#213).

Mean distance is the wrong lens for a categorical UP/FLAT/DOWN call: an arm
that always says FLAT can look precise while never separating the tails.
This scorer reports, per cell and only then pooled:

- n
- balanced accuracy (mean recall over the outcome labels present in the cell)
- the median forward return of the rows the arm called UP, and of the rows
  it called DOWN (the separation)
- a bootstrap confidence interval of that separation, resampling whole
  decision dates so rows that share a date stay together
"""
from __future__ import annotations

import random

LABELS = ("UP", "FLAT", "DOWN")
N_BOOT = 1000
SEED = 20260927


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def balanced_accuracy(expected: list[str], predicted: list[str]) -> float | None:
    recalls = []
    for lab in LABELS:
        idx = [i for i, e in enumerate(expected) if e == lab]
        if not idx:
            continue
        recalls.append(sum(1 for i in idx if predicted[i] == lab) / len(idx))
    if not recalls:
        return None
    return round(sum(recalls) / len(recalls), 4)


def cell_block(rows: list[dict]) -> dict:
    expected = [str(r.get("expected") or "") for r in rows]
    predicted = [str(r.get("predicted") or "") for r in rows]
    up = [float(r["fwd"]) for r in rows if r.get("predicted") == "UP" and isinstance(r.get("fwd"), (int, float))]
    down = [float(r["fwd"]) for r in rows if r.get("predicted") == "DOWN" and isinstance(r.get("fwd"), (int, float))]
    med_up = _median(up)
    med_down = _median(down)
    sep = None if med_up is None or med_down is None else round(med_up - med_down, 6)
    return {
        "n": len(rows),
        "balanced_accuracy": balanced_accuracy(expected, predicted),
        "n_called_up": len(up),
        "n_called_down": len(down),
        "median_fwd_called_up": None if med_up is None else round(med_up, 6),
        "median_fwd_called_down": None if med_down is None else round(med_down, 6),
        "separation": sep,
    }


def bootstrap_ci(rows: list[dict], *, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """95% interval of separation, resampling decision dates with replacement."""
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(str(row.get("date") or ""), []).append(row)
    dates = sorted(by_date)
    out = {
        "stat": "separation",
        "n_dates": len(dates),
        "n_boot": n_boot,
        "seed": seed,
        "lo": None,
        "hi": None,
    }
    if len(dates) < 2:
        return out
    rng = random.Random(seed)
    stats: list[float] = []
    n_dates = len(dates)
    for _ in range(n_boot):
        drawn: list[dict] = []
        for _i in range(n_dates):
            drawn.extend(by_date[dates[rng.randrange(n_dates)]])
        sep = cell_block(drawn)["separation"]
        if sep is not None:
            stats.append(sep)
    if not stats:
        return out
    stats.sort()
    last = len(stats) - 1
    out["lo"] = round(stats[int(0.025 * last)], 6)
    out["hi"] = round(stats[int(0.975 * last)], 6)
    return out


def robust_report(obs: list[dict]) -> dict:
    """Per-cell table first. Pooled is present only as a sibling of that table."""
    per_cell: dict[str, dict] = {}
    for cls in sorted({str(o.get("cls") or "") for o in obs}):
        rows = [o for o in obs if str(o.get("cls") or "") == cls]
        block = cell_block(rows)
        block["bootstrap_ci"] = bootstrap_ci(rows)
        per_cell[cls] = block
    pooled = cell_block(obs)
    pooled["bootstrap_ci"] = bootstrap_ci(obs)
    return {"per_cell": per_cell, "pooled": pooled}
