#!/usr/bin/env python3
"""Integrity check for the memory index — the damage nothing was watching for.

WHY THIS EXISTS (2026-09-02). MEMORY.md line 120 ended mid-link:

    … [atoms/cells/organs](project_substrate_atoms_cells_organs.

no closing paren, no remaining pointers. It silently removed NINE substrate-paper
files from every retrieval path, and survived indefinitely because **a truncated
line still looks like a line**. The same sweep found 41 memory files reachable
from neither index — including two written that same day.

The index's own header warns "over budget = SILENT partial load (18 lost
2026-08-26)", so the failure mode was known and had already happened twice. What
was missing is the only thing that catches it: something that reads the index and
asks whether it still points at what exists.

FIVE CHECKS, each for a failure that produced real loss:

  1. ORPHANS      a memory file no index points at — invisible when gbrain is down
                  and dependent on semantic luck when it is up
  2. TRUNCATION   an index line with an unclosed `(` — the line 120 defect
  3. DANGLING     a pointer to a file that no longer exists — the reverse rot
  4. BUDGET       MEMORY.md over 24985 BYTES (wc -c, NOT len(str) — ⭐/— are
                  multi-byte, so a character count reads ~600 low and says
                  "under" for a file that is over)
  5. LINES        MEMORY.md over the harness's 200-line load (#938). Bytes and
                  lines are independent limits — long lines hit bytes first;
                  short ones hit lines first (droplet counter-specimen).

Exit 0 = clean, 1 = findings. Read-only; it never edits the index.
Run: python3 ~/tools/memory-index-check.py [--quiet]
"""
from __future__ import annotations

import pathlib
import re
import sys

MEM = pathlib.Path(
    next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--mem-dir=")),
         str(pathlib.Path.home() / ".claude/projects/-home-ubuntu/memory")))
INDEXES = ("MEMORY.md", "MEMORY_FULL.md")
BUDGET = 24985  # bytes, MEMORY.md only — stated in the file's own header comment
# THE THIRD STATE. Without WARN this check reported "clean" at 24,972 bytes —
# 13 bytes of headroom — which is the exact condition ORGANIZE exists to
# prevent, sitting green in the instrument meant to catch it. It read clean
# continuously from 23,486 to 24,985 and would then hard-fail on ONE added
# pointer. Under target / in the band / over are three different states and a
# binary files the middle one under "fine". (a peer, 2026-09-03.)
# WARN does NOT change the exit code: this runs as a hook on every
# Write|Edit|Bash across several cells, and turning a healthy-but-tight file
# into a non-zero exit would be a fleet change, not a check.
TARGET = int(BUDGET * 0.94)  # 23485 bytes; the band is TARGET..BUDGET
# Claude Code truncates MEMORY.md after line 200 at session start (#938).
LINE_LIMIT = 200
LINE_TARGET = int(LINE_LIMIT * 0.94)  # 188
LINK = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")
TITLE_LINK = re.compile(r"\[([^\]]+)\]\(([A-Za-z0-9_./-]+\.md)\)")
# A line is suspect when it opens a link it never closes. An explicit "…" means
# the author elided on purpose, which is a different thing from a cut.
TRUNC = re.compile(r"\([A-Za-z0-9_./-]*$")


def cut_point(text: str) -> int:
    """First 1-based line the harness drops: byte ceiling or line 201, whichever first.

    cut_line = min(first line where cumulative bytes > BUDGET, LINE_LIMIT + 1).
    Naming that line (and the pointers at/after it) is the finding payload —
    "600 bytes over" does not say which memories went invisible (#938 droplet).
    """
    cum = 0
    lines = text.split("\n")
    for n, line in enumerate(lines, 1):
        if n > LINE_LIMIT:
            return LINE_LIMIT + 1
        # recreate file bytes: join with \n except we count each line's UTF-8
        # plus the newline that follows it (except possibly the last). Mirror
        # how the file is stored: "\n".join(lines).encode — so newlines between
        # lines, none after the final line if the file had no trailing \n.
        piece = line if n == 1 else "\n" + line
        cum += len(piece.encode("utf-8"))
        if cum > BUDGET:
            return n
    return 0


def pointer_titles_at_or_after(text: str, cut_line: int) -> list[str]:
    """Pointer entries at or after cut_line — the names that go invisible."""
    out: list[str] = []
    for n, line in enumerate(text.split("\n"), 1):
        if n < cut_line:
            continue
        m = TITLE_LINK.search(line)
        if m:
            out.append("%s -> %s" % (m.group(1), m.group(2)))
        elif LINK.search(line):
            out.append(LINK.search(line).group(1))
    return out


def links_in(name: str) -> set[str]:
    p = MEM / name
    return set(LINK.findall(p.read_text(encoding="utf-8"))) if p.exists() else set()


def deployment_population() -> int:
    """How many live sessions are actually RUNNING this hook? Usually zero.

    THE FINDING THIS ANSWERS (2026-09-03, a peer + a peer). A hook
    written to settings.json binds only on sessions started AFTER the write, so
    its deployment population at the moment of writing is ZERO. It is maximally
    un-deployed exactly when it feels finished, and it deploys one session at a
    time, invisibly, as cells restart.

    Worse here than elsewhere, because long-lived sessions are POLICY on this
    box -- the long session IS a cell's continuity. So the cells with the BEST
    continuity run the OLDEST control set, and nothing reports the gap.
    Measured when this was written: 3 of 18 claude processes covered, and all
    three were transient `claude -p` helpers. ZERO interactive cells.

    Do NOT prove a hook fires by reading stdout: PostToolUse stdout is
    discarded on success, so silence cannot tell "did not fire" from "fired and
    was swallowed". That test has no resolving power. Use a side effect.
    """
    import datetime, re, subprocess
    st = pathlib.Path.home() / ".claude/settings.json"
    if not st.exists():
        return -1
    written = datetime.datetime.fromtimestamp(st.stat().st_mtime)
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,lstart,args", "--no-headers"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        return -1
    live = cov = 0
    for line in out.split("\n"):
        if "claude" not in line or "grep" in line:
            continue
        m = re.match(r"\s*(\d+)\s+(\w{3} \w{3}\s+\d+ [\d:]+ \d{4})\s", line)
        if not m:
            continue
        try:
            t = datetime.datetime.strptime(m.group(2), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        # SCOPE: only the claude BINARY can host a hook. The first version of
        # this counted every `ps` line containing "claude" -- which swept in
        # `claude-service` and the `/bin/bash -c source .../shell-snapshots/...`
        # wrapper that EVERY Bash tool call spawns. Those wrappers were not
        # padding the denominator, they WERE the numerator: short-lived children
        # spawned seconds earlier BY the uncovered sessions. The control read
        # 17% deployed when it was 0% deployed. (a peer, 2026-09-03 --
        # caught because our two independently-derived numbers nearly AGREED,
        # which felt like confirmation and was two instruments sharing a flaw.)
        # `ps -eo pid,lstart,args`: lstart is FIVE fields (Fri Aug 28 02:54:27
        # 2026), so args begins at index 6. Using index 3 read the DAY NUMBER,
        # matched nothing, and reported "0 of 0" -- which renders identically to
        # a real "nothing is covered" while actually meaning "this instrument
        # saw nothing at all". A denominator of zero is a REFUSAL, not a result.
        parts = line.split()
        exe = parts[6] if len(parts) > 6 else ""
        if not (exe == "claude" or exe.rstrip("/").endswith("/claude")):
            continue
        if "bg-pty-host" in line or "bg-spare" in line:
            continue
        live += 1
        cov += t > written
    if live == 0:
        print("deployment: CANNOT REPORT -- found no hook-hosting claude process at all. "
              "That is an instrument failure, not a coverage result: a real 0-of-N and a "
              "broken 0-of-0 read the same in any summary that prints only the numerator.")
        return -1
    print("deployment: this hook is IN FORCE on %d of %d live claude session(s) "
          "(settings.json written %s). A session started before that write does NOT "
          "run it, however correct the file is."
          % (cov, live, written.strftime("%Y-%m-%d %H:%M:%S")))
    return cov


def main() -> int:
    quiet = "--quiet" in sys.argv
    if "--deployment" in sys.argv:
        return 0 if deployment_population() >= 0 else 1
    if not MEM.is_dir():
        print(f"memory-index-check: {MEM} is not a directory", file=sys.stderr)
        return 1

    files = {p.name for p in MEM.glob("*.md")} - set(INDEXES)
    linked: set[str] = set()
    for i in INDEXES:
        linked |= links_in(i)

    findings: list[str] = []

    orphans = sorted(files - linked)
    if orphans:
        findings.append(f"ORPHANS: {len(orphans)} memory file(s) in no index")
        findings += [f"    {o}" for o in orphans[:20]]
        if len(orphans) > 20:
            findings.append(f"    … and {len(orphans) - 20} more")

    for idx in INDEXES:
        p = MEM / idx
        if not p.exists():
            findings.append(f"MISSING INDEX: {idx}")
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            # Only flag list rows: comment blocks legitimately span lines.
            if line.startswith("- ") and TRUNC.search(line):
                findings.append(f"TRUNCATED: {idx}:{n} ends mid-link — ...{line[-46:]}")

    dangling = sorted(linked - files)
    if dangling:
        findings.append(f"DANGLING: {len(dangling)} pointer(s) to a file that does not exist")
        findings += [f"    {d}" for d in dangling[:20]]

    # Duplicate-target pointers under different titles: allowed, but listed (#938).
    mm = MEM / "MEMORY.md"
    if mm.exists():
        by_target: dict[str, set[str]] = {}
        for line in mm.read_text(encoding="utf-8").split("\n"):
            for title, target in TITLE_LINK.findall(line):
                by_target.setdefault(target, set()).add(title)
        dupes = sorted((t, sorted(titles)) for t, titles in by_target.items()
                       if len(titles) > 1)
        if dupes:
            findings.append(
                "DUP_TARGET: %d file(s) pointed at under different titles (allowed, listed)"
                % len(dupes))
            for target, titles in dupes[:20]:
                findings.append("    %s <- %s" % (target, " | ".join(titles)))

    warn = None
    if mm.exists():
        text = mm.read_text(encoding="utf-8")
        size = len(mm.read_bytes())
        nlines = len(text.split("\n"))
        # Bytes keep the WARN band (TARGET..BUDGET, exit 0). Lines do not:
        # over LINE_TARGET is a FINDING — lab specimen was 197/200 still "clean"
        # on bytes while 3 more pointers would be cut (#938 accept).
        if BUDGET >= size > TARGET and nlines <= LINE_TARGET:
            warn = ("WARN: MEMORY.md is %d BYTES — under the %d ceiling but inside the last %d "
                    "bytes of it. Not broken, not fine: %d bytes of headroom, so the next pointer "
                    "line breaches. Trim to <= %d." % (size, BUDGET, BUDGET - TARGET,
                                                       BUDGET - size, TARGET))

        # Over byte ceiling, line ceiling, or line target: name the cut line.
        over_bytes = size > BUDGET
        over_lines = nlines > LINE_LIMIT
        over_line_target = nlines > LINE_TARGET
        if over_bytes or over_lines or over_line_target:
            cut = cut_point(text)
            if not cut and over_line_target:
                cut = LINE_LIMIT + 1 if nlines > LINE_LIMIT else (LINE_TARGET + 1)
            # Which limit binds first: byte cut before line 201, else line.
            if over_bytes and (not over_lines or cut <= LINE_LIMIT):
                binder = "BYTES"
            elif over_lines or over_line_target:
                binder = "LINES"
            else:
                binder = "BYTES"
            findings.append(
                "CUT: MEMORY.md %d bytes / %d lines — %s binds first; "
                "cut_line=%d (harness drops from here; byte ceiling %d, line ceiling %d, "
                "line target %d)"
                % (size, nlines, binder, cut or LINE_LIMIT + 1, BUDGET, LINE_LIMIT,
                   LINE_TARGET))
            dropped = pointer_titles_at_or_after(text, cut or (LINE_LIMIT + 1))
            if dropped:
                findings.append("    first lost pointers:")
                findings += ["    %s" % d for d in dropped[:20]]
                if len(dropped) > 20:
                    findings.append("    … and %d more" % (len(dropped) - 20))
            if over_bytes:
                findings.append(
                    f"BUDGET: MEMORY.md is {size} BYTES, over {BUDGET} by {size - BUDGET} "
                    f"— over budget loads SILENTLY PARTIAL (its own header records 18 lost 2026-08-26)")
            if over_lines or over_line_target:
                findings.append(
                    "LINES: MEMORY.md is %d lines (harness truncates after %d; "
                    "target <= %d) — over target by %d"
                    % (nlines, LINE_LIMIT, LINE_TARGET, max(0, nlines - LINE_TARGET)))

    if warn:
        print(warn)
    if findings:
        print("memory-index-check: %d finding group(s)" % sum(
            1 for f in findings if not f.startswith("    ")))
        for f in findings:
            print(f)
        return 1
    if not quiet:
        mmsize = len((MEM / "MEMORY.md").read_bytes()) if mm.exists() else 0
        mmlines = (len((MEM / "MEMORY.md").read_text(encoding="utf-8").split("\n"))
                   if mm.exists() else 0)
        print("memory-index-check: clean — %d files, all indexed; MEMORY.md %d/%d bytes "
              "(target %d), %d/%d lines (target %d)"
              % (len(files), mmsize, BUDGET, TARGET, mmlines, LINE_LIMIT, LINE_TARGET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
