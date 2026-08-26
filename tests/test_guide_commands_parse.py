"""Every `swarph ...` command documented in GUIDE.md must PARSE against the real CLI parser.

GUIDE.md opens with a falsifiable promise -- "Every command here can be run as written once
you substitute your own peer name". On 2026-08-26 two lines in its own How-to table did not
parse at all:

    swarph mesh send --to <peer> ...      -> error: the following arguments are required: to
    swarph board cards ask <id> --of ...  -> error: unrecognized arguments: --of --what

Both were in the "I want to..." table -- the first thing a new cell reads. lab-ovh hit the
first one six hours before it was reported and assumed the typo was its own, which is the
whole hazard: a guide that "depends on nothing" is read by cells with no peer to ask, who
cannot tell "I typed it wrong" from "the guide is wrong".

Nothing joined the guide to the parser, so renaming a flag could not turn the guide red.
This is that joint.

WHY NOT `--help`: `swarph mesh send --help` exits 0 whether or not a `--to` flag exists, so
a --help check cannot see the defect that motivated this test. The flags must be parsed.

WHY IT IS SAFE TO RUN SIDE-EFFECTING VERBS: every command is invoked with an unreachable
gateway (127.0.0.1:1) and a scrubbed environment, so nothing can reach the mesh. argparse
runs BEFORE any network call, so a parse error still surfaces; anything that gets past the
parser dies on connection-refused, which this test ignores. That is what lets `mesh send`
and `board cards ask` -- the two verbs that were actually broken -- be covered rather than
skipped. A test that skipped them could not have caught this.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import os
import shlex
import shutil
import subprocess

GUIDE = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "guide" / "GUIDE.md"

PLACEHOLDER = {
    "<you>": "guide-test-cell", "<peer>": "guide-test-cell", "<id>": "1",
    "<message_id>": "1", "<name>": "guide-test-chan", "<path>": "/dev/null",
    "<symbol>": "x", "<query>": "x", "<question>": "x", "<word>": "x", "<date>": "2026-08-26",
    "<from>": "2026-08-01", "<to>": "2026-08-26",
    "<what is owed>": "x", "<h>": "claude", "<uuid>": "0" * 32,
    "<description>": "x", "<gateway>": "http://127.0.0.1:1",
}

# Prose mentions the verbs too ("`swarph monitor` is the supported path"). A DOCUMENTED
# command has a verb plus at least one more token; a bare `swarph <verb>` is prose.
# Stated rather than hidden: this heuristic means a genuinely-2-token command would not be
# covered. None exist today.
MIN_TOKENS = 3


def _extract() -> list[str]:
    text = GUIDE.read_text(encoding="utf-8")
    found: list[str] = []

    for raw in re.findall(r"`(swarph [^`\n]+)`", text):          # inline + How-to table
        found.append(raw)

    for block in re.findall(r"```\n(.*?)```", text, re.S):        # fenced blocks
        buf = ""
        for line in block.splitlines():
            line = line.split("#", 1)[0].rstrip()
            if not buf and not line.lstrip().startswith("swarph"):
                continue
            buf += " " + line.strip().rstrip("\\")
            if not line.rstrip().endswith("\\"):
                found.append(buf.strip())
                buf = ""

    out, seen = [], set()
    for c in (x.strip() for x in found):
        if not c.startswith("swarph ") or c in seen or len(c.split()) < MIN_TOKENS:
            continue
        if "$" in c:                        # shell expansion, not a literal command
            continue
        seen.add(c)
        out.append(c)
    return out


SWARPH = shutil.which("swarph")
UNREACHABLE = "http://127.0.0.1:1"

# argparse's own words for "this does not match my parser". Connection-refused and auth
# failures produce different text and are deliberately ignored -- this test is about the
# CLI surface, not about the mesh being up.
PARSE_ERRORS = ("unrecognized arguments", "the following arguments are required",
                "invalid choice", "expected one argument")

COMMANDS = _extract()


def test_extraction_premise_still_holds():
    """If the regex stops matching, every test below passes vacuously. Pin the premise."""
    assert len(COMMANDS) >= 10, f"extracted only {len(COMMANDS)}: {COMMANDS!r}"
    joined = " ".join(COMMANDS)
    for verb in ("mesh send", "mesh inbox", "board cards", "channel"):
        assert verb in joined, f"{verb!r} vanished from extraction -- guide or regex changed"
    assert "cards ask" in joined, (
        "`board cards ask` is one of the two defects this test was written for; if it stops "
        "being extracted the test passes vacuously on its own founding specimen")


@pytest.mark.skipif(SWARPH is None, reason="swarph not on PATH")
@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[:52])
def test_documented_command_parses(cmd: str) -> None:
    # shlex, not .split() -- `board cards ask <id> <peer> "<what is owed>"` carries a quoted
    # argument, and a naive split would either mangle it or force skipping the command. The
    # first draft skipped every quoted line, which silently excluded `cards ask` -- one of the
    # two defects this test was written for. A test that cannot fail on its own founding
    # specimen is not a test.
    argv = []
    for tok in shlex.split(cmd)[1:]:                  # drop the leading "swarph"
        for k, v in PLACEHOLDER.items():
            tok = tok.replace(k, v)
        argv.append(tok)
    env = {k: v for k, v in os.environ.items()
           if k not in ("MESH_GATEWAY_URL", "SWARPH_BRAIN_GATEWAY")}
    def _run(extra: list[str]):
        r = subprocess.run([SWARPH, *argv, *extra], capture_output=True,
                           text=True, timeout=60, env=env)
        return r, (r.stderr + r.stdout).lower()

    # Prefer an unreachable gateway so nothing can reach the mesh. Not every subcommand
    # accepts --gateway (local ones like `memory get` and the hook verbs do not), so a
    # complaint about that flag ALONE means retry without it -- those verbs make no mesh
    # call to guard against anyway. Distinguishing this from a real defect matters: the
    # first draft of this test reported 9 failures that were all its own injected flag.
    r, blob = _run(["--gateway", UNREACHABLE])
    if "unrecognized arguments" in blob and "--gateway" in blob:
        r, blob = _run([])

    bad = [e for e in PARSE_ERRORS if e in blob]
    assert not bad, (
        f"GUIDE.md documents a command the CLI cannot parse:\n    {cmd}\n"
        f"  argparse said: {bad}\n  {r.stderr.strip()[:300]}")
