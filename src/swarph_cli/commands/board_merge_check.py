"""``swarph board merge-check`` — the board decides whether a PR may merge.

Board card #137, commander-directed: "PRs need to go through the Board auto merge&Push
process (from swairm)." Shape chosen by the commander: BOARD-DRIVEN MERGE, PUBLISH
STAYS COMMANDER-GATED.

>>> PHASE 1 IS DRY-RUN. IT DECIDES AND LOGS. IT MERGES NOTHING. <<<
The graduation trigger is a DATE, not a data-condition, and it is written on the card
before a line of this ran — because a data-conditioned trigger never fires: the data
can always be argued not-yet-conclusive by whoever would have to do the work. That is
how the #1 reaper has been "pending" in dry-run since 2026-07-21.

WHY THE BOARD AND NOT GITHUB — this is not a workaround, it is the reason the feature
belongs here at all. MEASURED 2026-07-27:

    gh pr review --approve  ->  "Can not approve your own pull request"

Every mesh cell pushes under the SAME GitHub account, so GitHub structurally cannot
tell author from reviewer. A branch-protection rule requiring approvals would be
UNSATISFIABLE, not merely unsatisfied — it would deadlock every PR. So the peer verdict
lives on the CARD, and the card is the authority this reads.

SWAIRM PARITY, and where we deliberately diverge: adopted — the Overmind pattern, ONE
credential holder playing state transitions, no durable local state so a crash cannot
double-merge. DIVERGED — swairm keeps MERGE HUMAN (the Reine's verdict is advisory).
We automate it. State that plainly: the safety swairm buys with a human is bought here
by PRECONDITIONS THAT CANNOT BE RECALLED WRONG.

DESIGN NOTE, the load-bearing one: `decide()` is PURE. It takes a card dict and a
pr_state dict and returns a Decision. Every precondition is therefore testable with no
board, no GitHub, no network — the same rule that made `cell selfcheck`'s five defect
shapes reproducible off the box that had them.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

# >>> READINESS IS THE `move_ready` FLAG AND A REAL STAGE — NOT AN INVENTED ONE. <<<
# The first version gated on stage == "peer-green". MEASURED AFTER WRITING IT:
#     swarph board cards move 141 peer-green  ->  gateway 400: unknown stage 'peer-green'
# NO CARD CAN EVER BE IN THAT STAGE. The tool would have refused every card forever, and
# all 19 tests passed because THE FIXTURES CONSTRUCTED A CARD THE REAL SYSTEM CANNOT
# PRODUCE. Third instance of one defect in this file: `gh pr checks --json` (does not
# exist), a passing check printing a failure message, and now a stage that does not
# exist — each a check that can never succeed, which reads as "strict" not "broken".
#
# `move_ready` is the mechanism that already exists for exactly this: #70 built it as
# an explicit ball-in-court handoff, a NON-orchestrator saying "this should advance".
# NOTE THE DEPENDENCY THIS CREATES: card #141 — move_ready is unreachable on UNASSIGNED
# cards — now blocks the merge process too, because a PR card nobody has claimed cannot
# be flagged ready by the peer who reviewed it.
READY_STAGES = ("build", "test")

# Paths whose modification makes a PR PERMANENTLY HUMAN-MERGE-ONLY.
# A process that can ship changes to its own gate has no gate.
_SELF_PATHS = (
    "src/swarph_cli/commands/board_merge_check.py",
    "tests/test_board_merge_check.py",
)

# "<cell> APPROVED <sha>" — the verdict names a COMMIT, never a branch.
_VERDICT_RE = re.compile(r"^\s*(?P<cell>[\w.-]+)\s+(?P<verdict>[A-Z]+)\s+(?P<sha>[0-9a-f]{7,40})\s*$")


@dataclass(frozen=True)
class Check:
    """One precondition. `ok=None` means COULD NOT EVALUATE — never treated as pass."""
    name: str
    ok: Optional[bool]
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    card_id: str
    # >>> THREE VERDICTS, NOT TWO (droplet, PR #154 review — my own lesson, one level
    # in). A two-valued REFUSE collapses "this PR FAILS a precondition" and "I COULD NOT
    # EVALUATE one", and those need opposite responses: the first says FIX YOUR PR, the
    # second says THE TOOL IS BLIND — go look at it. Same split shipped in `cell
    # selfcheck` (read/not-read/not-applicable) and GET /highlights (empty/behind/blind),
    # and omitted here in the module written to enforce discipline. <<<
    verdict: str                      # WOULD_MERGE | REFUSE | CANNOT_EVALUATE
    checks: list = field(default_factory=list)

    @property
    def blockers(self) -> list:
        return [c for c in self.checks if c.ok is not True]


def parse_verdict(text: str) -> Optional[dict]:
    """Parse a `peer_verdict` card link: "<cell> APPROVED <sha>".

    Returns None on anything unrecognised — REPORTED as a failed precondition, never
    guessed. A guessed verdict is an unreviewed merge wearing a review's name.
    """
    m = _VERDICT_RE.match(text or "")
    if not m:
        return None
    return {"cell": m.group("cell"), "verdict": m.group("verdict"), "sha": m.group("sha")}


def decide(card: dict, pr_state: dict) -> Decision:
    """PURE. Card + PR state -> Decision. No I/O, no clock, no network."""
    links = card.get("links") or {}
    checks: list = []

    def add(name, ok, detail=""):
        checks.append(Check(name, ok, detail))

    add("stage is one the board accepts for execution", card.get("stage") in READY_STAGES,
        f"stage={card.get('stage')!r} (expected one of {', '.join(READY_STAGES)})")
    add("card flagged move_ready", bool(card.get("move_ready")),
        "" if card.get("move_ready") else "the reviewer has not signalled ready-to-advance")

    verdict = parse_verdict(links.get("peer_verdict", ""))
    # Detail only when it FAILED. A passing check that prints a failure message is a
    # verdict line contradicting the data beside it — the exact thing this mesh spent
    # a night catching in `cell selfcheck`. Found by running this live.
    add("peer verdict recorded on the card", verdict is not None,
        "" if verdict else "missing or unparseable — expected '<cell> APPROVED <sha>'")

    author = card.get("created_by")
    assignee = card.get("assignee")
    if verdict:
        # Author identity comes from the CARD. Git says the same account for every
        # cell, so it cannot answer "was this reviewed by someone else".
        add("reviewer is not the author", verdict["cell"] != author,
            f"reviewer={verdict['cell']} author={author}")
        # >>> LEG 2 (#144): THE INDEPENDENCE CHECK MUST EXCLUDE THE ASSIGNEE, NOT
        # ONLY created_by. <<<
        #
        # created_by is merely WHO FILED the card. THE ASSIGNEE IS THE PERSON BEING
        # REVIEWED — the one doing the work. Guarding only the former guards the
        # weaker of the two, and on the NORMAL AI² split (filed by one identity,
        # assigned to another) the weaker leg PASSES while the stronger one fails.
        # Measured 2026-08-01: 20 of 213 live cards (9%) have created_by !=
        # assignee. Not an edge case — the standard working shape.
        #
        # WHY BINDING DOES NOT COVER THIS, and it is the subtle part: the assignee
        # is a LEGITIMATELY BOUND writer. `is_exec_assignee` permits them to write
        # peer_verdict on their own card, so every identity check passes CORRECTLY
        # while the reviewee approves themselves. BINDING CLOSES FORGERY — asserting
        # someone else's name — AND DOES NOTHING AGAINST SELF-DEALING, acting under
        # your own true name in a role you should not occupy. Different attacks.
        # (drop-on-meta-edge, correcting lab's own insufficient fix.)
        #
        # SEVERITY CHANGES ON THE GRADUATION DATE WITHOUT THE CODE CHANGING: today a
        # self-verdict is VISIBLE and a human reading the card is the compensating
        # control. Phase 2 makes the authority AUTOMATIC and removes exactly that
        # control. Date-holder's ruling (droplet, 2026-08-01): graduate 2026-08-11
        # as scheduled with phase-2 scope NARROWED — no auto-merge where a verdict
        # author is the assignee — which is a narrowing the register permits rather
        # than an extension it forbids, and fails safe to the human gate.
        add("reviewer is not the assignee", not assignee or verdict["cell"] != assignee,
            f"reviewer={verdict['cell']} assignee={assignee} — the person being "
            f"reviewed cannot supply the review")
        add("verdict is APPROVED", verdict["verdict"] == "APPROVED",
            f"verdict={verdict['verdict']}")
        head = pr_state.get("head_sha") or ""
        # >>> A REVIEW IS OF A COMMIT, NOT OF A BRANCH. <<< Re-pushing after review
        # invalidates it. This is the precondition that will actually fire — review
        # fixes got pushed four times in one night on card #133 alone.
        add("verdict names the current head", bool(head) and head.startswith(verdict["sha"]),
            f"verdict@{verdict['sha']} head={head[:12] or '?'}")

    unresolved = links.get("unresolved_findings", "").strip()
    add("no unresolved findings", not unresolved, unresolved[:80])

    ci = pr_state.get("checks")
    if ci is None:
        add("CI all green (live)", None, "could not query CI — NOT a pass")
    elif not ci:
        # Zero checks is not "all passed". mesh-gateway had ZERO CI for months and
        # merged on trust alone (card #105).
        add("CI all green (live)", False, "no checks reported — zero is not green")
    else:
        bad = [c["name"] for c in ci if c.get("bucket") != "pass"]
        add("CI all green (live)", not bad,
            f"{len(bad)} non-passing: {', '.join(bad[:3])}" if bad
            else f"{len(ci)} legs")

    touched = pr_state.get("files_changed")
    if touched is None:
        add("does not modify the merger itself", None, "file list unavailable — NOT a pass")
    else:
        self_mod = sorted(set(touched) & set(_SELF_PATHS))
        add("does not modify the merger itself", not self_mod,
            f"human-merge-only: {', '.join(self_mod)}" if self_mod else "")

    if any(c.ok is None for c in checks):
        # BLIND BEATS FAILED: if anything could not be evaluated we do not know whether
        # the PR passes, so we must not report a substantive refusal we did not measure.
        v = "CANNOT_EVALUATE"
    elif all(c.ok is True for c in checks):
        v = "WOULD_MERGE"
    else:
        v = "REFUSE"
    return Decision(card_id=str(card.get("id")), verdict=v, checks=checks)


def fetch_pr_state(repo: str, number: str) -> dict:
    """Live PR state via gh. THE ONLY IMPURE FUNCTION HERE.

    Any failure yields None fields rather than optimistic defaults, so an unreachable
    GitHub produces COULD-NOT-EVALUATE and therefore REFUSE — never a silent pass.
    """
    def gh(*args):
        try:
            p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
            return p.stdout if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    # ONE call, and it uses `statusCheckRollup` on `pr view` rather than
    # `pr checks --json`. MEASURED: `gh pr checks --json` DOES NOT EXIST in the
    # installed gh ("unknown flag: --json") — I wrote it from assumption and the live
    # run caught it. The fail-safe held (COULD-NOT-EVALUATE -> REFUSE, never a silent
    # pass), but a check that can never succeed is a check that always refuses, which
    # would have read as "the tool is strict" rather than "the tool is broken".
    out = gh("pr", "view", number, "--repo", repo, "--json",
             "headRefOid,files,statusCheckRollup")
    state: dict = {"head_sha": None, "files_changed": None, "checks": None}
    if not out:
        return state
    try:
        d = json.loads(out)
    except ValueError:
        return state
    state["head_sha"] = d.get("headRefOid")
    files = d.get("files")
    if files is not None:
        state["files_changed"] = [f.get("path") for f in files if f.get("path")]
    roll = d.get("statusCheckRollup")
    if roll is not None:
        # SUCCESS/NEUTRAL/SKIPPED pass; anything else (FAILURE, CANCELLED, TIMED_OUT)
        # does not. A null conclusion means STILL RUNNING — not passed.
        state["checks"] = [
            {"name": c.get("name") or c.get("context") or "?",
             "bucket": "pass" if (c.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED"))
                       else "fail"}
            for c in roll]
    return state


def format_decision(d: Decision) -> str:
    lines = [f"card #{d.card_id}: {d.verdict}"]
    for c in d.checks:
        mark = "ok  " if c.ok is True else ("FAIL" if c.ok is False else "????")
        lines.append(f"  [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
    return "\n".join(lines)


def run_board_merge_check(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph board merge-check",
        description="DRY-RUN: decide whether a card's PR meets the merge preconditions. "
                    "Merges nothing (phase 1, card #137).")
    p.add_argument("--card-json", required=True,
                   help="path to a card JSON (id, stage, created_by, links{pr,peer_verdict})")
    p.add_argument("--repo", help="owner/repo (else parsed from the card's pr link)")
    p.add_argument("--offline", action="store_true",
                   help="skip the gh query; report CI/files as COULD-NOT-EVALUATE")
    args = p.parse_args(argv)

    try:
        with open(args.card_json, encoding="utf-8") as f:
            card = json.load(f)
    except (OSError, ValueError) as e:
        print(f"swarph board merge-check: cannot read card ({e})", file=sys.stderr)
        return 2

    pr_link = (card.get("links") or {}).get("pr", "")
    m = re.search(r"(?:github\.com/)?([\w.-]+/[\w.-]+)/pull/(\d+)", pr_link)
    if not m and not args.offline:
        print("swarph board merge-check: card has no parseable `pr` link "
              "(expected .../<owner>/<repo>/pull/<n>)", file=sys.stderr)
        return 2

    pr_state = ({"head_sha": None, "files_changed": None, "checks": None}
                if args.offline else fetch_pr_state(args.repo or m.group(1), m.group(2)))
    d = decide(card, pr_state)
    print(format_decision(d))
    print("\n  DRY RUN — phase 1 merges nothing. See card #137 for the graduation date.")
    # 0 would-merge · 1 refused on a measured precondition · 2 could not evaluate.
    # A caller that cannot tell 1 from 2 will retry a broken tool forever, or file a
    # bug against a PR that is fine.
    return {"WOULD_MERGE": 0, "REFUSE": 1}.get(d.verdict, 2)
