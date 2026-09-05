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

FOUR CHECKS, each for a failure that produced real loss:

  1. ORPHANS      a memory file no index points at — invisible when gbrain is down
                  and dependent on semantic luck when it is up
  2. TRUNCATION   an index line with an unclosed `(` — the line 120 defect
  3. DANGLING     a pointer to a file that no longer exists — the reverse rot
  4. BUDGET       MEMORY.md over 24985 BYTES (wc -c, NOT len(str) — ⭐/— are
                  multi-byte, so a character count reads ~600 low and says
                  "under" for a file that is over)

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
LINK = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")
# A line is suspect when it opens a link it never closes. An explicit "…" means
# the author elided on purpose, which is a different thing from a cut.
TRUNC = re.compile(r"\([A-Za-z0-9_./-]*$")


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
        out = subprocess.run(["ps", "-eo", "pid,lstart,args", "--no-headers"],
                             capture_output=True, text=True, timeout=10).stdout
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

    mm = MEM / "MEMORY.md"
    warn = None
    if mm.exists():
        size = len(mm.read_bytes())
        if BUDGET >= size > TARGET:
            warn = ("WARN: MEMORY.md is %d BYTES — under the %d ceiling but inside the last %d "
                    "bytes of it. Not broken, not fine: %d bytes of headroom, so the next pointer "
                    "line breaches. Trim to <= %d." % (size, BUDGET, BUDGET - TARGET,
                                                       BUDGET - size, TARGET))
        if size > BUDGET:
            findings.append(
                f"BUDGET: MEMORY.md is {size} BYTES, over {BUDGET} by {size - BUDGET} "
                f"— over budget loads SILENTLY PARTIAL (its own header records 18 lost 2026-08-26)")

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
        print("memory-index-check: clean — %d files, all indexed; MEMORY.md %d/%d bytes "
              "(target %d)" % (len(files), mmsize, BUDGET, TARGET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
