"""`swarph cell selfcheck` — does this cell agree with itself about where its state lives?

Board card #133. READ-ONLY. No writes, no network, no mutation of any kind.

WHY THIS EXISTS AND WHY IT MUST RUN ON THE CELL
-----------------------------------------------
The state-dir literal has ESCAPED the codebase. Beyond the in-package call sites it
lives in each cell's systemd units, crontab lines and scripts — surfaces CI is
STRUCTURALLY BLIND TO. Only a check running ON a cell can read that cell's own units
and crons; another cell cannot, CI cannot, the commander cannot.

    RESTART FROM ABOVE, READ FROM WITHIN.
Supervision must live ABOVE the thing supervised (you cannot restart yourself, which
is why the monitor is a system unit that survives tmux dying). INSPECTION must live
INSIDE. Those two rules look opposed and are not.

SCOPE — deliberately narrow (gpt-ops): "a narrow read-only baseline gate with the
five known fixtures. Do not let it become an open-ended platform before it can
produce pre/post evidence." Its job is to produce a PRE-MIGRATION BASELINE for the
#132/#130 fleet migration, so a post-migration diff separates MIGRATION BREAKAGE from
PRE-EXISTING ROT. Anything present in both runs was already there; anything new is
ours. Resist adding checks until that has been done once.

WHAT IT REPORTS — the product is telling a CHOICE apart from ROT
----------------------------------------------------------------
  DRIFT           one key, two live values, nothing declared     -> exit 1
  DECLARED        the same divergence, declared in cell_expected -> exit 0
  RELATION BROKEN --cursor does not live under --state-dir       -> exit 1
  UNOWNED         a line on a shared surface no cell claims      -> exit 1
  MALFORMED       a flag with an empty/missing value             -> exit 1 if live
  FOSSIL          a surface that is inactive AND disabled        -> reported, never drift
  OTHER CELL      another cell's line on a shared surface        -> attributed, not checked

DESIGN NOTE, load-bearing: `live` is a FIELD on the surface dict, never a syscall
inside the verdict code. Liveness must be INJECTABLE or the FOSSIL shape is testable
only on the one box that has a dead unit. Discovery shells out; verdicts are pure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Keys worth comparing. Deliberately small — see SCOPE.
_KEYS = ("state-dir", "cursor", "gateway", "token-file", "as", "cell")

# A cursor path that is a liveness MARKER, not a DM cursor. droplet and
# drop-on-meta-edge both pass one to --cursor, and a naive relation check
# reported both as broken (2 false positives, caught by science-claude).
_MARKER_RE = re.compile(r"-claude-active\.txt$|/[\w.-]*-active\.txt$")
_DM_CURSOR_RE = re.compile(r"cursor\.json$")

# systemd SPECIFIERS are not values. A template unit's ExecStart carries `--as %i`,
# which a naive owner-parse reports as a cell literally named "%i". Found by running
# this on lab-ovh, which has a template unit; droplet's five fixtures could not cover
# it because his box has none. SIXTH SHAPE.
# Matches a placeholder ANYWHERE in the value: `%i`, `<PEER>`, `${HOME}` — and
# crucially also `<HOME>/state`, a PARTIALLY substituted path. A half-templated
# value is still unsubstituted template text; treating it as configuration invents
# a path no process will ever open.
_SPECIFIER_RE = re.compile(r"%[a-zA-Z]\b|<[A-Z_]+>|\$\{?[A-Z_]\w*\}?")


def is_placeholder(value: str) -> bool:
    """True for systemd specifiers (%i), shell vars ($HOME) and template slots (<PEER>).

    These are UNSUBSTITUTED TEMPLATE TEXT, not configuration. Treating one as an owner
    invents a cell; treating one as a value invents drift.
    """
    return bool(_SPECIFIER_RE.search(value.strip()))


@dataclass(frozen=True)
class Row:
    """One flag observed on one surface. `live` rides the surface, not a syscall."""
    key: str
    value: str
    owner: Optional[str]
    live: bool
    surface: str = ""


def cursor_type(path: str) -> str:
    """marker | dm-cursor | unknown.

    `unknown` is REPORTED, never guessed. Guessing here manufactures drift, which
    is worse than admitting the tool does not recognise a path.
    """
    if _MARKER_RE.search(path):
        return "marker"
    if _DM_CURSOR_RE.search(path):
        return "dm-cursor"
    return "unknown"


def relation_broken(cursor: str, state_dir: str) -> bool:
    """True when a DM cursor does not live under the declared state dir.

    Only meaningful for a dm-cursor; a marker is not state and has no relation.
    """
    if cursor_type(cursor) != "dm-cursor":
        return False
    return not cursor.rstrip("/").startswith(state_dir.rstrip("/") + "/")


def extract(surface: dict) -> list[Row]:
    """Parse `--key value` pairs out of one surface's text.

    Handles the two shapes that broke earlier versions:
      · `--cursor =`  -> ("cursor", "<EMPTY>")  NOT the next flag. A naive regex
        reports cursor="--cell" WITH CONFIDENCE, which is worse than no answer.
      · a SHARED surface (one crontab, six cells) -> ownership resolved PER LINE
        from `--cell`/`--as`, never per file.
    """
    rows: list[Row] = []
    live = bool(surface.get("live", True))
    name = surface.get("name", "?")
    for line in surface.get("text", "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            toks = shlex.split(line, comments=False)
        except ValueError:
            toks = line.split()
        owner = None
        pairs: list[tuple[str, str]] = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok.startswith("--"):
                key = tok[2:].split("=", 1)[0]
                if "=" in tok:
                    val = tok.split("=", 1)[1]
                else:
                    nxt = toks[i + 1] if i + 1 < len(toks) else None
                    # `--cursor =` and `--cursor --cell` are both MISSING VALUES.
                    val = None if (nxt is None or nxt == "=" or nxt.startswith("--")) else nxt
                    if val is not None:
                        i += 1
                    elif nxt == "=":
                        i += 1
                if key in ("cell", "as") and val and not is_placeholder(val):
                    owner = val
                if key in _KEYS and not (val and is_placeholder(val)):
                    pairs.append((key, val if val else "<EMPTY>"))
            i += 1
        for key, val in pairs:
            rows.append(Row(key=key, value=val, owner=owner, live=live, surface=name))
    return rows


def discover_surfaces() -> list[dict]:
    """Read this cell's units and crontab. THE ONLY IMPURE FUNCTION HERE.

    Shells to `systemctl`/`crontab -l` with capture-only, timeouts, and no writes.
    Tests replace this wholesale — every verdict test runs from string fixtures so
    it passes on any box, including boxes with no systemd at all.
    """
    surfaces: list[dict] = []
    unit_dirs = [Path("/etc/systemd/system"), Path.home() / ".config/systemd/user"]
    for d in unit_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*swarph*.service")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            surfaces.append({"name": f.name, "kind": "unit", "text": text,
                             "live": _unit_live(f.name, user=("user" in str(d)))})
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=10).stdout
        if cron.strip():
            surfaces.append({"name": "(crontab)", "kind": "cron", "text": cron,
                             "live": True, "shared": True})
    except (OSError, subprocess.SubprocessError):
        pass
    return surfaces


def _unit_live(unit: str, *, user: bool) -> bool:
    """A unit is live unless it is BOTH inactive AND disabled — that is a FOSSIL."""
    scope = ["--user"] if user else []
    def _q(verb: str) -> str:
        try:
            return subprocess.run(["systemctl", *scope, verb, unit],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    return not (_q("is-active") != "active" and _q("is-enabled") in ("disabled", "masked"))


def run_selfcheck(*, self_name: str, declaration: Path) -> int:
    """Pure verdict pass over discovered surfaces. Returns 0 (clean) or 1 (drift)."""
    declared: dict = {}
    if declaration.exists():
        try:
            declared = json.loads(declaration.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  WARN      declaration unreadable ({exc}) — treating as absent")

    rows = [r for s in discover_surfaces() for r in extract(s)]
    mine = [r for r in rows if r.owner in (None, self_name)]
    drift = False

    print(f"cell selfcheck: {self_name}   ({len(rows)} flags across surfaces)")

    for r in rows:
        if r.owner and r.owner != self_name:
            print(f"  OTHER CELL[{r.owner}] --{r.key} {r.value}   ({r.surface})")

    for r in rows:
        if not r.live:
            tag = "MALFORMED" if r.value == "<EMPTY>" else "FOSSIL"
            print(f"  {tag:9s} --{r.key} {r.value}   ({r.surface}, inactive+disabled)")

    for r in mine:
        if r.live and r.value == "<EMPTY>":
            print(f"  MALFORMED --{r.key} has no value   ({r.surface})")
            drift = True

    # shared surfaces: a line nobody claims rots unnoticed
    for r in rows:
        if r.owner is None and r.live and r.key == "cursor" and r.surface == "(crontab)":
            print(f"  UNOWNED   --{r.key} {r.value}   ({r.surface}) — no --cell/--as on this line")
            drift = True

    by_key: dict[str, set[str]] = {}
    for r in mine:
        if r.live and r.value != "<EMPTY>":
            by_key.setdefault(r.key, set()).add(r.value)

    for key, vals in sorted(by_key.items()):
        if len(vals) == 1:
            print(f"  OK        --{key} {next(iter(vals))}")
            continue
        allowed = declared.get(key)
        if allowed and set(vals) <= set(allowed if isinstance(allowed, list) else [allowed]):
            print(f"  DECLARED  --{key} {sorted(vals)} — divergence is a stated choice")
        else:
            print(f"  DRIFT     --{key} {sorted(vals)} — {len(vals)} values, none declared")
            drift = True

    cur = sorted(by_key.get("cursor", []))
    sd = sorted(by_key.get("state-dir", []))
    if len(cur) == 1 and len(sd) == 1 and relation_broken(cur[0], sd[0]):
        print(f"  RELATION BROKEN  --cursor {cur[0]} is not under --state-dir {sd[0]}")
        print("                   (both read OK per-key — the relation is the finding)")
        drift = True

    print(f"\n  verdict: {'DRIFT' if drift else 'consistent'}")
    return 1 if drift else 0


def run_cell_selfcheck(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="swarph cell selfcheck", description=__doc__.split("\n")[0])
    p.add_argument("--as", dest="self_name", default=os.environ.get("SWARPH_SELF"),
                   help="this cell's peer name (default: $SWARPH_SELF)")
    p.add_argument("--declaration", default=None,
                   help="path to cell_expected.json declaring intentional divergence")
    args = p.parse_args(argv)
    if not args.self_name:
        print("swarph cell selfcheck: pass --as <peer> or set $SWARPH_SELF", file=sys.stderr)
        return 2
    decl = Path(args.declaration).expanduser() if args.declaration else (
        Path.home() / ".config" / "swarph" / "cell_expected.json")
    return run_selfcheck(self_name=args.self_name, declaration=decl)
