"""`swarph board merge-check` — the preconditions that replace a human at the merge.

Board card #137. Phase 1 is DRY-RUN: it decides and logs, merges nothing.

>>> EVERY TEST HERE DRIVES `decide()`, WHICH IS PURE. No board, no GitHub, no network.
    The impure half is `fetch_pr_state`, and it is replaced by a dict. <<<
Same rule that made `cell selfcheck`'s five defect shapes reproducible off the boxes
that had them: a test that only passes where the live system is reachable is a test
that gets deleted by whoever cannot run it.

THE POINT OF THESE TESTS is not that the happy path merges. It is that each REFUSAL
fires for a reason that was measured on this mesh:
  · reviewer==author        GitHub cannot tell them apart — every cell is one account
  · stale verdict           review fixes were pushed 4x in one night on card #133
  · zero CI checks          mesh-gateway merged on trust with no CI at all (#105)
  · unreachable GitHub      COULD-NOT-EVALUATE must never read as pass
  · self-modification       a process that can ship changes to its own gate has none
"""
import json

import pytest

from swarph_cli.commands import board_merge_check as mc

SHA = "abc1234def5678"


def _card(**over):
    card = {"id": 152, "stage": "build", "move_ready": True, "created_by": "lab-ovh",
            "links": {"pr": "https://github.com/o/r/pull/152",
                      "peer_verdict": f"droplet APPROVED {SHA}"}}
    card.update(over)
    return card


def _pr(**over):
    st = {"head_sha": SHA, "files_changed": ["src/swarph_cli/commands/timeline.py"],
          "checks": [{"name": "pytest (3.11)", "bucket": "pass"},
                     {"name": "pytest (3.12)", "bucket": "pass"}]}
    st.update(over)
    return st


def _fail(d, name):
    return [c for c in d.blockers if c.name == name]


# ── the happy path, so the refusals mean something ───────────────────────────

def test_all_preconditions_met_would_merge():
    d = mc.decide(_card(), _pr())
    assert d.verdict == "WOULD_MERGE", [(c.name, c.detail) for c in d.blockers]


# ── the refusals, each anchored to a measured incident ────────────────────────

def test_reviewer_cannot_be_the_author():
    """MEASURED: `gh pr review --approve` -> "Can not approve your own pull request".
    Every cell pushes under one GitHub account, so git CANNOT answer this and the
    card must. Author identity therefore comes from `created_by`, never from the PR."""
    d = mc.decide(_card(links={"pr": "https://github.com/o/r/pull/152",
                               "peer_verdict": f"lab-ovh APPROVED {SHA}"}), _pr())
    assert d.verdict == "REFUSE"
    assert _fail(d, "reviewer is not the author")


def test_stale_verdict_blocks_because_a_review_is_of_a_commit():
    """A REVIEW IS OF A COMMIT, NOT OF A BRANCH. Re-pushing after review invalidates
    it — and review-fixes were pushed four times in one night on card #133 alone, so
    this is the precondition that will actually fire in practice."""
    d = mc.decide(_card(), _pr(head_sha="9999999aaaa"))
    assert d.verdict == "REFUSE"
    assert _fail(d, "verdict names the current head")


def test_zero_ci_checks_is_not_green():
    """mesh-gateway had ZERO CI for months and merged on trust alone (card #105).
    An empty check list must never satisfy "all checks passed" vacuously."""
    d = mc.decide(_card(), _pr(checks=[]))
    assert d.verdict == "REFUSE"
    assert _fail(d, "CI all green (live)")


def test_one_failing_leg_blocks():
    d = mc.decide(_card(), _pr(checks=[{"name": "pytest (3.11)", "bucket": "pass"},
                                       {"name": "windows (3.12)", "bucket": "fail"}]))
    assert d.verdict == "REFUSE"
    assert "windows (3.12)" in _fail(d, "CI all green (live)")[0].detail


def test_unreachable_github_is_could_not_evaluate_never_pass():
    """`ok=None` is a THIRD state, and it must refuse. The same empty-vs-blind
    discipline as `cell selfcheck` (read/not-read/not-applicable) and GET /highlights
    (empty/behind/blind): an unreachable check is not a passing check."""
    d = mc.decide(_card(), _pr(checks=None, files_changed=None))
    assert d.verdict == "CANNOT_EVALUATE", "blind must not render as a measured refusal"
    ci = _fail(d, "CI all green (live)")[0]
    assert ci.ok is None, "must be COULD-NOT-EVALUATE, not False"


def test_blind_beats_failed_when_both_are_present():
    """A real precondition failure AND an unevaluable one -> CANNOT_EVALUATE.

    Reporting REFUSE would claim we measured the PR when part of it was never read —
    the same overstatement as `consistent` over an unread crontab (card #133/grok)."""
    d = mc.decide(_card(stage="proposed"), _pr(checks=None))
    assert d.verdict == "CANNOT_EVALUATE"


def test_a_pr_touching_the_merger_is_human_merge_only():
    """A process that can ship changes to its own gate has no gate. Permanent, not a
    phase-1 caution — this PR itself is human-merge-only by its own rule."""
    d = mc.decide(_card(), _pr(files_changed=["src/swarph_cli/commands/board_merge_check.py"]))
    assert d.verdict == "REFUSE"
    assert _fail(d, "does not modify the merger itself")


def test_unresolved_findings_block():
    c = _card()
    c["links"]["unresolved_findings"] = "droplet: transport projection is no evidence"
    assert mc.decide(c, _pr()).verdict == "REFUSE"


def test_wrong_stage_blocks():
    assert mc.decide(_card(stage="proposed"), _pr()).verdict == "REFUSE"


def test_unflagged_card_blocks():
    """move_ready is the reviewer's ball-in-court signal (#70). Without it, a green
    PR on a card nobody flagged would merge on CI alone."""
    assert mc.decide(_card(move_ready=False), _pr()).verdict == "REFUSE"


def test_the_vocabulary_is_one_the_real_board_accepts():
    """>>> THE GUARD FOR THE DEFECT THAT PRODUCED THIS TEST. <<<

    The first version gated on stage "peer-green". The gateway rejects it (400: unknown
    stage), so no card could ever satisfy it — and all tests passed, because the
    FIXTURES BUILT A CARD THE REAL SYSTEM CANNOT PRODUCE. A fixture is not a schema.

    _BOARD_EXECUTE_STAGES in the deployed gateway is {"build", "test"}; these must be a
    subset. If someone invents a stage again, this fails instead of the tool silently
    refusing everything forever.
    """
    assert set(mc.READY_STAGES) <= {"build", "test", "spec", "proposed", "idea", "done"}
    assert "peer-green" not in mc.READY_STAGES


# ── the verdict parser: unrecognised is REPORTED, never guessed ───────────────

@pytest.mark.parametrize("text,ok", [
    ("droplet APPROVED abc1234", True),
    ("drop-on-meta-edge APPROVED abc1234def5678", True),
    ("droplet approved abc1234", False),      # lowercase verdict is not a verdict
    ("droplet APPROVED", False),              # no sha — cannot bind to a commit
    ("APPROVED abc1234", False),              # no reviewer
    ("looks good to me!", False),
    ("", False),
])
def test_verdict_parsing_refuses_to_guess(text, ok):
    """A guessed verdict is an unreviewed merge wearing a review's name."""
    assert (mc.parse_verdict(text) is not None) is ok


def test_missing_verdict_blocks_and_does_not_crash():
    d = mc.decide(_card(links={"pr": "https://github.com/o/r/pull/1"}), _pr())
    assert d.verdict == "REFUSE"
    assert _fail(d, "peer verdict recorded on the card")


# ── phase-1 contract ─────────────────────────────────────────────────────────

def test_nothing_mutating_is_ever_INVOKED(monkeypatch):
    """>>> BEHAVIOURAL, NOT TEXTUAL. THE PREVIOUS VERSION OF THIS TEST WAS A SUBSTRING
    SCAN OF THE SOURCE AND MISSED THE FORMS A FUTURE AUTHOR WOULD ACTUALLY WRITE. <<<

    droplet tested five plausible edits against the old guard's three forbidden strings:
        gh("pr merge --squash 154")                          CAUGHT
        gh("pr","merge",num,"--squash")                      CAUGHT
        gh("pr","merge",num,"--"+"squash")                   MISSED
        MERGE=["pr","merge"]; gh(*MERGE,num)                 MISSED
        gh("api", f"repos/{r}/pulls/{n}/merge","-X","PUT")   MISSED
    The last is the form this module is MOST likely to reach for, because it already hit
    `gh pr checks --json` not existing and fell back to a different gh invocation — a
    future author blocked on `gh pr merge` does exactly that.

    A SOURCE GREP TESTS TEXT; THE CLAIM IS ABOUT BEHAVIOUR. Same defect as `pgrep -f`
    matching its own command line — and as droplet's replacement for it, where he fixed
    the self-match and not the substring match.

    This observes what RAN, so string construction cannot evade it.
    """
    seen = []

    class _Done:
        returncode, stdout, stderr = 1, "", ""

    def spy(argv, **kw):
        seen.append(list(argv))
        return _Done()

    monkeypatch.setattr(mc.subprocess, "run", spy)
    mc.fetch_pr_state("o/r", "154")
    mc.decide(_card(), _pr())

    assert seen, "guard is vacuous if nothing was invoked"
    MUTATING = {"merge", "close", "edit", "delete", "push", "ready", "comment", "review"}
    for argv in seen:
        flat = " ".join(argv)
        assert not (MUTATING & set(argv)), f"mutating verb invoked: {flat}"
        assert "-X" not in argv and "--method" not in argv, f"raw API write: {flat}"
        assert "/merge" not in flat, f"merge endpoint reached: {flat}"


def test_cli_reports_dry_run(tmp_path, capsys):
    p = tmp_path / "card.json"
    p.write_text(json.dumps(_card()), encoding="utf-8")
    rc = mc.run_board_merge_check(["--card-json", str(p), "--offline"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert rc == 2, "offline means COULD NOT EVALUATE (2), never a measured refusal (1)"


# ── #144 leg 2: the independence check must exclude the ASSIGNEE ─────────────

def test_the_ASSIGNEE_cannot_supply_their_own_review():
    """>>> THE HOLE, AND IT NEEDS NO FORGERY. <<< `is_exec_assignee` permits the
    assignee to write peer_verdict on their own card. They write
    "droplet APPROVED <sha>" on a card ASSIGNED to droplet and FILED by the
    orchestrator; the old check compared only against created_by, so
    cell(droplet) != author(lab-ovh) PASSED and the person doing the work
    approved themselves.

    MEASURED 2026-08-01: 20 of 213 live cards (9%) have created_by != assignee —
    the standard AI2 split, not an edge case.

    Binding does not close this: the assignee is a LEGITIMATELY BOUND writer, so
    every identity check passes CORRECTLY. Binding closes FORGERY (asserting
    someone else's name); this closes SELF-DEALING (acting under your own true
    name in a role you should not occupy). Different attacks.
    """
    d = mc.decide(_card(created_by="lab-ovh", assignee="droplet"), _pr())
    # precondition: the OLD leg must still PASS, or this proves nothing about the new one
    assert not _fail(d, "reviewer is not the author"), (
        "the created_by leg fired — this test would pass for the wrong reason")
    assert _fail(d, "reviewer is not the assignee")
    assert d.verdict != "WOULD_MERGE"


def test_a_genuine_third_party_review_still_passes():
    """NON-VACUITY. If the new leg fired on real reviews it would be a blocker,
    not a gate — and third-party review is the entire feature."""
    d = mc.decide(
        _card(created_by="lab-ovh", assignee="droplet",
              links={"pr": "https://github.com/o/r/pull/152",
                     "peer_verdict": f"grok-researcher APPROVED {SHA}"}),
        _pr())
    assert not _fail(d, "reviewer is not the assignee")
    assert d.verdict == "WOULD_MERGE", [(c.name, c.detail) for c in d.blockers]


def test_an_UNASSIGNED_card_does_not_trip_the_new_leg():
    """A card with no assignee has no person-being-reviewed, so the leg must not
    fire. A check that fails on ABSENT data is noise, and noise is how a gate
    stops being read."""
    d = mc.decide(_card(created_by="lab-ovh", assignee=None), _pr())
    assert not _fail(d, "reviewer is not the assignee")


def test_BOTH_legs_are_required_not_either():
    """created_by is who FILED the card; the assignee is who is BEING REVIEWED.
    Either identity self-approving must fail — which is why the fix is
    `by != assignee AND by != created_by`, not a choice between them."""
    both = mc.decide(_card(created_by="droplet", assignee="droplet"), _pr())
    assert _fail(both, "reviewer is not the author")
    assert _fail(both, "reviewer is not the assignee")

    # the FILER self-approving a card assigned to someone else: old leg catches it,
    # new leg correctly stays silent — they are independent guards, not duplicates
    filer = mc.decide(_card(created_by="droplet", assignee="gridiron"), _pr())
    assert _fail(filer, "reviewer is not the author")
    assert not _fail(filer, "reviewer is not the assignee")
