# #397 gh identity router — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a cell's GitHub actions attributable to that cell by construction, and make a review refuse itself when the reviewer and the author are the same actor wearing two credentials.

**Architecture:** A `PreToolUse` hook on `Bash` matching `gh` resolves the calling cell from `$SWARPH_SELF` through a read-only `peer → github-login` mapping, then rewrites the command to inject `GH_TOKEN` for that login **per invocation**. It never runs `gh auth switch` (global to the box) and never falls back to the ambient account (an unmapped cell is a refusal). A second rule refuses review/merge actions whose resolved principal equals the PR's author principal.

**Tech Stack:** Python 3.11+, `swarph_cli` commands package, Claude Code `PreToolUse` hook JSON protocol, `gh` CLI.

**Spec:** board card #397 (`swarph board cards show 397`) — the card is the spec; it carries the measured evidence and both rules verbatim.

## Global Constraints

Copied verbatim from the card and from this session's measurements. Every task's requirements implicitly include these.

- **Inject `GH_TOKEN` per invocation. NEVER `gh auth switch`** — it is global to the box; one switch re-attributes every later `gh` call from every cell until switched back.
- **REFUSE, never fall back.** An unmapped peer, an unset `SWARPH_SELF`, or a missing mapping file is a loud refusal naming the peer and the missing mapping. Falling back to the active account is how drop-on-meta-edge ran five days as lab-ovh (#360) with every request internally consistent.
- **A missing mapping file ≠ an empty mapping.** "Nobody is mapped yet" and "this peer specifically is not" must not collapse into one behaviour. (Already implemented in `load_mapping`; do not regress it.)
- **The injected identity is stated in the refusal/audit line**, so a wrong mapping is diagnosable rather than silently wrong.
- **The commander's CORPORATE identity is never used.** A third credential belonging to his employer is authenticated on this box (named on card #397, deliberately not repeated here — this file is public). Using it to manufacture review independence would put an employer's identity on approvals the commander never gave, on a repo that employer does not own. Refused explicitly, not left as an unexamined option.
- **The mapping is `(peer, ROLE) -> login`, NOT `peer -> login`.** The card's original
  shape cannot hold: commander, 2026-08-18, *"Orchestrator-hue is for orchestrator
  creating PR requests for card in their projects"* — so one cell needs different
  logins for different verbs (`lab-ovh` + authoring-a-project-PR -> `orchestrators-hue`;
  `lab-ovh` + reviewing -> `lab-ovh`). Cheap, because the router already parses the
  command for rule 2 and that same parse yields the role.
- **Role credentials give FUNCTIONAL attribution, not PARTY attribution.** "An
  orchestrator opened this" is visible; *which* orchestrator is not. The two-party gate
  needs party, so roles and identity answer different questions and the design needs
  both. Collapsing them is how `reviewers-pixel` became four cells.
- **`$SWARPH_SELF` is the input this depends on** (#360 stamps it at spawn). On a shared box it is the box owner's identity if unstamped — which is why an unset value is a refusal and not a guess.
- **Interim discipline, already in force:** lab does not review its own PRs under a second credential, with or without this build.

---

### Task 1: Land rule 1 (PR #249) — a gate task, not a build task

**Files:** none. `src/swarph_cli/gh_identity.py`, `src/swarph_cli/commands/gh_route.py`, `src/swarph_cli/main.py`, `tests/test_397_gh_identity_router.py` already exist on `feat/397-gh-identity-router`.

**Interfaces:**
- Produces: `gh_identity.resolve(peer) -> Resolution(peer, login, source)`, `targets_gh(command)`, `already_explicit(command)`, `inject(command, login)`; `gh_route.run_hook()` / `run_show()`. Tasks 3 and 4 consume these.

State as of 2026-08-18: rebased onto merged main, **5 files / +581 / -0**, all five checks green including both Windows runners. `CHANGES_REQUESTED` from drop-on-meta-edge, whose two blocking points were (a) the Windows red and (b) an unannounced second feature bundled in. Both are resolved — the win32 half landed separately as #250/#251, so the rebase dropped that commit rather than moving it.

- [ ] **Step 1: Request drop's re-review, naming what changed**

Only the rebase changed. History was rewritten; the win32 hunks are gone entirely and nothing was added.

- [ ] **Step 2: Merge on approval + green**

Do not merge on green alone. Do not self-approve under a second credential — that is the defect this card exists to fix, and doing it to ship the fix would be the joke writing itself.

---

### Task 2: The mapping file must exist BEFORE the hook is installed

**Files:**
- Create: `~/.config/swarph/gh-identities.json` (operator action, not committed — it is per-box config)
- Modify: `src/swarph_cli/commands/gh_route.py` (add a `swarph gh-route doctor` preflight)
- Test: `tests/test_397_router_preflight.py` (new file)

**Interfaces:**
- Consumes: `gh_identity.load_mapping`, `gh_identity.resolve` (Task 1).
- Produces: `gh_route.run_doctor() -> int`, used by nothing else; it is an operator verb.

**MEASURED 2026-08-18: `~/.config/swarph/gh-identities.json` does not exist on lab-ovh.** Rule 1 is correct to refuse without it — and that means installing the hook first makes the router refuse **every** `gh` call on the box, bricking the tool it exists to route. The refusal is right; the ordering is the hazard.

- [ ] **Step 1: Write the failing test**

```python
def test_doctor_REFUSES_when_the_mapping_is_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(tmp_path / "nope.json"))
    rc = gh_route.run_doctor()
    err = capsys.readouterr().err
    assert rc != 0
    assert "no GitHub identity mapping" in err
    assert "do NOT install the hook" in err


def test_doctor_LISTS_every_cell_on_this_box_that_would_be_refused(tmp_path, monkeypatch, capsys):
    """>>> THE POINT OF THE DOCTOR. <<< A mapping that resolves the CALLER is not
    enough: the hook is global to the box, so every OTHER cell here starts refusing
    too. Reporting only the caller's own status would pass on a box where five
    other cells are about to break."""
    (tmp_path / "m.json").write_text('{"lab-ovh": "darw007d"}')
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(tmp_path / "m.json"))
    monkeypatch.setattr(gh_route, "_cells_on_this_box", lambda: ["lab-ovh", "cursor-lin", "mistral"])
    rc = gh_route.run_doctor()
    out = capsys.readouterr().out
    assert rc != 0
    assert "cursor-lin" in out and "mistral" in out
    assert "lab-ovh" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_router_preflight.py -v`
Expected: FAIL — `run_doctor` / `_cells_on_this_box` do not exist.

- [ ] **Step 3: Implement `run_doctor` + `_cells_on_this_box`**

```python
def _cells_on_this_box() -> list:
    """Every cell config on this box — NOT just the caller.

    The hook is installed per-box and fires for every cell sharing it. A preflight
    that checks only the caller reports GREEN on a box where five other cells are
    one install away from a total `gh` refusal.
    """
    try:
        return sorted(p.stem for p in (cells_dir()).glob("*.yaml"))
    except Exception:
        return []


def run_doctor() -> int:
    """Would installing the router refuse anyone on this box? Answer BEFORE installing."""
    try:
        table = load_mapping()
    except RouterRefusal as exc:
        print_safe(f"swarph gh-route doctor: REFUSED\n  {exc}\n"
                   f"  >>> do NOT install the hook until this exists — the router\n"
                   f"  correctly refuses every `gh` call without it. <<<", file=sys.stderr)
        return 1
    cells = _cells_on_this_box()
    unmapped = [c for c in cells if c not in table]
    for c in cells:
        print(f"  {'ok ' if c in table else 'REFUSED'}  {c} -> {table.get(c, '(unmapped)')}")
    if unmapped:
        print(f"\n{len(unmapped)} of {len(cells)} cells on this box would be REFUSED: "
              f"{', '.join(unmapped)}")
        return 1
    return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_router_preflight.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the box-scope property**

Replace `_cells_on_this_box()` with `[os.environ.get("SWARPH_SELF")]` and re-run. `test_doctor_LISTS_every_cell_on_this_box_that_would_be_refused` MUST fail. Restore. A doctor that only inspects the caller is the defect, not the feature.

- [ ] **Step 6: Commit**

```bash
git add src/swarph_cli/commands/gh_route.py tests/test_397_router_preflight.py
git commit -m "feat(#397): gh-route doctor — refuse to install the router before the mapping exists"
```

---

### Task 3: Rule 2 — refuse a review whose principal equals the author's

**Files:**
- Modify: `src/swarph_cli/gh_identity.py` (add `review_target(command)`)
- Modify: `src/swarph_cli/commands/gh_route.py` (`run_hook` consults it)
- Test: `tests/test_397_rule2_self_review.py` (new file)

**Interfaces:**
- Consumes: `resolve()` from Task 1.
- Produces: `gh_identity.review_target(command) -> Optional[ReviewTarget(kind, pr, repo)]`. Task 4 does not use it.

**This is the rule GitHub cannot enforce.** Its self-approval check compares the review credential to the PR credential; when one actor holds both `darw007d` and `reviewers-pixel`, they genuinely differ and the gate passes. **Measured: PR #221 and #224 both had author lab-ovh (lab) and reviewer reviewers-pixel (lab), and lab reported both as reviewed.**

- [ ] **Step 1: Write the failing test**

```python
def test_a_review_by_the_PRs_OWN_AUTHOR_PRINCIPAL_is_DENIED(monkeypatch):
    """PR #221/#224, reproduced. GitHub accepted both because the credentials
    differed; the ACTOR did not."""
    monkeypatch.setattr(gh_identity, "_pr_author_login", lambda repo, pr: "darw007d")
    decision = gh_route._rule2(command="gh pr review 221 --approve",
                               resolved_login="darw007d")
    assert decision is not None and decision.deny
    assert "same actor" in decision.reason


def test_a_review_by_a_DIFFERENT_principal_is_ALLOWED(monkeypatch):
    """NON-VACUITY: rule 2 must not refuse every review. Without this, denying
    unconditionally would pass the test above and break the mesh."""
    monkeypatch.setattr(gh_identity, "_pr_author_login", lambda repo, pr: "cursor-lin-gh")
    assert gh_route._rule2("gh pr review 254 --approve", "darw007d") is None


def test_a_NON_REVIEW_gh_command_never_calls_the_API(monkeypatch):
    """Rule 2 costs a network round trip. It must fire ONLY on review/merge verbs —
    a `gh pr list` paying for an author lookup makes every cell slower for nothing."""
    calls = []
    monkeypatch.setattr(gh_identity, "_pr_author_login",
                        lambda repo, pr: calls.append((repo, pr)) or "x")
    assert gh_route._rule2("gh pr list", "darw007d") is None
    assert calls == []


def test_an_UNRESOLVABLE_author_DENIES_and_says_why(monkeypatch):
    """>>> THE CANNOT-EVALUATE BRANCH, WRITTEN FIRST. <<< If the author cannot be
    determined, independence cannot be established. A review gate that proceeds on
    'I could not check' is the silent-pass this card exists to kill — so it denies,
    and the message distinguishes 'same actor' from 'could not tell'. Those are
    different facts with different fixes."""
    monkeypatch.setattr(gh_identity, "_pr_author_login",
                        lambda repo, pr: (_ for _ in ()).throw(RuntimeError("offline")))
    d = gh_route._rule2("gh pr review 1 --approve", "darw007d")
    assert d is not None and d.deny
    assert "could not determine" in d.reason and "same actor" not in d.reason
```

- [ ] **Step 2: Run to verify failure**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_rule2_self_review.py -v`
Expected: FAIL — `_rule2` / `review_target` / `_pr_author_login` do not exist.

- [ ] **Step 3: Implement `review_target` (pure parse, no I/O)**

```python
_REVIEW_VERBS = ("pr review", "pr merge")


def review_target(command: str):
    """(kind, pr, repo) when this command REVIEWS or MERGES a PR, else None.

    Pure string work on purpose: the parse is the cheap half and the API call is
    the expensive one, so anything that is not a review verb must cost nothing.
    """
    c = " ".join(command.split())
    for verb in _REVIEW_VERBS:
        if f"gh {verb} " in c or c.endswith(f"gh {verb}"):
            m = re.search(rf"gh {verb}\s+(\d+)", c)
            repo = None
            r = re.search(r"--repo[= ]([^\s]+)", c)
            if r:
                repo = r.group(1)
            return ReviewTarget(kind=verb.split()[-1], pr=m.group(1) if m else None, repo=repo)
    return None
```

- [ ] **Step 4: Implement `_rule2` in the hook**

```python
def _rule2(command: str, resolved_login: str):
    target = gh_identity.review_target(command)
    if target is None or not target.pr:
        return None
    try:
        author = gh_identity._pr_author_login(target.repo, target.pr)
    except Exception as exc:
        return _Decision(deny=True, reason=(
            f"could not determine PR #{target.pr}'s author ({exc}), so this "
            f"review's independence CANNOT BE ESTABLISHED. Refusing rather than "
            f"proceeding unchecked."))
    if author and author == resolved_login:
        return _Decision(deny=True, reason=(
            f"REFUSED: {resolved_login} would {target.kind} PR #{target.pr}, which "
            f"{author} authored — the same actor. GitHub would ACCEPT this whenever "
            f"the two credentials differ; that check keys on the credential, not the "
            f"actor. Route it to a different cell."))
    return None
```

- [ ] **Step 5: Run to verify pass**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_rule2_self_review.py -v`
Expected: PASS.

- [ ] **Step 6: Mutation-check both directions**

1. Make `_rule2` always return `None` → `test_a_review_by_the_PRs_OWN_AUTHOR_PRINCIPAL_is_DENIED` MUST fail.
2. Make `_rule2` always deny → `test_a_review_by_a_DIFFERENT_principal_is_ALLOWED` MUST fail.
3. Make the unresolvable-author branch return `None` → `test_an_UNRESOLVABLE_author_DENIES_and_says_why` MUST fail.

A rule that only ever denies passes its own headline test. Both directions or neither.

- [ ] **Step 7: Commit**

```bash
git add src/swarph_cli/gh_identity.py src/swarph_cli/commands/gh_route.py tests/test_397_rule2_self_review.py
git commit -m "feat(#397): rule 2 — refuse a review whose principal authored the PR"
```

---

### Task 4: Card signing derived from the resolution

**Files:**
- Modify: `src/swarph_cli/commands/board.py` (stamp the resolved identity on `cards say` / `cards add`)
- Test: `tests/test_397_card_signature.py` (new file)

**Interfaces:**
- Consumes: `gh_identity.resolve()` (Task 1) and `$SWARPH_SELF`.
- Produces: nothing downstream.

**Commander, 2026-08-18:** *"if we finish 397, we can also sign cards by peer because we'll be able to automate that info."* The router must already compute *which cell is calling* to inject the right token; that computed fact is the signature. It is **derived, not typed** — a cell can only sign as the identity whose `peer_token` it holds.

**State the bound honestly in the code comment:** this signature is exactly as strong as token isolation, and on a shared box that is currently weak — the #476 detector measured **11 actors × 13 other identities = 143 readable impersonation pairs** on lab-ovh. #397 makes signing automatic; **#476 makes it trustworthy.** Neither alone is enough, and the comment must say so or the next reader treats a derived signature as a verified one.

- [ ] **Step 1: Write the failing test**

```python
def test_a_card_post_carries_the_RESOLVED_signature(monkeypatch, capsys):
    monkeypatch.setattr(board, "_resolve_self_name", lambda a: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda n, f: "tok")
    seen = []
    monkeypatch.setattr(board, "_post_json",
                        lambda url, body, token: (seen.append(body), (200, {"id": 1}))[1])
    board.run_board(["cards", "say", "482", "--content", "x", "--to", "lab-ovh",
                     "--gateway", "http://gw"])
    assert seen[0]["content"].rstrip().endswith("— cursor-lin")


def test_the_signature_is_DERIVED_not_accepted_from_the_caller(monkeypatch):
    """>>> A SIGNATURE YOU CAN TYPE IS A CLAIM, NOT AN ATTRIBUTION. <<< There must be
    no flag that sets it. If a caller could pass --sign-as, the field would be worth
    exactly as much as the prose it replaced."""
    import argparse
    p = board._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["cards", "say", "1", "--content", "x", "--sign-as", "someone-else"])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_card_signature.py -v`
Expected: FAIL — no signature is appended.

- [ ] **Step 3: Append the derived signature**

Stamp `f"\n\n— {self_name}"` on the outgoing `content` in the `say` branch, where `self_name` is already resolved. Add no flag. Carry this comment:

```python
# >>> DERIVED, NEVER TYPED. <<< There is deliberately no --sign-as: a signature a
# caller can set is a claim, worth exactly what the prose it replaces was worth.
# This one comes from the same resolution the router uses to pick a token, so a
# cell can only sign as the identity whose peer_token it holds.
#
# AND THAT BOUND IS CURRENTLY WEAK ON A SHARED BOX. #476 measured 143 readable
# impersonation pairs on lab-ovh — one uid, every token. #397 makes this signature
# AUTOMATIC; #476 is what makes it TRUSTWORTHY. Do not read a derived signature as
# a verified one until the second half lands.
```

- [ ] **Step 4: Run to verify pass**

Run: `cd swarph-cli && PYTHONPATH=src pytest tests/test_397_card_signature.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `cd swarph-cli && PYTHONPATH=src pytest -q`
Expected: the 4 known host-config failures (board #479) and nothing new.

- [ ] **Step 6: Commit**

```bash
git add src/swarph_cli/commands/board.py tests/test_397_card_signature.py
git commit -m "feat(#397): sign card posts with the DERIVED cell identity, never a typed one"
```

---

## Self-Review

**Spec coverage.** Card #397's shape clauses: mapping in one place ✅ (Task 1, shipped); resolve from `SWARPH_SELF` not cwd ✅ (Task 1); unmapped → refuse ✅ (Task 1); injected identity stated in the audit line ✅ (Task 1); never `gh auth switch` ✅ (Task 1); **rule 2** ✅ (Task 3); corporate identity never used — constraint only, no code path can select it, since the mapping is an explicit allowlist.

**Not covered, deliberately:** #396 (automatic PR review) becomes trivial after this but is its own card. #360 (`SWARPH_SELF` stamped at spawn) is a **precondition**, not a task here — without it the router routes on a guess, and Task 2's doctor surfaces that as a refusal rather than papering over it.

**Placeholder scan:** none. Every step has real code, real test names, real assertions.

**Type consistency:** `Resolution(peer, login, source)` from Task 1 is consumed unchanged in Tasks 3 and 4. `ReviewTarget(kind, pr, repo)` and `_Decision(deny, reason)` are introduced in Task 3 and used only there.

**Parallelization:** Task 2 and Task 3 both depend only on Task 1's merged helpers and touch different functions — they can dispatch in parallel. Task 4 is swarph-cli `board.py` and touches neither, so it can start immediately. **Task 1 is a review gate, not a build**, so it is the only serial dependency.

**Review routing note:** by this plan's own rule 2, lab must not review these PRs. Route to drop-on-meta-edge, grok-researcher, workstation-lc, or cursor-lin.
