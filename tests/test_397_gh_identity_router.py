"""#397 — the gh identity router: resolve from the CELL, or REFUSE. Never fall back.

COMMANDER, 2026-08-18: "im in for automation, can't be the gate when we are dishing
out multiple PR every hour." The human paste-step does not scale past today.

>>> BUT THE OBVIOUS AUTOMATION WOULD HOLLOW OUT THE CONTROL IT AUTOMATES. <<<
GitHub's gates key on the CREDENTIAL, not on authorship. PR #214 was
authored-of-record by `darw007d` purely because that credential happened to be
active at `gh pr create` time — lab wrote every line. Handing lab the
`reviewers-pixel` token would make it ONE ACTOR HOLDING TWO CREDENTIALS: the review
record would read "reviewers-pixel approved" and the fact would be "the author
approved itself" (see feedback_independence_is_over_actors).

The router is what makes author-of-record mean author-in-fact: the mapping is
one-to-one from a cell's MESH identity, so a cell structurally cannot present
another cell's login. That is why this is a security control and not a convenience,
and why every test below is about what it REFUSES.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


@pytest.fixture
def mapping(tmp_path, monkeypatch):
    p = tmp_path / "gh-identities.json"
    p.write_text(json.dumps({
        "lab-ovh": "lab-ovh",
        "drop-on-meta-edge": "reviewers-pixel",
    }), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(p))
    return p


# ── command detection: the router must be INERT on everything it does not own ──

@pytest.mark.parametrize("cmd", [
    "gh pr review 12 --approve",
    "cd /tmp && gh pr list",
    "GH_HOST=github.com gh api user",
    "foo; gh pr merge 3",
])
def test_recognises_real_gh_invocations(cmd):
    assert ghid.targets_gh(cmd)


@pytest.mark.parametrize("cmd", [
    "grep gh file.txt",
    "echo 'gh is a tool'",
    "ls /usr/bin/gh",
    "python3 -c \"print('gh')\"",
])
def test_does_NOT_match_the_word_gh_in_unrelated_commands(cmd):
    """>>> A ROUTER THAT REWRITES COMMANDS IT DOES NOT OWN IS WORSE THAN NO ROUTER.
    <<< `grep gh` would become `GH_TOKEN=$(...) grep gh` — a mangled command and a
    credential subprocess for nothing."""
    assert not ghid.targets_gh(cmd)


def test_an_explicit_GH_TOKEN_is_left_alone():
    """#332: an explicit argument is a DECISION, not a hint. The operator named a
    credential; stacking a second assignment in front of it would silently override
    the one they chose — the exact ordering bug that shipped in 0.41.6."""
    assert ghid.already_explicit("GH_TOKEN=$(gh auth token --user x) gh pr review 1")


# ── the refusals — the reason this exists ──────────────────────────────────────

def test_an_UNMAPPED_cell_is_REFUSED_not_defaulted(mapping):
    """>>> THE HEADLINE. <<< Falling back to the active account is how a cell runs
    for days as another cell (#360): every request stays internally consistent, so
    no server-side control can fire."""
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("meta-muse")
    msg = str(e.value)
    assert "meta-muse" in msg
    assert "REFUSING RATHER THAN USING THE ACTIVE ACCOUNT" in msg
    assert "lab-ovh, drop-on-meta-edge" in msg or "drop-on-meta-edge, lab-ovh" in msg


def test_an_UNSET_SWARPH_SELF_is_REFUSED_and_the_router_does_not_GUESS(mapping):
    """A default cell name makes a cell act as whichever identity the default
    happened to name — measured on 6 of 6 cells (#243)."""
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve(None)
    assert "WILL NOT GUESS" in str(e.value)


def test_a_MISSING_mapping_file_is_REFUSED_not_treated_as_empty(tmp_path, monkeypatch):
    """SPLIT THE STATE: 'the router is unconfigured' and 'this peer specifically is
    unmapped' are different operator problems with different fixes. Collapsing them
    into one behaviour is how a missing credential gets reported as a broken verb."""
    monkeypatch.setenv(ghid.MAPPING_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("lab-ovh")
    assert "no GitHub identity mapping at" in str(e.value)


def test_a_mapped_cell_resolves(mapping):
    """NON-VACUITY: if it refused everyone, every `gh` call on every cell would
    break — which is a worse outage than the problem it solves."""
    r = ghid.resolve("drop-on-meta-edge")
    assert r.login == "reviewers-pixel"
    assert r.peer == "drop-on-meta-edge"


def test_lab_cannot_resolve_to_the_REVIEWER_identity(mapping):
    """>>> THE TWO-PARTY PROPERTY, AS AN ASSERTION. <<< The mapping is one-to-one
    from the mesh identity, so lab cannot present reviewers-pixel no matter what it
    runs. This is what stops the automation from becoming self-approval wearing a
    second name."""
    assert ghid.resolve("lab-ovh").login == "lab-ovh"
    assert ghid.resolve("lab-ovh").login != "reviewers-pixel"


# ── injection: per-call, never global ─────────────────────────────────────────

def test_injection_is_PER_CALL_and_never_switches_the_box():
    """`gh auth switch` is GLOBAL. Measured 2026-08-11: it would have attributed a
    later MERGE from another session to the reviewer. If this assertion ever fails,
    the router has become the bug it was built to prevent."""
    out = ghid.inject("gh pr review 12 --approve", "reviewers-pixel")
    assert out.startswith("GH_TOKEN=$(gh auth token --user reviewers-pixel) ")
    assert "auth switch" not in out
    assert out.endswith("gh pr review 12 --approve")


# ── the hook envelope ─────────────────────────────────────────────────────────

def _hook(payload, monkeypatch, self_name="drop-on-meta-edge"):
    monkeypatch.setenv("SWARPH_SELF", self_name)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps(payload))
    out = buf.getvalue().strip()
    return rc, (json.loads(out) if out else None)


def test_hook_rewrites_the_command_and_STATES_the_identity(mapping, monkeypatch):
    rc, out = _hook({"tool_name": "Bash",
                     "tool_input": {"command": "gh pr review 5 --approve"}}, monkeypatch)
    assert rc == 0
    cmd = out["hookSpecificOutput"]["updatedInput"]["command"]
    assert cmd.startswith("GH_TOKEN=$(gh auth token --user reviewers-pixel) ")
    # A wrong mapping must be DIAGNOSABLE rather than silently wrong.
    assert "drop-on-meta-edge -> reviewers-pixel" in out["systemMessage"]


def test_hook_DENIES_an_unmapped_cell(mapping, monkeypatch):
    rc, out = _hook({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 5"}},
                    monkeypatch, self_name="meta-muse")
    assert rc == 0          # the hook succeeded; the DECISION is the deny
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "meta-muse" in hso["permissionDecisionReason"]


def test_hook_is_SILENT_on_non_gh_commands(mapping, monkeypatch):
    """Inert on everything it does not own — no output at all, so the tool call is
    passed through byte-identical."""
    rc, out = _hook({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, monkeypatch)
    assert rc == 0 and out is None


def test_hook_FAILS_OPEN_on_a_malformed_payload(mapping, monkeypatch):
    """>>> DELIBERATE ASYMMETRY, AND THE ONE PLACE THIS ROUTER DOES NOT REFUSE. <<<
    A router that cannot parse its envelope has learned NOTHING about the command,
    so denying would brick every Bash call on a harness change. It is the UNMAPPED
    CELL that must fail closed — a known cell with no identity — not an unreadable
    envelope that might not even be a `gh` call."""
    import io, contextlib
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook("{not json")
    assert rc == 0 and buf.getvalue().strip() == ""


def test_hook_does_not_double_inject(mapping, monkeypatch):
    rc, out = _hook({"tool_name": "Bash", "tool_input": {
        "command": "GH_TOKEN=$(gh auth token --user lab-ovh) gh pr view 1"}}, monkeypatch)
    assert rc == 0 and out is None
