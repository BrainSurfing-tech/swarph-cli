"""The README must not advertise a `--gateway` default the code does not have.

WHY (2026-08-25, found by science-claude in the PUBLISHED 0.49.0 wheel). The release that
removed the baked-in gateway host shipped a README line still claiming one:

    "...and `--gateway` (default `http://localhost:8788`) points at the hub."

That default does not exist. With `MESH_GATEWAY_URL` unset and no `--gateway`, 0.49.0
raises `GatewayNotConfigured`. It does not dial localhost.

>>> THE SAME DEFECT #578 EXISTS TO FIX, IN THE RELEASE THAT FIXED IT, ON THE PAGE A NEW
USER READS FIRST. <<< README.md becomes the PyPI project page, so the wrong claim is the
most-read sentence swarph publishes.

The wheel audit could not have caught it. That audit counted files containing the retired
IP (13 in 0.48.2, 0 in 0.49.0) — both numbers correct, and neither can see a document
PROMISING a fallback the code dropped. "The wrong host is absent" is not "no host is
promised".

## Two failed designs before this one. The failures are the lesson, so they stay.

**v1 matched the literal `localhost:8788`.** Rejected before shipping: it goes blind the
moment someone writes `127.0.0.1` or the next retired IP — exactly how #546's finder
disarmed itself, its own fix having moved the literal into a fallback argument so the
grep never matched again.

**v2 matched a CUE LIST** (`default`, `falls back`, `if unset`, `assumes`, ...) near
`--gateway`. Its comment claimed "written against the property, not the string"; it was
written against a DIFFERENT string. science-claude then ran five phrasings he had NOT
previously reported:

    MISSED  In the absence of `--gateway`, swarph uses `http://localhost:8788`.
    MISSED  `--gateway` is optional; `http://localhost:8788` is used.
    MISSED  Without `--gateway`, requests go to `http://localhost:8788`.
    MISSED  `--gateway` - omit it and swarph talks to `http://localhost:8788`.
    MISSED  The hub is `http://localhost:8788` unless `--gateway` says otherwise.

    novel phrasings caught: 0/5

>>> THE CUE LIST HAD GROWN BY EXACTLY THE PHRASINGS HE REPORTED AND NOTHING ELSE. That is
a denylist fitted to complaints — the shape he flagged on hedge-fund #259 five hours
earlier — reproduced inside the guard built from his own report. It stays invisible until
someone supplies cases from OUTSIDE the report, which is why a guard cannot be validated
by the person who wrote it. <<<

Two limits no cue list reaches: WORD ORDER (*"The hub is X unless `--gateway` says
otherwise"* states the claim first) and CUE-FREE CLAIMS (*"`--gateway` is optional; X is
used"* has no cue word to add).

## v3, this one: INVERT THE PROPERTY (science-claude's design)

Stop enumerating ways to promise a default. Assert what is true instead:

    A line is a CLAIM if it mentions `--gateway` AND contains a host,
    UNLESS that host is the flag's own argument (`--gateway http://...`).

Word order stops mattering, because presence is not ordered. Cue words stop mattering,
because none are consulted. The property is small enough to state in one sentence and to
check exactly — and it still fails in the safe direction: a shape it misses leaves the
README unguarded, it never flags a true line, so nobody is ever taught to mute it.
"""
import pathlib
import re

import pytest

_README = pathlib.Path(__file__).resolve().parent.parent / "README.md"

_GATEWAY_FLAG = re.compile(r"--gateway\b")
_A_HOST = re.compile(r"https?://[^\s`)\]]+|\b\d{1,3}(?:\.\d{1,3}){3}:\d+")
#: The host is the flag's ARGUMENT — `--gateway http://x`, `--gateway=http://x`,
#: `--gateway "$MESH_GATEWAY_URL"`. Those are usage examples, not promises.
_HOST_IS_THE_FLAGS_ARGUMENT = re.compile(
    r"--gateway[=\s]+[\"'`$]*(?:https?://|\d{1,3}(?:\.\d{1,3}){3})"
)


def _lines_claiming_a_gateway_default(path: pathlib.Path) -> list[str]:
    """THE SCANNER. The guard and every can-fail control call THIS.

    >>> THEY MUST SHARE ONE IMPLEMENTATION OR THE CONTROL CONTROLS NOTHING. <<<
    See hedge-fund-mcp `test_queue_ceiling_doc_matches_code.py`, where a can-fail test
    that re-implemented its scanner inline proved *a* regex worked and never that *the*
    regex did — neutering the real scanner left that control green.
    """
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not _GATEWAY_FLAG.search(line):
            continue
        host = _A_HOST.search(line)
        if not host:
            continue
        if _HOST_IS_THE_FLAGS_ARGUMENT.search(line):
            continue                       # a usage example, not a promise
        out.append(f"{i}: pairs --gateway with {host.group(0)!r} without passing it — "
                   f"{line.strip()[:80]}")
    return out


def test_readme_does_not_advertise_a_gateway_default() -> None:
    bad = _lines_claiming_a_gateway_default(_README)
    assert not bad, (
        "README.md pairs --gateway with a host it does not pass; swarph ships no default "
        "and an unconfigured call raises GatewayNotConfigured (#578). README.md is the "
        "PyPI project page, so this is the most-read sentence swarph publishes:\n  "
        + "\n  ".join(bad))


def test_the_readme_states_the_positive_fact() -> None:
    """The other half of the inversion: absence of a false claim is not presence of the
    true one. A README that simply never mentions the subject passes the scan above and
    still leaves a reader guessing what happens with nothing configured."""
    body = _README.read_text(encoding="utf-8").lower()
    assert "no default host" in body, (
        "README.md never states that swarph ships NO default gateway host. Removing the "
        "false claim is not the same as making the true one — say it explicitly.")


@pytest.mark.parametrize("line", [
    # the original, as published in the 0.49.0 wheel
    "`--gateway` (default `http://localhost:8788`) points at the hub.",
    # v2's blind spots, REPORTED by science-claude
    "`--gateway` - if unset, uses http://localhost:8788",
    "`--gateway` assumes http://localhost:8788 when not given",
    # v2's blind spots, NOT reported — supplied from outside the report. These are the
    # ones that matter: a guard validated only against reported cases is fitted to them.
    "In the absence of `--gateway`, swarph uses `http://localhost:8788`.",
    "`--gateway` is optional; `http://localhost:8788` is used.",
    "Without `--gateway`, requests go to `http://localhost:8788`.",
    "`--gateway` - omit it and swarph talks to `http://localhost:8788`.",
    # WORD ORDER REVERSED — unreachable by any cue list requiring flag-before-cue
    "The hub is `http://localhost:8788` unless `--gateway` says otherwise.",
    # host-independence: the next retired IP, and the current fleet IP
    "`--gateway` (default http://100.107.222.72:8788)",
    "`--gateway` defaults to http://100.64.189.91:8788 for the fleet",
])
def test_the_guard_fires(tmp_path, line) -> None:
    """>>> PROVE IT FIRES — THROUGH THE SHIPPED SCANNER, NOT A LOOKALIKE. <<<

    Ten phrasings, three sources: the real defect, the two science-claude reported, and
    five he supplied without reporting first. Neutering `_A_HOST` or `_GATEWAY_FLAG`
    turns all of these red, which is the point of routing every case through
    `_lines_claiming_a_gateway_default`.
    """
    bad = tmp_path / "README.md"
    bad.write_text(line + "\n", encoding="utf-8")

    assert _lines_claiming_a_gateway_default(bad), f"walked past: {line!r}"


@pytest.mark.parametrize("line", [
    "swarph watchdog --check --peer researcher --gateway http://localhost:8788 --dm-wake",
    'swarph monitor start --as x --gateway "$MESH_GATEWAY_URL" --token-file f',
    "swarph onboard researcher --gateway=http://100.64.189.91:8788",
    "[swarph-daemon] starting: self=researcher gateway=http://localhost:8788 poll=30s",
    "$ swarph gateway serve                      # binds 127.0.0.1:8788",
    "`--gateway` / `MESH_GATEWAY_URL` points at the hub. There is no default host.",
])
def test_legitimate_gateway_mentions_are_not_flagged(tmp_path, line) -> None:
    """The other half of can-fail: a detector that fires on EVERYTHING is as useless as
    one that never fires — and worse, because a noisy guard is one someone deletes.

    science-claude found four `localhost:8788` strings in the published METADATA and only
    ONE was false. The other three are here verbatim, so a future tightening that starts
    flagging them fails loudly instead of training someone to mute the test."""
    ok = tmp_path / "README.md"
    ok.write_text(line + "\n", encoding="utf-8")

    assert _lines_claiming_a_gateway_default(ok) == [], f"false positive on: {line!r}"


@pytest.mark.xfail(reason="KNOWN GAP, recorded deliberately: the scan is LINE-scoped, so "
                          "a claim whose `--gateway` mention sits on a previous line is "
                          "invisible. Documented rather than hidden — if someone makes "
                          "the scan paragraph-aware this XPASSes and says so.",
                   strict=False)
def test_a_claim_split_across_lines_is_a_KNOWN_MISS(tmp_path) -> None:
    """The honest limit, and the reason it is not fixed.

    science-claude's case 4 — "if unset it falls back to http://localhost:8788" with no
    `--gateway` on that line — is real README prose, not a contrived string. Widening the
    anchor across lines is where false positives start, and per the test above, a guard
    that flags true lines is a guard someone deletes. So: an xfail naming the gap, rather
    than a fix trading a known miss for an unknown noise rate."""
    bad = tmp_path / "README.md"
    bad.write_text("The `--gateway` flag points at the hub.\n"
                   "If unset it falls back to http://localhost:8788\n", encoding="utf-8")

    assert _lines_claiming_a_gateway_default(bad), "line-scoped scan cannot see this"


def test_the_premise_still_holds() -> None:
    """>>> PREMISE-GONE IS THE ONLY FAILURE THAT STAYS GREEN. <<<

    If swarph ever ships a default host again, this guard becomes WRONG rather than
    unnecessary, and a wrong guard that passes is machinery nobody can explain. Pin the
    reason so the test dies together with it."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "swarph_cli" / "gateway_default.py")
    assert src.exists(), (
        "gateway_default.py is gone — swarph may ship a default host again, in which case "
        "this guard is WRONG, not merely redundant. Delete it deliberately.")
