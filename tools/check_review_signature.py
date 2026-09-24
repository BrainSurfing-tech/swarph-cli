#!/usr/bin/env python3
"""Fail the merge when a SHARED-CREDENTIAL approval does not name the cell behind it.

Board card #668. What this gate is for, in one measurement: on 2026-09-02 a sweep of
484 merged PRs found `reviewers-pixel` carrying 72% of mesh-gateway's reviews and 79%
of swarph-cli's, and the sys3 box alone holds four gh logins including that one. So an
approval's `user.login` does not identify the actor, and 141 merges have an approver who
cannot be shown independent of the pusher. GitHub's `require_last_push_approval` can
enforce "not the same LOGIN"; nothing can make it enforce "not the same ACTOR" while one
credential is shared. This asks the review to say who wrote it.

SCOPE, stated so nobody later mistakes it for something stronger: this enforces the
PRESENCE of attribution, not its TRUTH. The trailer is self-declared and forgeable by
anyone holding the credential. It converts "unknowable" into "claimed", which is the
whole of the win — an unsigned approval leaves no one to ask, a signed one does.

Only shared logins are asked to sign. An approval from `lab-ovh` or `darw007d` already
names its actor; requiring a trailer there is how a control gets softened in week one
because it annoys people it was never aimed at.

The trailer is strict on purpose:

    Reviewed-by: <cell>

exactly one such line, cell in the allowlist. The looser "— <cell>" sign-off that 97 of
201 reviews already use CANNOT be machine-checked here: review bodies quote peer DMs,
which end that way, and 31 measured bodies name more than one cell — a first-match rule
picks the quoted peer over the author. A prefixed trailer cannot collide with prose.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
TRAILER = re.compile(r"^Reviewed-by:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


def current_approvals(reviews):
    """The approvals that actually satisfy the gate right now.

    GitHub keeps every review row forever, so `state == "APPROVED"` also matches
    approvals a later review superseded and ones a push DISMISSED. Only a user's
    LATEST non-comment review carries their standing verdict — that is the row the
    merge button reads, so it is the row this gate must read.
    """
    latest = {}
    for r in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0)):
        if (r.get("state") or "").upper() == "COMMENTED":
            continue  # a comment never replaces a standing approval
        login = ((r.get("user") or {}).get("login")) or ""
        latest[login] = r
    return [r for r in latest.values() if (r.get("state") or "").upper() == "APPROVED"]


def evaluate(reviews, config):
    """-> (ok, lines). Pure: no network, so the tests can state real cases."""
    shared = {s.lower() for s in config.get("shared_credentials", [])}
    cells = set(config.get("cells", []))
    problems, notes = [], []

    approvals = current_approvals(reviews)
    if not approvals:
        # Nothing to attribute yet. required_approving_review_count enforces that an
        # approval exists at all; this gate has no opinion until one does, so it must
        # be GREEN here or every fresh PR shows a red that means nothing.
        return True, ["no standing approval yet — nothing to attribute (gate is not the approval requirement)"]

    for r in approvals:
        login = ((r.get("user") or {}).get("login")) or "?"
        if login.lower() not in shared:
            notes.append(f"OK  {login}: a login held by one actor — it names its own author")
            continue
        # Normalise CRLF first. `[ \t]*$` under MULTILINE cannot cross a `\r`, so a
        # web-submitted body ending "...edge\r\n" never matches and the gate reports
        # "no trailer" about a body that HAS one — a false RED whose message denies the
        # evidence in front of the reader. drop-on-meta-edge measured the prevalence on
        # PR #145: 0 of 191 reviewers-pixel bodies carry CRLF (all CLI-submitted), but
        # 3 of 12 orchestrators-hue and 10 of 36 copilot bodies do. Latent, then live
        # the moment orchestrators-hue joined shared_credentials in this same review.
        names = TRAILER.findall((r.get("body") or "").replace("\r\n", "\n"))
        if not names:
            problems.append(
                f"{login} approved with no `Reviewed-by:` trailer. That login is shared, so this "
                f"approval currently names nobody."
            )
        elif len(names) > 1:
            problems.append(
                f"{login} approved with {len(names)} `Reviewed-by:` trailers ({', '.join(names)}). "
                f"Exactly one, so the author is unambiguous."
            )
        elif names[0] not in cells:
            problems.append(
                f"{login} approved as `Reviewed-by: {names[0]}`, which is not in the roster in "
                f".github/mesh-review-signing.json. Add the cell there in its own PR, or fix the name."
            )
        else:
            notes.append(f"OK  {login} -> {names[0]}")
    return (not problems), (notes + problems)


def _get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.loads(fh.read().decode())


def fetch_config(repo, base_ref, token):
    """Read the config from the BASE REF, never from the checkout.

    A `pull_request` run checks out the merge commit, which contains the PR's OWN copy
    of this file — so a PR that widens its allowlist would be graded against the
    widened list. The gate has to consult a source the PR under test cannot write.
    """
    import base64
    blob = _get(f"{API}/repos/{repo}/contents/.github/mesh-review-signing.json?ref={base_ref}", token)
    return json.loads(base64.b64decode(blob["content"]).decode())


def fetch_reviews(repo, pr, token):
    out, page = [], 1
    while True:
        got = _get(f"{API}/repos/{repo}/pulls/{pr}/reviews?per_page=100&page={page}", token)
        out += got
        if len(got) < 100:
            return out
        page += 1


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pr = os.environ["PR_NUMBER"]
    base_ref = os.environ["BASE_REF"]
    token = os.environ["GH_TOKEN"]

    config = fetch_config(repo, base_ref, token)
    reviews = fetch_reviews(repo, pr, token)
    ok, lines = evaluate(reviews, config)

    print(f"review-signature gate — {repo}#{pr} (config from {base_ref})")
    for line in lines:
        print("  " + line)
    if ok:
        return 0
    print()
    print("HOW TO CLEAR THIS WITHOUT A NEW COMMIT: edit the approving review's body and add")
    print("a line `Reviewed-by: <your-cell>`. Editing a review fires pull_request_review:edited,")
    print("this check re-runs, and the gate goes green. No re-approval, no push, no re-review.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
