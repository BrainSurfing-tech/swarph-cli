"""``swarph dreaming`` — between-sessions memory verify / organize / enrich (#684 / #656).

Distribution surface for the dreaming pass that lives as plain modules under
``swarph_cli.dreaming``. This verb does NOT schedule anything and does NOT
write the live corpus through `run`: that legacy verb clones/reports with GC4i.
Separate `propose`, `apply`, `restore` and `recall-score` verbs implement v2
consolidation; only explicit apply/restore can write live memory.

Exit codes (unchanged from #656 — do not collapse to 0/1):
  0  adjudicated >0 claims AND no disagreements
  1  disagreements / surface_disagreements found
  2  refused to run (e.g. dirty destination)
  3  ran but adjudicated nothing

Scope: SINGLE-CELL. Runs against THIS cell's own corpus. It is not the
cross-agent multi-transcript dreaming from the Anthropic talk; transcripts
are not a shared mesh surface in v1.
"""

from __future__ import annotations

import argparse
import sys


def _add_consolidation_parsers(sub):
    p = sub.add_parser("propose", help="propose consolidation with an isolated subscription harness")
    reporting = [p]
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", help="empty private proposal directory; default: a new run under per-cell state")
    p.add_argument("--cell", required=True)
    p.add_argument("--state-root", default=None)
    p.add_argument("--harness", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--token-cap", type=int, default=32000)
    p.add_argument("--questions", help="optional frozen pilot question set, pinned before first propose")
    for action in ("apply", "restore"):
        p = sub.add_parser(action, help=action + " with whole-corpus backup and a corpus lock")
        reporting.append(p)
        p.add_argument("--cell", required=True)
        p.add_argument("--corpus", required=True)
        p.add_argument("--state-root", default=None)
        p.add_argument("--idle-minutes", type=float, default=15)
        p.add_argument("--keep-backups", type=int, default=5)
        if action == "apply":
            p.add_argument("--proposal", required=True)
            p.add_argument("--allow-owner-notes", action="store_true")
        else:
            p.add_argument("--backup", required=True)
    for p in reporting:
        p.add_argument("--notify-card", type=int, help="post counts/basenames/op types only to a fleet card")
        p.add_argument("--gateway", default=None)
        p.add_argument("--token-file", default=None)
    p = sub.add_parser("recall-score", help="score externally supplied paired answers against frozen questions")
    for option in ("cell", "corpus", "questions", "before", "after"):
        p.add_argument("--" + option, required=True)
    p.add_argument("--state-root", default=None)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph dreaming",
        description=(
            "Periodic memory dreaming pass: VERIFY, ORGANIZE, ENRICH against a "
            "CLONE of your corpus with `run`; it never writes live.\n"
            "V2: propose creates reviewed JSON edits; apply/restore explicitly write live\n"
            "with a backup, per-file guards and a corpus lock. Templates are NOT installed.\n\n"
            "SINGLE-CELL SCOPE: operates on one cell's own memory + local "
            "transcripts. Not cross-agent dreaming.\n\n"
            "Exit codes:\n"
            "  0  adjudicated >0 claims AND no disagreements\n"
            "  1  findings (disagree / surface_disagreement)\n"
            "  2  refused to run\n"
            "  3  ran but adjudicated nothing\n\n"
            "Enrich: when the SLM client (workers.slm_client) is absent — the\n"
            "normal installed-wheel case — enrich is skipped and the report\n"
            "says 'enrich skipped: no SLM client' (never exit 1 as findings).\n"
            "Pass --enrich to require it (rc=2 if unavailable)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="action", required=True)
    _add_consolidation_parsers(sub)

    run = sub.add_parser(
        "run",
        help="clone corpus to --out, verify/organize/enrich, print report",
        description=(
            "Clone --corpus into --out, then VERIFY / ORGANIZE / ENRICH the "
            "clone. The live corpus is left byte-identical. Propagate the exit "
            "code if you schedule this — do not wrap with `exit 0`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("--corpus", required=True, help="path to the live memory corpus directory")
    run.add_argument("--out", required=True, help="empty destination for the clone + report")
    run.add_argument(
        "--cursor",
        default=None,
        help="transcript cursor JSON (default: ~/.dreaming-cursor.json)",
    )
    run.add_argument("--no-enrich", action="store_true", help="skip transcript enrich stage")
    run.add_argument(
        "--enrich", action="store_true",
        help="require enrich; refuse (rc=2) if SLM client unavailable (#734)",
    )
    run.add_argument("--verify-only", action="store_true", help="verify only; skip organize/enrich")
    run.add_argument(
        "--notify",
        default=None,
        metavar="CELL",
        help="DM CELL when the finding set changes; no DM when it is unchanged (#937)",
    )
    return p


def run_dreaming(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow `swarph dreaming --help` without a subcommand.
    if not argv or argv[0] in ("-h", "--help"):
        _build_parser().print_help()
        return 0
    args = _build_parser().parse_args(argv)
    if args.action != "run":
        from swarph_cli.dreaming.consolidate import execute
        return execute(args)

    from pathlib import Path
    from swarph_cli.dreaming import run as dreaming_run

    forwarded = ["--corpus", args.corpus, "--out", args.out]
    if args.cursor:
        forwarded.extend(["--cursor", args.cursor])
    else:
        forwarded.extend(["--cursor", str(Path.home() / ".dreaming-cursor.json")])
    if args.no_enrich:
        forwarded.append("--no-enrich")
    if getattr(args, "enrich", False):
        forwarded.append("--enrich")
    if args.verify_only:
        forwarded.append("--verify-only")
    if getattr(args, "notify", None):
        forwarded.extend(["--notify", args.notify])
    return dreaming_run.main(forwarded)
