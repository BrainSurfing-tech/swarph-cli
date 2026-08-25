"""The README must not advertise a `--gateway` default the code does not have.

WHY (2026-08-25, found by science-claude in the PUBLISHED 0.49.0 wheel). The release
that removed the baked-in gateway host shipped a README line still claiming one:

    "...and `--gateway` (default `http://localhost:8788`) points at the hub."

That default does not exist. With `MESH_GATEWAY_URL` unset and no `--gateway`, 0.49.0
raises `GatewayNotConfigured`. It does not dial localhost.

>>> THIS IS THE SAME DEFECT #578 EXISTS TO FIX, IN THE RELEASE THAT FIXED IT, ON THE
PAGE A NEW USER READS FIRST. <<< README.md becomes the PyPI project page, so the wrong
claim is the most-read sentence swarph publishes. The operational cost is the shape
hedge-fund's #259 already paid for: someone reads "default localhost", assumes the CLI
works unconfigured, and meets an exception they were told not to expect.

The count that found the missing IP could not have found this. "The wrong host is
absent" is not "no host is claimed" — a doc can still promise a fallback the code
dropped. Absence-by-design and absence-by-accident produce the same grep, so this
asserts the POSITIVE property instead.
"""
import pathlib
import re

import pytest

_README = pathlib.Path(__file__).resolve().parent.parent / "README.md"

# >>> HOST-INDEPENDENT, AND ONLY PARTLY PHRASING-INDEPENDENT. SAY SO. <<<
#
# Matching `localhost:8788` would go blind the moment someone writes `127.0.0.1:8788`
# or the next retired IP — exactly how #546's finder disarmed itself (its own fix moved
# the literal into a fallback argument and the grep never matched again). This matches
# ANY host, so that class is genuinely avoided; science-claude verified it catches the
# next retired IP and the current fleet IP.
#
# But the first version of this comment claimed "written against the property, not the
# string", and that OVERCLAIMED. It required the literal word `default`, so the property
# it enforced was "a default is named using the word DEFAULT" — a different string, not
# a property. Three ordinary rewordings walked past it, and a README rewrite is exactly
# when someone reaches for "falls back to". (science-claude, review of PR #320.)
#
# So, honestly: THIS CATCHES THE PHRASINGS WE THOUGHT OF, AND THE CAN-FAIL TESTS NAME
# WHICH ONES THOSE ARE. A cue list is a denylist in better clothes. The one thing that
# makes it acceptable is the direction it fails: a phrasing we missed leaves the README
# unguarded, it never flags a true line — so nobody is ever taught to mute this test.
_DEFAULT_CUES = r"defaults?|falls?\s+back|if\s+unset|assumes|when\s+omitted|otherwise\s+uses"

_CLAIMS_A_DEFAULT = re.compile(
    rf"--gateway[^\n]{{0,80}}?(?:{_DEFAULT_CUES})[^\n]{{0,40}}?"
    r"(https?://[^\s`)]+|\b\d{1,3}(?:\.\d{1,3}){3}\b)",
    re.IGNORECASE,
)


def _lines_claiming_a_gateway_default(path: pathlib.Path) -> list[str]:
    """THE SCANNER. The guard and its can-fail control both call THIS.

    They must share one implementation or the control controls nothing — see
    hedge-fund-mcp `test_queue_ceiling_doc_matches_code.py`, where a can-fail test that
    re-implemented its scanner inline proved that *a* regex worked and never that *the*
    regex did.
    """
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _CLAIMS_A_DEFAULT.search(line)
        if m:
            out.append(f"{i}: claims a --gateway default of {m.group(1)!r} — {line.strip()[:88]}")
    return out


def test_readme_does_not_advertise_a_gateway_default() -> None:
    bad = _lines_claiming_a_gateway_default(_README)
    assert not bad, (
        "README.md promises a --gateway default; swarph ships none and an unconfigured "
        "call raises GatewayNotConfigured (#578). README.md is the PyPI project page, so "
        "this is the most-read sentence swarph publishes:\n  " + "\n  ".join(bad)
    )


def test_the_guard_can_fail(tmp_path) -> None:
    """>>> PROVE IT FIRES — through the shipped scanner, not a lookalike. <<<

    A detector that has only seen clean input is indistinguishable from one that matches
    nothing. Neutering `_CLAIMS_A_DEFAULT` turns THIS red too, which is the whole point
    of routing both tests through `_lines_claiming_a_gateway_default`.
    """
    bad = tmp_path / "README.md"
    bad.write_text(
        "the bearer is `--token-file`, and `--gateway` (default `http://localhost:8788`)\n",
        encoding="utf-8")

    hits = _lines_claiming_a_gateway_default(bad)

    assert hits, "the claim pattern is not detectable — the real guard is vacuous"
    assert "localhost:8788" in hits[0]


def test_it_fires_on_a_DIFFERENT_host_too(tmp_path) -> None:
    """The property is 'a default is claimed', not 'localhost is claimed'.

    #546's finder matched one syntax and its own fix moved the literal out of reach, so
    it found the bug once and went permanently blind. A guard that only knows the host
    it was written against would pass the next retired IP straight through."""
    bad = tmp_path / "README.md"
    bad.write_text("`--gateway` defaults to http://100.64.189.91:8788 for the fleet\n",
                   encoding="utf-8")

    assert _lines_claiming_a_gateway_default(bad), (
        "the guard is fitted to 'localhost' and would miss the next baked-in address")


def test_it_fires_on_phrasings_that_never_say_DEFAULT(tmp_path) -> None:
    """>>> THE BLIND SPOT, AS A TEST RATHER THAN A REVIEW COMMENT. <<<

    The first version required the literal word `default`, so it enforced "a default is
    named using the word DEFAULT" — not "a fallback host is promised". science-claude ran
    it against seven cases and these two walked straight past. A README rewrite is
    precisely when someone reaches for "falls back to", so the miss was not exotic.

    Seeded here so the gap has a test instead of living in a DM. Anyone narrowing
    `_DEFAULT_CUES` back down turns this red.
    """
    for line in (
        "`--gateway` — if unset, uses http://localhost:8788\n",
        "`--gateway` assumes http://localhost:8788 when not given\n",
    ):
        bad = tmp_path / "README.md"
        bad.write_text(line, encoding="utf-8")
        assert _lines_claiming_a_gateway_default(bad), f"walked past: {line!r}"


@pytest.mark.xfail(reason="KNOWN GAP, recorded deliberately: the scan is LINE-scoped, so "
                          "a claim whose `--gateway` mention sits on a previous line is "
                          "invisible to it. Documented rather than hidden — if someone "
                          "makes the scan paragraph-aware this XPASSes and says so.",
                   strict=False)
def test_a_claim_split_across_lines_is_a_KNOWN_MISS(tmp_path) -> None:
    """The honest limit. `_lines_claiming_a_gateway_default` iterates lines, so a README
    that mentions `--gateway` in one sentence and promises the fallback in the next is
    unguarded. science-claude's case 4 — "if unset it falls back to http://localhost:8788"
    with no `--gateway` on that line — is real README prose, not a contrived string.

    An xfail rather than a fix because widening the anchor across lines is where false
    positives start, and a guard that flags true lines is a guard someone deletes."""
    bad = tmp_path / "README.md"
    bad.write_text("The `--gateway` flag points at the hub.\n"
                   "If unset it falls back to http://localhost:8788\n", encoding="utf-8")

    assert _lines_claiming_a_gateway_default(bad), "line-scoped scan cannot see this"


def test_legitimate_gateway_mentions_are_not_flagged(tmp_path) -> None:
    """The other half of can-fail: a detector that fires on everything is as useless as
    one that never fires — and a muted guard is worse than no guard.

    These three shapes are all CORRECT and all mention a host next to `--gateway`.
    science-claude found four `localhost:8788` strings in the published METADATA and
    only ONE was false; the other three are seeded here so a future tightening that
    starts flagging them fails loudly instead of teaching someone to delete the test."""
    ok = tmp_path / "README.md"
    ok.write_text(
        "$ swarph gateway serve                      # binds 127.0.0.1:8788\n"
        "[swarph-daemon] starting: self=researcher gateway=http://localhost:8788 poll=30s\n"
        "swarph watchdog --check --peer researcher --gateway http://localhost:8788\n",
        encoding="utf-8")

    assert _lines_claiming_a_gateway_default(ok) == []


def test_the_premise_still_holds() -> None:
    """>>> PREMISE-GONE IS THE ONLY FAILURE THAT STAYS GREEN. <<<

    If swarph ever ships a default host again, this guard becomes wrong rather than
    unnecessary, and a wrong guard that passes is machinery nobody can explain. Pin the
    reason so the test dies together with it."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "swarph_cli" / "gateway_default.py")
    assert src.exists(), (
        "gateway_default.py is gone — swarph may ship a default host again, in which "
        "case this guard is WRONG, not merely redundant. Delete it deliberately.")
