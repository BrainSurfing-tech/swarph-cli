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
#
# SEVENTH SHAPE (cursor-lin, 2026-08-17): an instance drop-in is where the live
# ExecStart actually lives. `swarph-monitor@.service` is the FILE; the VALUES are
# in `swarph-monitor@<peer>.service.d/override.conf`. A glob that only matches
# `*.service` certifies the template (placeholders, skipped) and never opens the
# drop-in — `consistent` with zero owned flags. Same lie as grok's unread crontab.
# Matches a placeholder ANYWHERE in the value: `%i`, `<PEER>`, `${HOME}` — and
# crucially also `<HOME>/state`, a PARTIALLY substituted path. A half-templated
# value is still unsubstituted template text; treating it as configuration invents
# a path no process will ever open.
#
# THE SHELL-VAR BRANCH IS DELIBERATELY CASE-INSENSITIVE AND ACCEPTS DIGITS.
# An earlier form required an uppercase first character, so `$state`, `${state_dir}`
# and `$1` read as LITERAL VALUES and `--state-dir "$statedir"` reported DRIFT ON
# TEMPLATE TEXT (droplet, PR #148 review). Shell variables are not conventionally
# uppercase — only environment variables are, and a script's locals are neither.
_SPECIFIER_RE = re.compile(r"%[a-zA-Z]\b|<[A-Za-z_]+>|\$\{?\w+\}?")


# /etc/cron.d filenames cron will actually run (Debian/Ubuntu run-parts). Any dot
# disqualifies the file — see the note at the system-cron probe.
_CRON_D_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_placeholder(value: str) -> bool:
    """True for systemd specifiers (%i), shell vars ($HOME) and template slots (<PEER>).

    These are UNSUBSTITUTED TEMPLATE TEXT, not configuration. Treating one as an owner
    invents a cell; treating one as a value invents drift.
    """
    return bool(_SPECIFIER_RE.search(value.strip()))


@dataclass(frozen=True)
class Row:
    """One flag observed on one surface. `live` rides the surface, not a syscall.

    `shared` likewise rides the surface as a PROPERTY. It must never be re-derived
    from `surface`, which is a human-readable LABEL: gating a check on the string
    "(crontab)" means renaming that label for readability — the kind of change
    nobody reviews closely — silently disables the check and the run stays green.
    """
    key: str
    value: str
    owner: Optional[str]
    live: bool
    surface: str = ""
    shared: bool = False


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
    shared = bool(surface.get("shared", False))
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
            rows.append(Row(key=key, value=val, owner=owner, live=live,
                            surface=name, shared=shared))
    return rows


_OTHER_USER_CAVEAT = " (cannot see other users' crontabs in any case)"


def user_crontab_surfaces(returncode: int, stdout: str, stderr: str) -> list[dict]:
    """Classify a `crontab -l` result. PURE — takes the outcome, does not run it.

    Split out so every branch is testable on any box, including Windows where there
    is no crontab at all. Calling the impure discoverer from a test would make these
    cases unreachable on half the CI matrix, which is the same "only passes on one
    box" trap the whole suite is built to avoid.
    """
    if returncode == 0 and stdout.strip():
        return [{"name": "(crontab)", "kind": "cron", "text": stdout,
                 "live": True, "shared": True},
                {"kind": "coverage", "class": "user crontab", "read": True,
                 "detail": f"{len(stdout.splitlines())} lines" + _OTHER_USER_CAVEAT}]
    if returncode == 0:
        return [{"kind": "coverage", "class": "user crontab", "read": True,
                 "detail": "present but empty" + _OTHER_USER_CAVEAT}]
    if "no crontab for" in (stderr or ""):
        # LEGITIMATE EMPTY: a real answer, not a failure. Still stated, because a cron
        # line for this cell may live under another user. That is grok's situation.
        #
        # The stderr match is LOCALE-DEPENDENT ON PURPOSE: a translated message falls
        # through to the failure branch and becomes read=False -> DID NOT MEASURE.
        # That degrades BLIND rather than FALSELY-CLEAN, which is the safe direction.
        # Do not "fix" it into a broader match — a wider pattern would swallow real
        # failures (droplet, PR #150).
        return [{"kind": "coverage", "class": "user crontab", "read": True,
                 "detail": "no crontab for this user" + _OTHER_USER_CAVEAT}]
    return [{"kind": "coverage", "class": "user crontab", "read": False,
             "detail": (stderr or f"exit {returncode}").strip()[:120]}]


def system_cron_coverage(live: list, inert: list, unreadable: list, *,
                         platform_has_cron: bool) -> dict:
    """Classify the system-cron probe. PURE — takes the outcome, does not produce it.

    ABSENT IS NOT NOT-APPLICABLE, AND THIS CLASS FAILED BY AN EMPTY LOOP RATHER THAN
    BY AN EXCEPTION, so the FileNotFoundError fix for `user crontab` could not reach
    it (droplet, PR #150 follow-up — READ from the branch, not measured, because his
    box has cron; confirmed here by test). A Windows cell was returning

        COVERAGE  user crontab  read  not applicable — no cron on this platform
        COVERAGE  system cron   read  none present

    describing ONE platform fact two different ways, and "none present" — which reads
    as I LOOKED AND THERE WERE NONE — is the one a reader would trust. Same
    absent-vs-not-applicable distinction, one class over.
    """
    if unreadable:
        return {"kind": "coverage", "class": "system cron", "read": False,
                "detail": "unreadable: " + ", ".join(unreadable)[:100]}
    if not platform_has_cron:
        return {"kind": "coverage", "class": "system cron", "read": True,
                "detail": "not applicable — no cron on this platform"}
    detail = f"{len(live)} live" if live else "none live"
    if inert:
        detail += (f", {len(inert)} ignored (dot-named; cron's run-parts skips them "
                   f"on Debian/Ubuntu)")
    return {"kind": "coverage", "class": "system cron", "read": True,
            "detail": detail if (live or inert) else "present but empty"}


def dropin_parent_unit(relpath: str) -> Optional[str]:
    """Instance-or-plain unit name for a drop-in path, or None if this is not one.

    `swarph-monitor@cursor-lin.service.d/override.conf` ->
    `swarph-monitor@cursor-lin.service`. The parent is what `systemctl is-active`
    names; the drop-in file itself is not a unit.
    """
    posix = relpath.replace("\\", "/")
    parent = posix.rsplit("/", 1)[0] if "/" in posix else posix
    if parent.endswith(".service.d"):
        return parent[: -len(".d")]
    return None


def list_swarph_unit_files(unit_dir: Path) -> list[Path]:
    """Unit files AND drop-ins. Drop-ins are where instance ExecStart lives.

    MEASURED 2026-08-17 on cursor-lin: the live `--as` / `--token-file` /
    `--gateway` values exist only in
    `swarph-monitor@cursor-lin.service.d/override.conf`. `*swarph*.service`
    never opened that directory.
    """
    units = sorted(unit_dir.glob("*swarph*.service"))
    dropins = sorted(unit_dir.glob("*swarph*.service.d/*.conf"))
    return units + dropins


def discover_surfaces() -> list[dict]:
    """Read this cell's units and crontab. THE ONLY IMPURE FUNCTION HERE.

    Shells to `systemctl`/`crontab -l` with capture-only, timeouts, and no writes.
    Tests replace this wholesale — every verdict test runs from string fixtures so
    it passes on any box, including boxes with no systemd at all.
    """
    surfaces: list[dict] = []
    unit_dirs = [Path("/etc/systemd/system"), Path.home() / ".config/systemd/user"]
    n_units = 0
    n_dropins = 0
    for d in unit_dirs:
        if not d.is_dir():
            continue
        for f in list_swarph_unit_files(d):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = f.relative_to(d).as_posix()
            parent = dropin_parent_unit(rel)
            if parent:
                n_dropins += 1
                label = rel
                live_name = parent
            else:
                n_units += 1
                label = f.name
                live_name = f.name
            surfaces.append({"name": label, "kind": "unit", "text": text,
                             "live": _unit_live(live_name, user=("user" in str(d)))})
    detail = ", ".join(str(d) for d in unit_dirs if d.is_dir()) or "none present"
    detail += f"; {n_units} units, {n_dropins} drop-ins"
    surfaces.append({"kind": "coverage", "class": "systemd units",
                     "read": True,
                     "detail": detail})

    # >>> THE CRONTAB PROBE HAD THREE SILENT FAILURE PATHS THAT ALL RENDERED AS
    #     "no crontab surface", AND THE VERDICT PROCEEDED AS IF IT HAD LOOKED. <<<
    # MEASURED 2026-07-27: grok-researcher's baseline came back `consistent` with 8
    # flags and ZERO crontab rows, while four other cells ON THE SAME BOX saw 18.
    # grok is the cell with the KNOWN relation-broken drift (shape 1) — and that
    # drift lives in the crontab it could not read. THE TOOL CERTIFIED CLEAN THE ONE
    # CELL IT WAS DESIGNED AROUND, because rc and stderr were both discarded:
    #   · exception (no crontab binary)  -> except: pass
    #   · rc=1 "no crontab for <user>"   -> empty stdout, no surface
    #   · genuinely empty crontab        -> no surface
    # `crontab -l` exits 1 with "no crontab for X" on STDERR, which the old code
    # never read. Coverage is now REPORTED as data, so a clean verdict can never be
    # read as "everything was inspected".
    # THE CLASS IS `user crontab`, NOT `crontab`. `crontab -l` reads THE INVOKING
    # USER'S crontab and nothing else. Naming the class `crontab` overstated it BY
    # THE NAME ALONE, before any logic ran: a cell with a swarph line in /etc/cron.d
    # would read "COVERAGE crontab read / verdict: consistent" while a live cron
    # surface was never opened — grok's failure moved one directory over
    # (droplet, PR #150 review). One name, two facts, inside the field added to fix
    # one name, two facts.
    #
    # THE OTHER-USER CAVEAT IS UNCONDITIONAL, and that is deliberate. It is a
    # property of THE PROBE — this command structurally cannot read another user's
    # crontab in ANY branch — not a property of the cell. Hanging it only off the
    # "no crontab" branch would tell grok and stay silent for a cell that HAS a
    # crontab and ALSO has a line under another user.
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        surfaces += user_crontab_surfaces(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        # NOT APPLICABLE — a THIRD state, distinct from read and from failed-to-read.
        # Windows CI found this: no crontab binary exists, so the probe raised, the
        # class went read=False, and the run returned DID NOT MEASURE. But "this
        # platform has no cron" is not "I could not read the cron" — the surface does
        # not exist to be read. Collapsing them makes every Windows cell permanently
        # unmeasurable, which trains people to ignore exit 2 on the platform where it
        # would eventually mean something real.
        surfaces.append({"kind": "coverage", "class": "user crontab", "read": True,
                         "detail": "not applicable — no cron on this platform"})
    except (OSError, subprocess.SubprocessError) as exc:
        # A PermissionError or a timeout is genuinely blind: we meant to read it and
        # could not. That must still be DID NOT MEASURE.
        surfaces.append({"kind": "coverage", "class": "user crontab", "read": False,
                         "detail": f"{type(exc).__name__}: {exc}"[:120]})

    # SYSTEM CRON — /etc/crontab and /etc/cron.d/*, which carry a sixth USER field.
    # Never enumerated before, so they were invisible AND unmentioned: the tool could
    # not say they were clean and could not say it had not looked.
    sys_cron, inert, unreadable = [], [], []
    for path in [Path("/etc/crontab")] + sorted(Path("/etc/cron.d").glob("*")
                                                if Path("/etc/cron.d").is_dir() else []):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append(f"{path.name} ({exc.__class__.__name__})")
            continue
        # CRON DOES NOT EXECUTE EVERY FILE IN /etc/cron.d. run-parts only runs names
        # matching ^[A-Za-z0-9_-]+$ — ANY DOT DISQUALIFIES THE FILE. So `.placeholder`,
        # `swarph.conf`, `foo.bak` and `x.dpkg-dist` are INERT. Reading them as live
        # configuration would report DRIFT / UNOWNED / RELATION BROKEN for a line CRON
        # WILL NEVER RUN (droplet, PR #150 — found because the file COUNT could not be
        # reconciled by hand, which is why the count is printed).
        #
        # This is the FOSSIL distinction the systemd path already models
        # (inactive+disabled -> reported, never drift). Cron expresses the same
        # live/dead split as a FILENAME RULE instead of unit state, and it must be
        # applied or one surface class silently lacks the category.
        #
        # ASSUMPTION STATED, NOT ASSERTED (droplet's own caveat on his finding): the
        # rule is Debian/Ubuntu run-parts behaviour, verified against a real file list,
        # NOT read out of this box's cron source. Vixie-cron variants differ. It is
        # named in the coverage line so a reader on another platform can distrust it.
        executable = path.name == "crontab" or _CRON_D_NAME_RE.match(path.name)
        (sys_cron if executable else inert).append(path.name)
        if "swarph" in text:
            # System cron lines carry a USER column that user crontabs do not;
            # ownership is still by --cell/--as, and the extra field is inert to the
            # flag parser.
            surfaces.append({"name": f"({path})", "kind": "cron", "text": text,
                             "live": bool(executable), "shared": True})
    surfaces.append(system_cron_coverage(
        sys_cron, inert, unreadable,
        platform_has_cron=Path("/etc/crontab").exists() or Path("/etc/cron.d").is_dir()))

    # NOT INSPECTED, stated so a clean verdict cannot be misread (science-claude,
    # 2026-07-27): her live monitor is a HAND-STARTED process, not a unit and not a
    # cron line, so THE ONE THING ACTUALLY DELIVERING HER DMs IS INVISIBLE HERE. This
    # tool reads DECLARED CONFIGURATION, never running state. A migration that
    # installs a unit beside a hand-started daemon gets two monitors, and this tool
    # cannot warn about the one it did not install.
    surfaces.append({"kind": "coverage", "class": "running processes", "read": False,
                     "detail": "NOT INSPECTED BY DESIGN — this tool reads declared "
                               "config, not running state; a hand-started daemon is "
                               "invisible to it"})
    return surfaces


def _unit_live(unit: str, *, user: bool) -> bool:
    """A unit is live unless it is BOTH inactive AND disabled — that is a FOSSIL."""
    scope = ["--user"] if user else []
    def _q(verb: str) -> str:
        try:
            return subprocess.run(["systemctl", *scope, verb, unit],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    return not (_q("is-active") != "active" and _q("is-enabled") in ("disabled", "masked"))


def run_selfcheck(*, self_name: str, declaration: Path) -> int:
    """Pure verdict pass over discovered surfaces. 0 clean, 1 drift, 2 DID NOT MEASURE."""
    # THE PER-CELL RENAME IN #148 IS A SILENT MIGRATION, AND IT LANDS ON THE BASELINE.
    # MEASURED on droplet: with the old shared cell_expected.json present and the
    # per-cell file absent, the run reported DRIFT on three keys and NEVER MENTIONED
    # the file sitting right there; renaming it flipped the same box to consistent
    # with no surface changed. Every cell that declared anything before #148 would
    # produce a baseline full of FALSE DRIFT — and that baseline is the "before"
    # picture #132/#130 gets diffed against.
    #
    # A POISONED BASELINE IS WORSE THAN NO BASELINE, BECAUSE IT IS DIFFED AGAINST
    # WITH CONFIDENCE. An operator reading it would either re-declare from whatever
    # the tool printed (turning the declaration into decoration) or EDIT SURFACES TO
    # MATCH a tool that cannot see their existing declaration.
    #
    # Deliberately NOT auto-reading the legacy file: that re-introduces the shared
    # one-file-many-cells defect #148 removed. Exit 2, not 1 — this run did not
    # measure drift, it measured a MISSING DECLARATION. Empty and blind must not
    # render identically. (droplet, PR #149 review.)
    if not declaration.exists():
        legacy = legacy_declaration_path(declaration)
        if legacy is not None and legacy.exists():
            print(f"  DECLARATION NOT READ — found legacy shared {legacy}")
            print(f"                         per-cell file expected at {declaration}")
            print("                         NOT read: one declaration per cell since #148.")
            print("                         Move or split it, then re-run.")
            print("\n  verdict: DID NOT MEASURE (legacy declaration present, per-cell absent)")
            return 2

    declared: dict = {}
    if declaration.exists():
        try:
            declared = json.loads(declaration.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  WARN      declaration unreadable ({exc}) — treating as absent")

    surfaces = discover_surfaces()
    coverage = [s for s in surfaces if s.get("kind") == "coverage"]
    rows = [r for s in surfaces if s.get("kind") != "coverage" for r in extract(s)]
    mine = [r for r in rows if r.owner in (None, self_name)]
    drift = False

    print(f"cell selfcheck: {self_name}   ({len(rows)} flags across surfaces)")

    # COVERAGE FIRST, ALWAYS. A verdict is only as wide as what was inspected, and a
    # baseline that does not state its own coverage invites "consistent" to be read
    # as "everything is fine". grok-researcher's clean verdict over an unread crontab
    # is the measured instance.
    for c in coverage:
        mark = "read" if c.get("read") else "NOT READ"
        print(f"  COVERAGE  {c.get('class'):18s} {mark:9s} {c.get('detail','')}")
    blind = [c for c in coverage if not c.get("read") and c.get("class") != "running processes"]

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

    # Shared surfaces: a line nobody claims rots unnoticed.
    #
    # Gated on the surface's `shared` PROPERTY, and on NO key allowlist. An earlier
    # form required `r.key == "cursor" and r.surface == "(crontab)"`, which was wrong
    # twice over (droplet, PR #148 review): the label as a control predicate, and
    # enumerating which keys can rot instead of stating the property — the same
    # unenumerable-denylist move this mesh rejected in #128b option (b) one day
    # earlier, reproduced inside the tool built while rejecting it.
    for r in rows:
        if r.shared and r.owner is None and r.live:
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

    # RELATIONS ARE FACTS, AND FACTS ARE NOT PER-OWNER. The asymmetry (droplet, whose
    # retired prototype had this and whose own ten fixtures never pinned it, so a
    # reimplementation dropped it and passed both CI and his review):
    #
    #   DECLARATION-DEPENDENT — DRIFT / DECLARED — is correctly PER-OWNER. You cannot
    #     judge another cell's divergence; it may be intentional and declared in THEIR
    #     file, which you cannot read.
    #   FACTUAL INCONSISTENCY — RELATION BROKEN — is NOT declaration-dependent.
    #     `--cursor` not under `--state-dir` is wrong no matter whose line it is, and
    #     NO DECLARATION CAN MAKE IT RIGHT.
    #
    # Attributing another cell's line and then checking NOTHING about it conflates the
    # two. "attributed, not checked" should be "attributed, not JUDGED — still checked
    # for facts." On a shared box this is exactly how a neighbour's live divergence
    # becomes visible to someone who can act on it.
    #
    # Another cell's relation break is REPORTED but does NOT set drift: it is not this
    # cell's verdict to fail on. It is their fact and this cell's observation.
    by_owner: dict[str, dict[str, set]] = {}
    for r in rows:
        if r.live and r.value != "<EMPTY>" and r.key in ("cursor", "state-dir"):
            by_owner.setdefault(r.owner or self_name, {}).setdefault(r.key, set()).add(r.value)

    for owner in sorted(by_owner):
        cur = sorted(by_owner[owner].get("cursor", []))
        sd = sorted(by_owner[owner].get("state-dir", []))
        if len(cur) == 1 and len(sd) == 1 and relation_broken(cur[0], sd[0]):
            tag = "RELATION BROKEN" if owner == self_name else f"RELATION BROKEN[{owner}]"
            print(f"  {tag}  --cursor {cur[0]} is not under --state-dir {sd[0]}")
            print("                   (both read OK per-key — the relation is the finding)")
            if owner == self_name:
                drift = True

    if blind:
        # A surface class we MEANT to read and could not. Exit 2, not 0 and not 1:
        # this run did not measure the cell, it measured part of it. Reporting
        # `consistent` over an unread surface is precisely the lie this card exists
        # to catch — and the tool committed it against grok-researcher.
        names = ", ".join(str(c.get("class")) for c in blind)
        print(f"\n  verdict: DID NOT MEASURE (could not read: {names})")
        return 2

    print(f"\n  verdict: {'DRIFT' if drift else 'consistent'}")
    return 1 if drift else 0


def default_declaration_path(self_name: str) -> Path:
    """PER-CELL by construction: ~/.config/swarph/cell_expected.<cell>.json

    A single shared `cell_expected.json` is SHAPE 3 — one file, many cells — inside
    the shape-3 detector (droplet, PR #148 review). On lab-ovh's shared home five
    cells resolve the same HOME, so two cells declaring different intentional
    --state-dir values would silence each other, and the declaration is precisely
    what separates a deliberate CHOICE from ROT.

    This must be per-cell BEFORE fleet baselines are captured: a baseline taken
    against a shared declaration records the wrong thing and is unrecoverable —
    you cannot reconstruct afterwards whose intent was in the file.
    """
    return Path.home() / ".config" / "swarph" / f"cell_expected.{self_name}.json"


# The pre-#148 shared name. Cells that declared anything before the per-cell rename
# still have this file on disk.
_LEGACY_DECLARATION_NAME = "cell_expected.json"


def legacy_declaration_path(declaration: Path) -> Optional[Path]:
    """The old shared name beside the expected per-cell one, or None if identical."""
    legacy = declaration.parent / _LEGACY_DECLARATION_NAME
    return None if legacy == declaration else legacy


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
    decl = (Path(args.declaration).expanduser() if args.declaration
            else default_declaration_path(args.self_name))
    return run_selfcheck(self_name=args.self_name, declaration=decl)


# RUNNABLE AS A BARE FILE, ON PURPOSE — do not remove.
#
#     python3 src/swarph_cli/commands/cell_selfcheck.py --as <peer>
#
# The baseline this produces is needed on cells whose swarph install may itself be
# part of what is broken, and on cells not yet carrying an unreleased version. Both
# normal entry points are unavailable there, MEASURED in an empty venv:
#   python -m swarph_cli.main ...                     -> ModuleNotFoundError: swarph_mesh
#   from swarph_cli.commands.cell_selfcheck import .. -> same; the package __init__
#                                                        eagerly imports parsers
# The module's own imports are stdlib-only, so the file runs standalone even though
# the package around it cannot be imported. REQUIRING A WORKING INSTALL TO DIAGNOSE
# A BROKEN INSTALL IS THE SAME CIRCULARITY AS SUPERVISING A THING FROM INSIDE IT.
# tests/test_cell_selfcheck.py pins this: stdlib-only imports, and an isolated-mode
# subprocess run that fails if anything non-stdlib creeps in.
if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(run_cell_selfcheck(sys.argv[1:]))
