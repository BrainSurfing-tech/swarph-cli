"""``swarph bench`` — deterministic LLM benchmark-pack runner (board card #101).

    swarph bench run --models <csv> --pack <path> [--report json|table]
    swarph bench validate <pack> [--reference-models <csv>]
    swarph bench add <pack-file> [--force]
    swarph bench prices [--refresh] [--grep <substr>]

Ported from the reference lab's showdown scripts per
``docs/2026-07-24-swarph-bench-pack-spec.md``; the distance math, backend
abstraction, and validate gates live in :mod:`swarph_cli.bench`. This module
is the thin argparse + dispatch + formatting layer, mirroring the existing
``commands/board.py`` pattern. Pack content is community-authored (untrusted)
text — every pack-derived string is sanitized (:func:`_s`) before it reaches
the terminal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from swarph_cli.bench import prices as prices_mod
from swarph_cli.bench.backends import Backend, MeteredGeminiBackend, SubscriptionBackend
from swarph_cli.bench.pack import PackError, load_pack, slugify_theme, validate_schema
from swarph_cli.bench.runner import ModelSpec, parse_models, preflight, run_pack
from swarph_cli.bench.validate import validate_pack
from swarph_cli.commands._display import sanitize_terminal as _s


# ── backend wiring (the real, network-capable seam; tests monkeypatch this) ──

def _default_backends() -> dict[str, Backend]:
    return {"metered": MeteredGeminiBackend(), "subscription": SubscriptionBackend()}


# ── formatters ─────────────────────────────────────────────────────────────

def _fmt_tok(row: dict) -> str:
    tag = " (ESTIMATED)" if row.get("estimated") else ""
    return f"{row['total_tokens']}{tag}"


def _format_run_table(result: dict) -> str:
    lines = [f"BENCH [{_s(result.get('theme'))}] — {len(result.get('detail', {}))} models x "
             f"{result['tasks_total']} tasks\n"]
    header = f"{'model':28} {'mean_dist':10} {'quality':8} {'tok':16} {'metered_$':12} {'lat_s':8} {'parse_fail':10} notes"
    lines.append(header)
    for row in result["board"]:
        quality = round(1 - row["mean_distance"], 3)
        notes = []
        if row.get("errors"):
            notes.append(f"{row['errors']} error(s)")
        if row["ran"] < row["tasks_total"]:
            notes.append(f"only {row['ran']}/{row['tasks_total']} ran")
        lat = f"{row['mean_latency_s']}s" if row["mean_latency_s"] is not None else "-"
        if row.get("estimated"):
            lat += " (est)"
        lines.append(
            f"{_s(row['label']):28} {row['mean_distance']:<10} {quality:<8} "
            f"{_fmt_tok(row):16} ${row['cost_usd']:<11.6f} {lat:8} {row['parse_fail']:<10} "
            f"{', '.join(notes)}"
        )
        lines.append("  per-class:")
        for cls, c in sorted(row.get("per_class", {}).items()):
            lines.append(
                f"    {_s(cls):20} n={c['n']:<4} hits={c['hits']:<4} "
                f"hit_rate={c['hit_rate']:<6} mean_distance={c['mean_distance']}"
            )
    return "\n".join(lines)


def _format_validate(report_dict: dict) -> str:
    lines = ["OK" if report_dict["ok"] else "INVALID"]
    for e in report_dict["errors"]:
        lines.append(f"  ERROR: {_s(e)}")
    for w in report_dict["warnings"]:
        lines.append(f"  WARN:  {_s(w)}")
    return "\n".join(lines)


def _format_prices(rows: dict[str, tuple[float, float]]) -> str:
    if not rows:
        return "(no cached prices — run `swarph bench prices --refresh`)"
    lines = [f"{'model':40} {'in $/1M':>10} {'out $/1M':>10}"]
    for model in sorted(rows):
        pin, pout = rows[model]
        lines.append(f"{_s(model):40} {pin:>10.4f} {pout:>10.4f}")
    return "\n".join(lines)


# ── parser ─────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph bench", description="Deterministic LLM benchmark-pack runner.")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run an N-way model showdown on a pack")
    r.add_argument("--models", required=True, help="comma list, id[:backend[:label]]")
    r.add_argument("--pack", required=True, help="path to a pack JSON file")
    r.add_argument("--report", choices=["json", "table"], default="table")
    r.add_argument("--strict", action="store_true",
                    help="abort (no dispatch) if ANY requested model lacks credentials, "
                         "instead of skipping it and running the rest")

    v = sub.add_parser("validate", help="run the 4 validate gates on a pack")
    v.add_argument("pack", help="path to a pack JSON file")
    v.add_argument("--reference-models", default=None, help="comma list for the discrimination check")
    v.add_argument("--json", action="store_true")

    a = sub.add_parser("add", help="validate + self-register a community pack (named from its own header)")
    a.add_argument("pack_file", help="path to the pack JSON to add")
    a.add_argument("--reference-models", default=None, help="comma list for the discrimination check")
    a.add_argument("--force", action="store_true", help="overwrite an existing packs/<theme>.json")
    a.add_argument("--packs-dir", default=None, help="override the packs/ registry directory (mainly for tests)")

    pr = sub.add_parser("prices", help="the LLM list-price table (shared cache; not bench-private)")
    pr.add_argument("--refresh", action="store_true", help="fetch fresh prices from LiteLLM and cache them")
    pr.add_argument("--grep", default=None, help="filter by model-id substring")
    pr.add_argument("--json", action="store_true")

    return p


def _default_packs_dir() -> Path:
    cwd_packs = Path.cwd() / "packs"
    if cwd_packs.is_dir():
        return cwd_packs
    try:
        repo_packs = Path(__file__).resolve().parents[3] / "packs"
        if repo_packs.is_dir():
            return repo_packs
    except IndexError:
        pass
    return cwd_packs


# ── dispatch ───────────────────────────────────────────────────────────────

def _cmd_run(args) -> int:
    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"swarph bench run: {exc}", file=sys.stderr)
        return 2
    errors, _ = validate_schema(pack)
    if errors:
        print("swarph bench run: pack failed schema validation:", file=sys.stderr)
        for e in errors:
            print(f"  {_s(e)}", file=sys.stderr)
        return 2

    specs = parse_models(args.models)
    backends = _default_backends()
    runnable, warnings = preflight(specs, backends)
    for w in warnings:
        print(f"swarph bench run: WARN {w}", file=sys.stderr)
    if warnings and args.strict:
        print("swarph bench run: --strict set and at least one model lacks credentials — "
              "aborting before any dispatch", file=sys.stderr)
        return 1
    if not runnable:
        print("swarph bench run: no runnable models (all lack credentials)", file=sys.stderr)
        return 1

    result = run_pack(runnable, pack, backends)
    if args.report == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_format_run_table(result))
    return 0


def _cmd_validate(args) -> int:
    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"swarph bench validate: {exc}", file=sys.stderr)
        return 2
    ref_models = [m.strip() for m in args.reference_models.split(",") if m.strip()] if args.reference_models else None
    report = validate_pack(pack, reference_models=ref_models, backends=_default_backends())
    d = report.as_dict()
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(_format_validate(d))
    return 0 if report.ok else 1


def _cmd_add(args) -> int:
    try:
        pack = load_pack(args.pack_file)
    except PackError as exc:
        print(f"swarph bench add: {exc}", file=sys.stderr)
        return 2

    ref_models = ([m.strip() for m in args.reference_models.split(",") if m.strip()]
                  if args.reference_models else None)
    report = validate_pack(pack, reference_models=ref_models, backends=_default_backends())
    for w in report.warnings:
        print(f"  WARN: {_s(w)}", file=sys.stderr)
    if not report.ok:
        print("swarph bench add: REFUSED — pack failed validate:", file=sys.stderr)
        for e in report.errors:
            print(f"  ERROR: {_s(e)}", file=sys.stderr)
        return 1

    theme = pack.get("theme")
    slug = slugify_theme(theme)
    packs_dir = Path(args.packs_dir) if args.packs_dir else _default_packs_dir()
    dest = packs_dir / f"{slug}.json"

    if dest.exists() and not args.force:
        print(
            f"swarph bench add: REFUSED — {dest} already exists (theme={theme!r} -> "
            f"slug={slug!r}); pass --force to overwrite",
            file=sys.stderr,
        )
        return 1

    packs_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(pack, indent=2) + "\n")
    print(f"swarph bench add: installed {dest} (theme={_s(theme)}, "
          f"{len(pack.get('tasks', []))} tasks)")
    return 0


def _cmd_prices(args) -> int:
    if args.refresh:
        from swarph_cli.bench import refresh_prices
        rc = refresh_prices.main()
        if rc != 0:
            return rc
    rows = prices_mod.all_cached(force=args.refresh)
    if args.grep:
        needle = args.grep.lower()
        rows = {m: v for m, v in rows.items() if needle in m.lower()}
    if args.json:
        print(json.dumps({m: {"in": v[0], "out": v[1]} for m, v in rows.items()}, indent=2))
    else:
        print(_format_prices(rows))
    return 0


def run_bench(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "prices":
        return _cmd_prices(args)
    print("swarph bench: unknown subcommand", file=sys.stderr)
    return 1
