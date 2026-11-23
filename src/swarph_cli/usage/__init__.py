"""Card #1445 phase 1b — usage pools: map, rows, table, burn.

One table over ``~/swarph_state/usage/*.json``: pool, used %, resets (UTC
and in-hours from now), read age, burn per hour when two readings exist.
A reading older than ``stale_after_hours`` (6h, from the data file) is
marked STALE in its row, never presented as current. A pool with no file
still appears as missing, visibly not current.

The cell-to-pool map lives in the packaged ``pools.json`` DATA file (an
optional ``.pools.local.json`` dotfile in the usage dir replaces it), so
editing the map never requires a code change.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:  # py3.9+; the packaged-resource reader, wheel-safe like guide.py
    from importlib.resources import files as _files
except ImportError:  # pragma: no cover - floor is 3.10
    _files = None  # type: ignore[assignment]

_PACKAGE = "swarph_cli.usage"
_POOLS_FILENAME = "pools.json"
_LOCAL_OVERRIDE = ".pools.local.json"
SNAPSHOT_NAME = ".usage-last-seen.json"

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"


def default_state_root() -> Path:
    """The codebase's state-root override (cf. wake_watchdog.py)."""
    raw = os.environ.get("SWARPH_STATE_ROOT")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "swarph_state"


def default_usage_dir(state_root: "str | Path | None" = None) -> Path:
    """``<state-root>/usage`` — where the per-pool JSON readings live."""
    root = Path(state_root).expanduser() if state_root else default_state_root()
    return root / "usage"


def _packaged_pools_text() -> str:
    if _files is not None:
        try:
            return (_files(_PACKAGE) / _POOLS_FILENAME).read_text(encoding="utf-8")
        except (OSError, ValueError):
            pass
    # Source-tree fallback (editable checkout without importlib.resources).
    here = Path(__file__).resolve().parent / _POOLS_FILENAME
    return here.read_text(encoding="utf-8")


def load_pool_map(usage_dir: "str | Path | None" = None) -> dict:
    """The cell-to-pool map: packaged default, dotfile override replaces it."""
    data = json.loads(_packaged_pools_text())
    if usage_dir is not None:
        override = Path(usage_dir) / _LOCAL_OVERRIDE
        if override.is_file():
            with open(override, encoding="utf-8") as handle:
                data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("pools"), list):
        raise ValueError("pool map has no pools list")
    stale = data.get("stale_after_hours", 6)
    if isinstance(stale, bool) or not isinstance(stale, (int, float)):
        raise ValueError("stale_after_hours is not a number")
    return data


def parse_time(value) -> "datetime | None":
    """ISO Z string or epoch seconds -> aware UTC datetime, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pool_for_file(pool_map: dict, filename: str, provider) -> "dict | None":
    """Which pool a usage file belongs to, by filename then provider."""
    for pool in pool_map["pools"]:
        if filename in (pool.get("files") or []):
            return pool
    if isinstance(provider, str):
        for pool in pool_map["pools"]:
            if provider in (pool.get("providers") or []):
                return pool
    return None


def read_document(path: Path) -> "dict | None":
    """One usage file as a dict, or None when unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def windows_from_file(path: Path) -> "tuple | None":
    """[(window, used_pct, resets_iso)] for one pool file, None if unreadable.

    Two live shapes: ``windows{name:{used_percentage, resets_at}}`` and the
    google ``quota{name:{remaining_fraction, reset_time}}`` block, where
    used% = 100 * (1 - remaining_fraction). Unknown shapes are ignored
    (None), never zeroed.
    """
    document = read_document(path)
    if document is None:
        return None
    read_at = parse_time(document.get("read_at"))
    if read_at is None:
        return None
    windows = document.get("windows")
    quota = document.get("quota")
    rows: list[tuple] = []
    if isinstance(windows, dict):
        for name, entry in windows.items():
            if not isinstance(entry, dict):
                continue
            used = entry.get("used_percentage")
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                continue
            resets = parse_time(entry.get("resets_at"))
            rows.append((str(name), float(used),
                         _iso_z(resets) if resets else None,
                         _iso_z(read_at),
                         document.get("provider"), document.get("source")))
    elif isinstance(quota, dict):
        for name, entry in quota.items():
            if not isinstance(entry, dict):
                continue
            remaining = entry.get("remaining_fraction")
            if (isinstance(remaining, bool)
                    or not isinstance(remaining, (int, float))):
                continue
            resets = parse_time(entry.get("reset_time"))
            rows.append((str(name), 100.0 * (1.0 - float(remaining)),
                         _iso_z(resets) if resets else None,
                         _iso_z(read_at),
                         document.get("provider"), document.get("source")))
    else:
        return None
    return rows


def _pool_files(usage_dir: Path) -> list:
    """Pool files only: dotfiles (burn snapshot, local override) never pools."""
    try:
        entries = sorted(usage_dir.glob("*.json"))
    except OSError:
        return []
    return [p for p in entries if p.is_file() and not p.name.startswith(".")]


def collect_rows(*, usage_dir, pool_map, now: "datetime | None" = None,
                 snapshot: "dict | None" = None) -> list:
    """One row per pool+window in map order; missing pools keep a row."""
    moment = now or datetime.now(timezone.utc)
    stale_after_h = float(pool_map.get("stale_after_hours", 6))
    seen: dict = {}
    for path in _pool_files(Path(usage_dir)):
        document = read_document(path)
        provider = (document.get("provider") if document else None)
        pool = pool_for_file(pool_map, path.name, provider)
        if pool is None:
            continue
        parsed = windows_from_file(path)
        if not parsed:
            continue
        for window, used, resets_iso, read_iso, _prov, source in parsed:
            key = (pool["pool"], window)
            if key in seen:
                continue
            read_at = parse_time(read_iso)
            resets_at = parse_time(resets_iso)
            age_h = (moment - read_at).total_seconds() / 3600.0
            stale = age_h > stale_after_h
            resets_in_h = ((resets_at - moment).total_seconds() / 3600.0
                           if resets_at else None)
            row = {
                "pool": pool["pool"],
                "window": window,
                "cells": list(pool.get("cells") or []),
                "source": source,
                "used_pct": used,
                "resets_at": resets_iso,
                "resets_in_h": resets_in_h,
                "read_at": read_iso,
                "age_h": age_h,
                "stale": stale,
                "status": STATUS_STALE if stale else STATUS_OK,
                "burn_per_h": None,
            }
            if snapshot is not None:
                prev_map = snapshot.get("readings")
                if not isinstance(prev_map, dict):
                    prev_map = {}
                prev = prev_map.get(f"{pool['pool']}\x00{window}")
                if isinstance(prev, dict):
                    prev_read = parse_time(prev.get("read_at"))
                    prev_used = prev.get("used_percentage")
                    if (prev_read is not None and read_at is not None
                            and read_at > prev_read
                            and isinstance(prev_used, (int, float))
                            and not isinstance(prev_used, bool)):
                        delta_h = ((read_at - prev_read).total_seconds()
                                   / 3600.0)
                        if delta_h > 0:
                            row["burn_per_h"] = (
                                (used - float(prev_used)) / delta_h)
            seen[key] = row
    rows = [seen[k] for k in sorted(seen)]
    matched_pools = {pool for pool, _window in seen}
    for pool in pool_map["pools"]:
        if pool["pool"] not in matched_pools:
            rows.append({
                "pool": pool["pool"],
                "window": None,
                "cells": list(pool.get("cells") or []),
                "source": None,
                "used_pct": None,
                "resets_at": None,
                "resets_in_h": None,
                "read_at": None,
                "age_h": None,
                "stale": False,
                "status": STATUS_MISSING,
                "burn_per_h": None,
            })
    order = [p["pool"] for p in pool_map["pools"]]
    rows.sort(key=lambda r: (order.index(r["pool"])
                             if r["pool"] in order else len(order),
                             r["window"] or ""))
    return rows


def load_snapshot(usage_dir: Path) -> dict:
    """Last-seen readings for burn/hr. Missing or corrupt -> empty."""
    try:
        with open(Path(usage_dir) / SNAPSHOT_NAME, encoding="utf-8") as h:
            data = json.load(h)
    except (OSError, ValueError):
        return {"readings": {}}
    if not isinstance(data, dict) or not isinstance(data.get("readings"), dict):
        return {"readings": {}}
    return data


def save_snapshot(usage_dir: Path, rows: list, snapshot: dict) -> None:
    """Merge the current readings in. Never raises: burn is advisory."""
    try:
        prior = snapshot.get("readings")
        readings = dict(prior) if isinstance(prior, dict) else {}
        for row in rows:
            if row["status"] == STATUS_MISSING or row["read_at"] is None:
                continue
            readings[f"{row['pool']}\x00{row['window']}"] = {
                "used_percentage": row["used_pct"],
                "read_at": row["read_at"],
            }
        target_dir = Path(usage_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / SNAPSHOT_NAME
        tmp = target.with_name(SNAPSHOT_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"readings": readings}, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError:
        pass


def _fmt_pct(used) -> str:
    return "—" if used is None else f"{used:.1f}%"


def _fmt_resets(row) -> tuple:
    if row["resets_at"] is None:
        return "—", "—"
    utc = row["resets_at"].replace("T", " ").removesuffix("Z") + "Z"
    hours = row["resets_in_h"]
    if hours is None:
        return utc, "—"
    if hours >= 0:
        return utc, f"in {hours:.1f}h"
    return utc, f"{-hours:.1f}h ago"


def _fmt_age(age_h) -> str:
    if age_h is None:
        return "—"
    if age_h < 0:
        return "future?"
    if age_h < 1:
        return f"{age_h * 60:.0f}m"
    if age_h < 48:
        return f"{age_h:.1f}h"
    return f"{age_h / 24:.1f}d"


def _fmt_burn(burn) -> str:
    return "" if burn is None else f"{burn:+.2f}%/h"


def render_table(rows: list) -> str:
    """The one pools table: pool, used %, resets, age, burn/h, note."""
    widths = {"pool": 4, "window": 6, "used": 5, "resets": 6, "in": 2,
              "age": 3, "burn": 6, "note": 4}
    body = []
    for row in rows:
        resets_utc, in_h = _fmt_resets(row)
        note = ""
        if row["status"] == STATUS_MISSING:
            note = "missing"
        elif row["stale"]:
            note = "STALE"
        cells = {
            "pool": row["pool"],
            "window": row["window"] or "—",
            "used": _fmt_pct(row["used_pct"]),
            "resets": resets_utc,
            "in": in_h,
            "age": _fmt_age(row["age_h"]),
            "burn": _fmt_burn(row["burn_per_h"]),
            "note": note,
        }
        body.append(cells)
        for key, value in cells.items():
            widths[key] = max(widths[key], len(value))
    header = (f"{'pool':<{widths['pool']}}  {'window':<{widths['window']}}  "
              f"{'used':>{widths['used']}}  {'resets (UTC)':<{widths['resets']}}  "
              f"{'in':<{widths['in']}}  {'age':<{widths['age']}}  "
              f"{'burn/h':<{widths['burn']}}  {'note':<{widths['note']}}")
    lines = [header, "-" * len(header)]
    for cells in body:
        lines.append(
            f"{cells['pool']:<{widths['pool']}}  "
            f"{cells['window']:<{widths['window']}}  "
            f"{cells['used']:>{widths['used']}}  "
            f"{cells['resets']:<{widths['resets']}}  "
            f"{cells['in']:<{widths['in']}}  "
            f"{cells['age']:<{widths['age']}}  "
            f"{cells['burn']:<{widths['burn']}}  "
            f"{cells['note']:<{widths['note']}}".rstrip())
    return "\n".join(lines) + "\n"


def rows_to_json(rows: list) -> dict:
    """The same rows as JSON: rounded floats, no formatting loss of meaning."""
    out = []
    for row in rows:
        out.append({
            "pool": row["pool"],
            "window": row["window"],
            "cells": row["cells"],
            "source": row["source"],
            "used_pct": row["used_pct"],
            "resets_at": row["resets_at"],
            "resets_in_h": (None if row["resets_in_h"] is None
                            else round(row["resets_in_h"], 2)),
            "read_at": row["read_at"],
            "age_h": (None if row["age_h"] is None
                      else round(row["age_h"], 2)),
            "stale": row["stale"],
            "status": row["status"],
            "burn_per_h": (None if row["burn_per_h"] is None
                           else round(row["burn_per_h"], 4)),
        })
    return {"rows": out}
