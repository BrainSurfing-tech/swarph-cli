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

def test_injection_never_switches_the_box():
    """`gh auth switch` is GLOBAL. Measured 2026-08-11: it would have attributed a
    later MERGE from another session to the reviewer."""
    out = ghid.inject("gh pr review 12 --approve", "reviewers-pixel")
    assert "auth switch" not in out
    assert "reviewers-pixel" in out


@pytest.mark.parametrize("shape", [
    "gh pr review 12",                       # simple
    "cd /tmp && gh pr list",                 # >>> THE ONE THAT WAS INERT <<<
    "echo 249 | xargs -I{} gh pr view {}",   # pipeline + xargs
    "timeout 40 gh pr view 249",             # wrapper
    "( gh pr list )",                        # subshell
    "bash -c 'gh pr merge 3'",               # nested shell
])
def test_the_injected_token_REACHES_a_composed_command(shape):
    """>>> THE TEST THAT WOULD HAVE CAUGHT IT, AND THE ONE THE FIRST DRAFT LACKED. <<<

    The first version asserted the STRING SHAPE — startswith/endswith on the rewritten
    command — and NOTHING EXECUTED. It stayed green while the feature was 100% inert
    for every composed command, because `VAR=x cmd` scopes VAR to `cmd` alone: the
    token landed on `cd`, on `echo`, on whatever ran first. Never on `gh`.

    drop-on-meta-edge found it and named why it was the worst available failure mode:
    the hook returned rc=0 AND a systemMessage naming the resolved identity, while the
    gh process ran under the ambient account. The router's own output was the evidence
    that it had worked.

    So this test asserts the PROPERTY — a real subprocess, further down a real
    composed command, actually sees GH_TOKEN in its environment — instead of asserting
    that the string looks right. `gh` is replaced by a probe so the assertion is about
    the ENVIRONMENT and needs no credential, no network and no GitHub account.
    """
    import subprocess, sys as _sys
    # >>> THE TEST WALKED INTO THE HAZARD THE PRODUCT CODE REJECTS BY NAME. <<<
    # `["bash", "-c", ...]` on the Windows runner resolves to C:\WINDOWS\system32\
    # bash.exe — the WSL launcher — which answered "Windows Subsystem for Linux has
    # no installed distributions" in UTF-16. That is cursor-win's reported bug,
    # committed in a test three files from the fix for it. Being right about a hazard
    # in one file does not stop you walking into it in another.
    #
    # So resolve the shell the same way the PRODUCT does, via the helper that exists
    # for this. On POSIX it is plain "bash"; on win32 it is Git's bash, never System32.
    if _sys.platform == "win32":
        from swarph_cli.commands.hooks import _windows_hook_bash
        shell = _windows_hook_bash()
        if shell == "bash":
            pytest.skip("no non-WSL bash on this runner; the property is POSIX shell "
                        "semantics and a WSL launcher cannot answer it")
    else:
        shell = "bash"
    probe = shape.replace("gh ", 'printenv GH_TOKEN >/dev/null && echo REACHED; : ', 1)
    cmd = ghid.inject(probe, "reviewers-pixel").replace(
        "$(gh auth token --user reviewers-pixel)", "SENTINEL", 1)
    r = subprocess.run([shell, "-c", cmd], capture_output=True, text=True, timeout=15)
    assert "REACHED" in r.stdout, (
        f"the token did NOT reach the gh position in: {shape!r}\n"
        f"  rewritten: {cmd}\n  stdout={r.stdout!r} stderr={r.stderr!r}"
    )


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
    # EXPORT form, not the `VAR=x cmd` prefix — the prefix scopes to the first
    # command only and was inert for everything composed (see the parametrised
    # execution test above, which is the one that actually proves it).
    assert cmd.startswith("export GH_TOKEN=$(gh auth token --user reviewers-pixel); ")
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


# ── drop-on-meta-edge, reviewing #249 against his OWN failing corpus ──────────

@pytest.mark.parametrize("cmd", [
    "if gh pr view 249; then echo yes; fi",
    "while gh pr checks 1; do sleep 1; done",
    "until gh api rate_limit; do sleep 5; done",
    "if true; then :; elif gh pr list; then :; fi",
])
def test_gh_in_a_CONDITION_is_still_a_gh_call(cmd):
    """>>> THE 1 OF 11 FALSE NEGATIVES THAT SURVIVED. <<< `_START` listed then/do/else
    — the words that FOLLOW a condition — and not if/while/until/elif, the words that
    OPEN one. A `gh` call in the condition of an `if` is exactly as much a `gh` call
    as one in its body, and it is a shape people write constantly.

    drop measured it by re-running HIS corpus, not the PR's seven cases: "0/7 false
    positives is measured against your seven cases, not mine, and the claim as
    written is wider than its evidence." So was the token list."""
    assert ghid.targets_gh(cmd), f"missed a gh call in a condition: {cmd!r}"


def test_the_condition_keywords_do_not_match_a_BARE_WORD_ending_in_them():
    """NON-VACUITY: \\b-anchored, so `notif gh` or a path like /usr/if/gh must not be
    matched by the new tokens for the wrong reason. Adding four keywords to a regex
    is exactly where an over-broad match sneaks in — and an over-broad match here
    REFUSES an unrelated command, which is worse than missing one."""
    assert not ghid.targets_gh("echo notifgh")
    assert not ghid.targets_gh("echo whilegh")
