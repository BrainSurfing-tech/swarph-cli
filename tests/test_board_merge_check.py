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
                      "peer_verdict": f"droplet APPROVED {SHA}"},
            # #144 leg 2: the DEFAULT fixture is a NORMAL card — a bound
            # third-party verdict. Without a stamp the self-authorship leg reports
            # COULD-NOT-EVALUATE (never a pass), which is correct behaviour and
            # would otherwise make every unrelated test refuse for the wrong
            # reason. Tests that care about the flag override it explicitly.
            "link_stamps": {"peer_verdict": {"by": "droplet", "self_authored": False,
                                             "caller_bound": True,
                                             "at": "2026-08-01T00:00:00Z"}}}
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


# ── #144 leg 2: read the FROZEN, BOUND self_authored flag ────────────────────
# The first version compared verdict["cell"] against card["assignee"].
# drop-on-meta-edge showed it was doubly weak: (i) ASSERTED — the writer controls
# the cell string, so an assignee writing "not-me APPROVED <sha>" passed it; and
# (ii) GATE-TIME — it read the assignee at merge, so a self-verdict laundered by a
# later reassignment passed. Both close by reading the server-computed flag.

def _stamped(self_authored, by="droplet", **over):
    """A card whose peer_verdict carries a link_stamp."""
    return _card(link_stamps={"peer_verdict": {"by": by, "self_authored": self_authored,
                                               "caller_bound": True, "at": "2026-08-01T00:00:00Z"}},
                 **over)


def test_a_SELF_AUTHORED_verdict_is_refused():
    """>>> THE HOLE. <<< `is_exec_assignee` permits the assignee to write
    peer_verdict on their own card, so the person being reviewed supplies the
    review — no forgery, no privilege escalation. 20 of 213 live cards (9%) have
    created_by != assignee, so the created_by leg passes on the normal split."""
    d = mc.decide(_stamped(True, created_by="lab-ovh"), _pr())
    assert not _fail(d, "reviewer is not the author"), (
        "the created_by leg fired — this would pass for the wrong reason")
    assert _fail(d, "verdict is not self-authored")
    assert d.verdict != "WOULD_MERGE"


def test_an_ASSERTED_non_self_name_CANNOT_dodge_it():
    """WEAKNESS (i) OF THE FIRST VERSION, pinned. The assignee writes a verdict
    naming someone else. The old leg compared that ASSERTED string and passed;
    the flag is computed server-side from the BOUND writer, so the string is
    irrelevant."""
    card = _stamped(True, created_by="lab-ovh",
                    links={"pr": "https://github.com/o/r/pull/152",
                           "peer_verdict": f"not-me APPROVED {SHA}"})
    d = mc.decide(card, _pr())
    assert _fail(d, "verdict is not self-authored"), (
        "an asserted non-self name defeated the self-review leg")


def test_a_LATER_REASSIGNMENT_cannot_launder_a_self_verdict():
    """WEAKNESS (ii), pinned. droplet self-verdicts, then the work is reassigned.
    A gate-time comparison sees cell != assignee(new) and passes; the FROZEN flag
    still says the verdict was self-authored when written."""
    card = _stamped(True, created_by="lab-ovh", assignee="gridiron")  # reassigned away
    d = mc.decide(card, _pr())
    assert _fail(d, "verdict is not self-authored")


def test_a_genuine_third_party_review_still_passes():
    """NON-VACUITY. If the leg fired on real reviews it would be a blocker, not a
    gate — and third-party review is the entire feature."""
    card = _stamped(False, by="grok-researcher", created_by="lab-ovh",
                    links={"pr": "https://github.com/o/r/pull/152",
                           "peer_verdict": f"grok-researcher APPROVED {SHA}"})
    d = mc.decide(card, _pr())
    assert not _fail(d, "verdict is not self-authored")
    assert d.verdict == "WOULD_MERGE", [(c.name, c.detail) for c in d.blockers]


def test_a_MISSING_stamp_is_COULD_NOT_EVALUATE_never_a_pass():
    """>>> ABSENCE IS NOT INNOCENCE. <<< A verdict written before the flag existed
    has no stamp. Treating that as "not self-authored" would make this leg vacuous
    for exactly the backlog it must cover — the empty-subject defect, in the fix
    for a self-review defect. ok=None is this file's existing doctrine."""
    for absent in ({}, None, {"some_other_key": {"by": "x"}}):
        d = mc.decide(_card(created_by="lab-ovh", link_stamps=absent), _pr())
        assert _fail(d, "verdict is not self-authored"), (
            f"a verdict with NO peer_verdict stamp passed (link_stamps={absent!r}) "
            "— absence read as innocence")
        check = [c for c in d.blockers if c.name == "verdict is not self-authored"][0]
        assert check.ok is None, "must be COULD-NOT-EVALUATE, not a hard False"


def test_the_summary_DISTINGUISHES_failed_from_could_not_evaluate():
    """>>> CONDITION 1 OF THE DATE-HOLDER'S RULING. <<< (droplet, #144.)

    "A gate that graduates and then refuses everything is INDISTINGUISHABLE from a
    gate that never graduated — from the outside, and from the board."

    A REFUSE that fails a check and a REFUSE that could not see its subject are
    different facts with different decay: the first is the gate working, the
    second shrinks to zero as verdicts are rewritten — or doesn't, which is the
    dead-control signal. They must not print the same.
    """
    d = mc.decide(_card(created_by="lab-ovh", link_stamps={}), _pr())
    out = mc.format_decision(d)
    assert "COULD-NOT-EVALUATE" in out
    assert "blind:" in out
    assert "verdict is not self-authored" in out.split("\n")[0], (
        "the abstaining check must be NAMED on the summary line, not only "
        "buried in the per-check list — a count cannot tell a stamp-less backlog "
        "from a GitHub outage")


def test_a_clean_pass_does_not_print_an_abstention_summary():
    """NON-VACUITY. If the summary always printed the tri-state it would be noise
    on every healthy run, and noise is how a gate stops being read."""
    out = mc.format_decision(mc.decide(_card(), _pr()))
    assert "COULD-NOT-EVALUATE" not in out
    assert out.split("\n")[0] == "card #152: WOULD_MERGE"


def test_a_READY_card_with_NO_peer_verdict_REFUSES():
    """>>> drop-on-meta-edge's pin, and it guards a live emptiness. <<< MEASURED
    2026-08-01: 13 cards are in build/test and ZERO carry a peer_verdict link. So
    at arming EVERY ready card hits "peer verdict recorded on the card" = False →
    REFUSE → human gate. The gate is safe-BY-EMPTINESS today.

    That is the same empty-subject shape found all night (group_grants, the verdict
    store) except here it fails SAFE — refuse, not pass — which is the one
    direction that makes an empty subject acceptable rather than a defect.

    Pinned so the day someone "helpfully" defaults a missing verdict to pass, it
    fails loudly instead of silently auto-merging an unreviewed card.
    """
    d = mc.decide(_card(links={"pr": "https://github.com/o/r/pull/152"}), _pr())
    assert _fail(d, "peer verdict recorded on the card")
    assert d.verdict != "WOULD_MERGE"
