"""``swarph import`` — Phase 2.5 implementation.

Per PLAN.md §17.6 forward-reorder, Phase 2.5 ships:
- Parser + converter for Claude session JSONL
- Import-report writer
- ``--report-only`` mode (no writes, no continuation)

Phase 3.5+ adds gemini + llm source formats. Phase 5+ adds
``--continue`` for live REPL integration.

Per PLAN.md §17.4 the subcommand spec:

    swarph import <source-path>
                  [--source-format claude|gemini|llm|chatgpt-export]
                  [--report-only]                   # v0.2.0 — this release
                  [--target-session NAME]           # v0.2.0 (write path)
                  [--force]                         # v0.2.0 (write path)
                  [--continue]                      # Phase 5+
                  [--provider gemini|...]           # Phase 5+
                  [--last N]                        # Phase 5+

v0.2.0 implements `--report-only` as the primary path. Write +
target-session + force land in the same release because they
share the cursor.json output codepath. `--continue` defers to
Phase 5+ (gates on REPL).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from swarph_cli.parsers import ClaudeParser, ImportResult


SUPPORTED_FORMATS = {"claude"}  # gemini + llm + chatgpt-export land in 3.5+/6+


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph import",
        description=(
            "Import a session from another CLI into swarph-native format. "
            "v0.2.0 ships --report-only + write paths for Claude JSONL "
            "sessions; teleport (--continue) lands in Phase 5+."
        ),
    )
    p.add_argument(
        "source_path",
        help="Path to source session file (e.g. ~/.claude/projects/.../X.jsonl).",
    )
    p.add_argument(
        "--source-format",
        default=None,
        choices=sorted(SUPPORTED_FORMATS),
        help="Override source-format detection (currently only 'claude' supported).",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="Print the import report and exit; do NOT write a swarph-native "
        "session. Use this to inspect what would be imported (and what would "
        "be lost) before committing.",
    )
    p.add_argument(
        "--target-session",
        default=None,
        help="Custom name for the swarph-native session file (default: source "
        "session-id or filename stem). Stored at ~/.swarph/sessions/<NAME>.jsonl.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target session file if it already exists. Without "
        "--force, swarph import refuses-with-error to prevent silently "
        "destroying continuation turns added since a prior import (drop PR "
        "#138 review concern (g)).",
    )
    p.add_argument(
        "--json-report",
        action="store_true",
        help="Emit the import report as a JSON object on stdout instead of "
        "human-readable text. Useful for downstream tooling.",
    )
    return p


def _detect_format(path: Path) -> Optional[str]:
    """Heuristic source-format detection. Phase 2.5 only knows 'claude'."""
    pstr = str(path).lower()
    if ".claude/projects/" in pstr or path.suffix == ".jsonl":
        # Sniff first line for Claude record-type discriminator
        try:
            with path.open(encoding="utf-8") as f:
                first = f.readline().strip()
            if first:
                rec = json.loads(first)
                if isinstance(rec, dict) and rec.get("type") in {
                    "user", "assistant", "system", "permission-mode",
                    "file-history-snapshot", "attachment", "queue-operation",
                }:
                    return "claude"
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _safe_session_filename(raw: str) -> str:
    """Reduce an arbitrary session identifier to a SAFE basename confined to
    the sessions dir.

    The identifier may come from the parsed JSONL (``session_id``), which is
    untrusted when importing a foreign file: an absolute value (``/etc/cron.d/
    evil``) would make ``base / name`` discard ``base`` entirely, and ``../``
    would escape it (adversarial-sweep path-traversal). Strip directory
    components and whitelist the charset so the result is always a plain
    filename inside the sessions dir.
    """
    # ``Path(...).name`` drops any directory components, incl a leading "/".
    base_name = Path(str(raw)).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base_name)
    # Reject names that are empty or pure dots ("", ".", "..", ...).
    if not safe.strip("."):
        safe = "imported-session"
    return safe


def _swarph_native_path(target_session: Optional[str], result: ImportResult) -> Path:
    """Resolve the target swarph-native session file path (path-traversal-safe)."""
    base = Path.home() / ".swarph" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    raw = target_session or (
        result.metadata.get("session_id")
        or Path(result.report.source_path).stem
    )
    name = _safe_session_filename(raw)
    if not name.endswith(".jsonl"):
        name = f"{name}.jsonl"
    out = base / name
    # Defense in depth: the sanitized name has no separators, so this always
    # holds — but assert the write target is a direct child of the sessions dir.
    if out.resolve().parent != base.resolve():
        raise ValueError(
            f"import: refusing to write session outside {base} (got {out})"
        )
    return out


def _write_swarph_native_session(
    out_path: Path,
    result: ImportResult,
) -> None:
    """Write the swarph-native JSONL per PLAN.md §17.5.

    First line is the metadata header; subsequent lines are
    role+content turns with `_meta.source = "imported"` per drop's
    PR #138 review carry-forward (f).
    """
    header = {
        "_meta": {
            "version": "v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "imported_from": {
                "format": result.report.source_format,
                "path": result.report.source_path,
                "session_id": result.report.session_id,
            },
            "import_report": result.report.to_dict(),
        }
    }
    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header, separators=(",", ":")) + "\n")
        for m in result.messages:
            row = {
                "role": m.role,
                "content": m.content,
                "_meta": {"source": "imported"},
            }
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def run_import(argv: list[str]) -> int:
    """Entry point for the ``swarph import`` verb."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    src_path = Path(args.source_path).expanduser()
    if not src_path.exists():
        print(f"swarph import: source file not found: {src_path}", file=sys.stderr)
        return 2

    fmt = args.source_format or _detect_format(src_path)
    if fmt is None:
        print(
            f"swarph import: could not detect source format for {src_path}; "
            f"specify with --source-format. Supported: {sorted(SUPPORTED_FORMATS)}",
            file=sys.stderr,
        )
        return 2
    if fmt not in SUPPORTED_FORMATS:
        print(
            f"swarph import: source-format '{fmt}' not supported in v0.2.0; "
            f"Phase 3.5+ adds gemini + llm; Phase 6+ adds chatgpt-export.",
            file=sys.stderr,
        )
        return 2

    # Parse
    try:
        if fmt == "claude":
            result = ClaudeParser().parse(src_path)
        else:  # pragma: no cover — guarded above
            raise NotImplementedError(fmt)
    except (FileNotFoundError, ValueError) as exc:
        print(f"swarph import: parse failed: {exc}", file=sys.stderr)
        return 2

    # Print report
    if args.json_report:
        print(json.dumps(result.report.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.report.render_human())

    if args.report_only:
        return 0

    # Write path — refuse-with-error default per drop's PR #138 review (g)
    out_path = _swarph_native_path(args.target_session, result)
    if out_path.exists() and not args.force:
        # Show diff-summary so the operator knows what they'd be
        # destroying — same-shape as `git commit --amend` requiring
        # explicit intent on already-pushed commits.
        try:
            existing_lines = sum(1 for _ in out_path.open(encoding="utf-8"))
        except OSError:
            existing_lines = -1
        new_total_lines = 1 + len(result.messages)  # header + turns
        print(
            f"swarph import: target {out_path} already exists "
            f"({existing_lines} existing lines vs {new_total_lines} would-be lines).\n"
            f"Refuse-with-error default protects swarph-continuation turns "
            f"added since the prior import.\n"
            f"To proceed:\n"
            f"  --force                  overwrite (destroys continuation turns)\n"
            f"  --target-session NAME    write to a different file",
            file=sys.stderr,
        )
        return 3

    _write_swarph_native_session(out_path, result)
    print(f"\nwrote swarph-native session: {out_path}", file=sys.stderr)
    return 0
