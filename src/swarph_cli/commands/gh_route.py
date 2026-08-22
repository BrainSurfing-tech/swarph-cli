"""``swarph gh-route`` — the #397 PreToolUse hook body, and its inspector.

Two entry points, one resolver:

  ``swarph gh-route hook``    reads a Claude Code PreToolUse payload on stdin and
                              emits the hook JSON that rewrites `gh ...` into
                              `GH_TOKEN=$(gh auth token --user <login>) gh ...`,
                              or DENIES when the cell is unmapped.
  ``swarph gh-route show``    prints what THIS cell would resolve to, and exits
                              non-zero on a refusal.

>>> `show` EXISTS BECAUSE A ROUTER YOU CANNOT INTERROGATE IS A SECOND AMBIENT
IDENTITY. <<< The whole defect class here is that structured provenance and prose
provenance disagreed and nothing could notice. An operator must be able to ask
"who am I about to be?" without performing a write to find out — the same reason
GET /whoami exists on the gateway and the same reason a DELETE was the wrong way
to learn a token's binding.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from swarph_cli import gh_identity as ghid
from swarph_cli.cell import cells_dir
from swarph_cli.console_safe import print_safe


def _deny(reason: str) -> int:
    """Emit a PreToolUse denial. The reason is operator-facing and must name the
    remedy — a refusal that does not carry its escape is #468's class."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"swarph gh-route (#397): {reason}",
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _allow_unchanged() -> int:
    """Say nothing. An empty output leaves the tool call exactly as it was — the
    router must be inert on every command it does not own."""
    return 0


def run_hook(stdin_text: Optional[str] = None) -> int:
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    # PS 5.1 native pipes prefix a (double) UTF-8 BOM; strip before parse.
    raw = (raw or "").lstrip("\ufeff")
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        # >>> FAIL OPEN ON A MALFORMED PAYLOAD, NOT CLOSED. <<< A router that cannot
        # parse its input has learned nothing about the command, so denying would
        # brick every Bash call on a harness change. It is the UNMAPPED CELL that
        # must fail closed, not the unreadable envelope.
        return _allow_unchanged()

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not ghid.targets_gh(command):
        return _allow_unchanged()
    if ghid.already_explicit(command):
        # The caller named a credential. #332: an explicit argument is a decision.
        return _allow_unchanged()

    peer = os.environ.get("SWARPH_SELF", "").strip()
    try:
        res = ghid.resolve(peer or None)
    except ghid.RouterRefusal as exc:
        return _deny(str(exc))

    rewritten = ghid.inject(command, res.login)
    updated = dict(payload.get("tool_input") or {})
    updated["command"] = rewritten
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        },
        # >>> STATE THE IDENTITY. <<< A wrong mapping must be DIAGNOSABLE rather
        # than silently wrong; the injected identity is the one fact a reader needs
        # and the one the old ambient path never printed.
        "systemMessage": f"gh identity: {res.peer} -> {res.login} (#397 router)",
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


def run_show() -> int:
    peer = os.environ.get("SWARPH_SELF", "").strip()
    try:
        res = ghid.resolve(peer or None)
    except ghid.RouterRefusal as exc:
        print_safe(f"swarph gh-route: REFUSED\n  {exc}", file=sys.stderr)
        return 1
    print_safe(f"cell        {res.peer}")
    print_safe(f"github      {res.login}")
    print_safe(f"mapping     {res.source}")
    print_safe(f"injected as GH_TOKEN=$(gh auth token --user {res.login}) <your gh command>")
    return 0


def _cells_on_this_box() -> list:
    """Every cell config on this box — NOT just the caller.

    >>> THE HOOK IS INSTALLED PER-BOX AND FIRES FOR EVERY CELL SHARING IT. <<< A
    preflight that inspects only the caller reports GREEN on a box where five other
    cells are one install away from a total `gh` refusal. lab-ovh runs eleven cells
    on one uid; a caller-scoped check there measures the wrong population, which is
    the defect this whole card family keeps finding.

    >>> IT MUST RETURN THE MESH NAME, NOT THE FILENAME. <<< The router resolves
    `SWARPH_SELF` (run_show, and the hook path) and looks THAT up in the mapping. A
    preflight keyed on `p.stem` is asking a different question than the thing it
    certifies, and the two only agree when the filename happens to equal the mesh name.

    Found on lab-ovh 2026-08-18: `lab.yaml` carries `name: lab-ovh` with `role: lab` —
    a deliberate split (commander, 2026-05-28: "I want lab to be lab"; the mesh name is
    lab-ovh, the display role is lab). Doctor reported `lab -> (unmapped)` while
    SWARPH_SELF=lab-ovh WAS mapped and would have worked.

    Both directions are wrong, and the second is the dangerous one:
      FALSE POSITIVE  doctor cries refusal for a cell that resolves fine
      FALSE NEGATIVE  "fix" it by adding the FILENAME key and doctor goes GREEN while
                      the router still looks up the mesh name — a preflight certifying
                      an install that refuses every gh call. A green that means nothing
                      is worse than a red that means nothing.
    """
    import yaml  # local import — keeps `swarph --version` PyYAML-free (cell.py:254)

    out = []
    try:
        files = sorted(cells_dir().glob("*.yaml"))
    except Exception:
        return []

    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            name = doc.get("name") if isinstance(doc, dict) else None
        except OSError as exc:
            # >>> AN UNREADABLE CONFIG IS NOT A CELL NAMED AFTER ITS FILE. <<< The first
            # version's docstring promised this distinction and the CODE DID NOT MAKE IT:
            # OSError and no-name-found both set name=None and both fell through to
            # f.stem, so an unreadable config was silently reported as a mesh name that
            # nothing had asserted. gpu-wsl, reviewing PR #262: "the docstring asserts a
            # safety property the code does not have, which is exactly the invents-a-fact
            # failure that same docstring warns against."
            #
            # Report it as unreadable and let doctor surface it. Coverage lab cannot
            # establish must not be reported as coverage it has.
            print_safe(f"  UNREADABLE  {f.name}: {exc} — cannot determine this cell's "
                       f"mesh name, so it is NOT counted as covered", file=sys.stderr)
            continue
        except yaml.YAMLError as exc:
            print_safe(f"  UNPARSEABLE {f.name}: {exc} — cannot determine this cell's "
                       f"mesh name, so it is NOT counted as covered", file=sys.stderr)
            continue
        # Fall back to the stem ONLY when the file parsed and declares no name — a real
        # fact about a readable config, not a guess about an unreadable one.
        out.append(name if isinstance(name, str) and name else f.stem)
    return sorted(out)


def run_doctor() -> int:
    """Would installing the router REFUSE anyone on this box? Answer BEFORE installing.

    >>> THE ORDERING IS THE HAZARD, NOT THE REFUSAL. <<< Rule 1 is CORRECT to refuse an
    unmapped cell — falling back to the ambient account is how a cell ran five days as
    another cell (#360). But `~/.config/swarph/gh-identities.json` does not exist on
    lab-ovh, so installing the hook first makes the router refuse EVERY `gh` call on
    the box, discovered one broken command at a time.

    drop-on-meta-edge measured that the blast radius is WIDER THAN `gh`: the invocation
    regex has 4 known false positives, all quoting, so
    `git commit -m "fix; gh routing was wrong"` matches, resolves to an unmapped cell,
    and the COMMIT is refused. An unmapped box does not merely lose `gh` — it loses
    every command that mentions one inside a string.

    Exit 0 only when every cell on this box resolves. Non-zero is "do not install yet".
    """
    try:
        table = ghid.load_mapping()
    except ghid.RouterRefusal as exc:
        print_safe(
            f"swarph gh-route doctor: REFUSED\n  {exc}\n"
            f"  >>> DO NOT INSTALL THE HOOK UNTIL THIS EXISTS. <<< The router correctly\n"
            f"  refuses every `gh` call without it — and, per the invocation regex's\n"
            f"  known false positives, every command that merely QUOTES one.",
            file=sys.stderr)
        return 1

    cells = _cells_on_this_box()
    if not cells:
        # >>> AN EMPTY CELL LIST IS NOT A CLEAN BOX. <<< It is a box whose config
        # directory is missing or unreadable, and "0 of 0 refused, all good" would be
        # a green derived from having looked nowhere.
        print_safe(
            f"swarph gh-route doctor: CANNOT EVALUATE — no cell configs found under\n"
            f"  {cells_dir()}. This is not a clean box, it is an unread one. The hook\n"
            f"  fires for every cell here and this check cannot see any of them.",
            file=sys.stderr)
        return 1

    unmapped = [c for c in cells if not isinstance(table.get(c), str) or not table.get(c)]
    for c in cells:
        login = table.get(c)
        ok = isinstance(login, str) and login
        print(f"  {'ok     ' if ok else 'REFUSED'}  {c} -> {login if ok else '(unmapped)'}")
    print(f"\nmapping: {ghid.mapping_path()}")
    if unmapped:
        print(f">>> {len(unmapped)} of {len(cells)} cells on this box would be REFUSED: "
              f"{', '.join(unmapped)}")
        print("    Add them to the mapping before installing the hook.")
        return 1
    print(f"all {len(cells)} cells on this box resolve — safe to install")
    return 0


def run_gh_route(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="swarph gh-route",
        description="#397 GitHub identity router: resolve a cell's gh identity, or refuse.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="PreToolUse hook body (reads the payload on stdin)")
    sub.add_parser("show", help="print what THIS cell resolves to; non-zero on refusal")
    sub.add_parser("doctor", help="would installing the hook refuse anyone ON THIS BOX?")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        return run_hook()
    if args.cmd == "doctor":
        return run_doctor()
    return run_show()
