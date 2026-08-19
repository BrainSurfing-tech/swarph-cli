"""`swarph guide` — print the bundled guide. Card #523.

>>> THIS COMMAND TOUCHES THE NETWORK NEVER. <<< The guide's whole audience is cells that
have not set up yet, so every dependency it carries is a cell it cannot reach:

  - a GATEWAY endpoint is unreachable before you are on the tailnet, and the mesh-gateway
    binds the tailnet IP only. Getting onto the tailnet is step one of onboarding, so a
    gateway-served guide cannot serve the moment it exists for.
  - a gateway endpoint is also down exactly when the troubleshooting content is needed.
  - a published verb that calls a live endpoint is card #496's failure mode: 0.44.0's
    `board cards ask` 404'd for 43 minutes because nothing joins a PyPI release to a
    gateway deploy. On the first surface a new cell touches, that teaches it the mesh is
    broken, on contact.

So this reads a file that shipped inside the wheel. The commander's framing was the DOS-era
HELP.ME / README.1ST, and the analogy is load-bearing rather than nostalgic: THOSE FILES
WORKED BECAUSE THEY HAD NO RUNTIME. You could read one with `type`, with `more`, or by
putting the disk in another machine.

A gateway `/guide` may exist later to add PERSONALISATION the server alone knows (your
channels, your wake_policy, your unread count). It must never become the only copy.
"""
from __future__ import annotations

import argparse
import re
import sys

# >>> THE GUIDE IS PROSE, SO ITS OUTPUT IS THE MOST LIKELY IN THE WHOLE CLI TO CARRY A
# CHARACTER AN 8-BIT CONSOLE CANNOT REPRESENT. <<< On cp1252 (a French Windows box) a bare
# `print` of an em-dash raises UnicodeEncodeError, and the verb dies.
#
# >>> THE MECHANISM BELOW WAS INFERRED AND IS WRONG. KEPT, WITH THE CORRECTION, BECAUSE
# THE INFERENCE IS THE INSTRUCTIVE PART. <<<
#
# lab wrote: "--list survived and --search did not: --list emits topic anchors and
# commands, all ASCII; --search emits prose." MEASURED AGAINST THE SHIPPED 0.45.0 WHEEL,
# that is BACKWARDS:
#
#     guide wake            0 non-ASCII   <- THE INVOCATION ACTUALLY REPORTED
#     guide --list          4 non-ASCII
#     guide channels        3 non-ASCII
#     guide --search wake   1 non-ASCII
#
# The commander reported `swarph guide 'wake'` "doesn't work". That path emits PURE
# ASCII and exits 2 with a topic list -- IT CANNOT RAISE UnicodeEncodeError. What he hit
# was the #520 discovery defect: no wake content existed and the error named nouns
# instead of a route. Both of which are fixed here (the Hooks section, and the error now
# says `try: swarph guide --search wake`).
#
# lab read "search doesn't work" as `--search`, inferred an encoding mechanism from a
# --list/--search asymmetry that does not exist, and built a test suite around it.
# gpu-wsl then could not reproduce the crash on a real fr-FR box -- CORRECTLY, because
# there was no crash on that path. One `swarph guide 'wake' | count non-ASCII` would
# have settled it before any of that.
#
# WHAT SURVIVES: cp1252 IS a real hazard for the CONTENT paths above (--list, a topic,
# --search all emit non-ASCII pre-fix), so print_safe and the pure-ASCII GUIDE.md are
# correct defence-in-depth. They fix a bug NOBODY REPORTED, which is fine -- but the
# record must not claim they fixed the reported one.
#
# console_safe.print_safe already existed with 44 call sites when this module was written
# with bare `print`. Its own docstring records the lesson: "A protection sited inside one
# module protects one module." Reported from Windows by the commander, 2026-08-19.
from ..console_safe import print_safe

# `files()` is the packaged-resource reader — it works from an installed wheel, a zip,
# and an editable checkout alike. Reading via __file__ would work in the source tree and
# break for the people who `pip install`, which is the audience.
try:  # py3.9+
    from importlib.resources import files as _files
except ImportError:  # pragma: no cover - floor is 3.9
    _files = None  # type: ignore[assignment]

_PACKAGE = "swarph_cli.guide"
_FILENAME = "GUIDE.md"


def _load_guide() -> str:
    """Return the bundled guide text.

    >>> RAISES RATHER THAN RETURNING A PLACEHOLDER. <<< A missing GUIDE.md means the wheel
    was built without its package-data, and 0.39.3 shipped exactly that bug — a README
    telling peers to run a script the wheel did not contain. A friendly "guide unavailable"
    string would let that ship again and read as a working command.
    """
    if _files is None:  # pragma: no cover
        raise RuntimeError("importlib.resources.files unavailable")
    return (_files(_PACKAGE) / _FILENAME).read_text(encoding="utf-8")


def _split_topics(text: str) -> "dict[str, str]":
    """Split the guide on its `## ` headings into {anchor: section}.

    The anchor matches the in-file table of contents (lowercased, spaces to hyphens), so
    `swarph guide channels` and the `#channels` link in the document resolve identically —
    one naming scheme, not two that drift.
    """
    topics: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            current = title.lower().replace(" ", "-")
            topics[current] = [line]
        elif current is not None:
            topics[current].append(line)
    return {k: "\n".join(v).rstrip() + "\n" for k, v in topics.items()}


def _commands_in(section: str) -> "list[str]":
    """The `swarph ...` invocations a section teaches, first line of each, deduped."""
    # >>> `how-to` LISTED NO COMMANDS, AND IT IS NOTHING BUT COMMANDS. <<< The rows are
    # markdown table cells -- `| get woken when a DM arrives | \`swarph install-wake-hook\` |`
    # -- so a startswith("swarph ") test saw none of them. The sections that under-reported
    # were exactly the most command-dense ones: how-to, check-your-own-setup, glossary.
    # Match the invocation ANYWHERE in the line, not only at its start.
    # (Commander, running `swarph guide --list`, 2026-08-19.)
    out: list[str] = []
    for raw in section.splitlines():
        for chunk in re.findall(r"(?:^|[|`$]\s*)(swarph\s+[a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)?)",
                                raw):
            s = chunk.strip()
        # Truncate to verb + subcommand FIRST, then dedupe. Deduping the full line and
        # truncating after yields "swarph board cards" three times — the dedupe has to
        # run on the value actually printed, not on its input.
            entry = " ".join(s.split()[:3]).rstrip(" \\")
            if entry not in out:
                out.append(entry)
    return out


def _search(topics: "dict[str, str]", term: str) -> "list[tuple[str, str]]":
    """Return (topic, first matching line) per topic — `apropos`, not full-text dump.

    Matches the WHOLE guide including prose, because the caller is searching by intent
    ('subscribe', 'who owes me') and the word they know may appear only in a sentence.
    One line per topic keeps the answer readable; the topic name is the actionable part.
    """
    # >>> THE FIRST MATCHING LINE IS OFTEN THE WORST ONE. <<< Searching "channel"
    # returned `## Channels` (the heading -- tautological), a wrapped mid-sentence
    # fragment from start-here, and -- the actively misleading one -- the TAIL OF THE
    # MONITOR DEFINITION from the glossary, because it mentions "channels" and sorts
    # earlier in the file than the channel definition itself.
    #
    # So rank rather than take-the-first. A result that answers is worth more than a
    # result that merely contains the word. (Commander, running it, 2026-08-19.)
    needle = term.strip().lower()
    hits: list[tuple[str, str]] = []
    for name, body in topics.items():
        best = None
        for line in body.splitlines():
            low = line.lower()
            if needle not in low:
                continue
            clean = line.strip().lstrip("#").strip().lstrip("> ").strip()
            if not clean:
                continue
            # the section heading restates the topic name and answers nothing
            if line.startswith("## "):
                continue
            score = 0
            if clean.lower().startswith(f"**{needle}**"):
                score = 3          # a glossary DEFINITION of the searched word
            elif clean.startswith("|"):
                score = 2          # a how-to row: a task plus its command
            elif clean.startswith(("`", "swarph ", "**")):
                score = 1          # a command or a bolded lead-in
            if best is None or score > best[0]:
                best = (score, clean[:96])
                if score == 3:
                    break
        if best is not None:
            hits.append((name, best[1]))
    return hits


def run_guide(argv: "list[str]") -> int:
    p = argparse.ArgumentParser(
        prog="swarph guide",
        description="Print the swarph guide. Reads a file bundled in this package — "
                    "no network, no gateway, no token.",
    )
    p.add_argument("topic", nargs="?",
                   help="a topic anchor, e.g. channels. Omit for the whole guide.")
    p.add_argument("--list", action="store_true",
                   help="list the topic anchors and exit")
    p.add_argument("--search", metavar="TERM",
                   help="find topics by INTENT rather than by name (FreeDOS `apropos`). "
                        "Searches the commands and the prose.")
    # >>> AN LLM CELL TYPES `guide search wake` LONG BEFORE IT TYPES `--search`. <<<
    # cursor-win measured all three dialects on a live box, and only one of them reached
    # the careful error:
    #
    #     guide --search wake    ok
    #     guide wake             "no topic 'wake'"      <- names alternatives
    #     guide search wake      argparse: unrecognized arguments  <- a USAGE DUMP that
    #                                                                 does not mention
    #                                                                 --search at all
    #
    # argparse rejects the third before run_guide is ever entered, so the "name the
    # alternatives" branch cannot fire. The natural spelling must BE the correct spelling:
    # a two-token rewrite, before parsing.
    if len(argv) >= 2 and argv[0] in ("search", "find", "apropos"):
        argv = ["--search", *argv[1:]]
    args = p.parse_args(argv)

    # >>> AN IGNORED ARGUMENT RETURNS AN UNFILTERED SUPERSET THAT LOOKS FILTERED. <<<
    # `swarph guide --list hook` ran the full list and DISCARDED "hook" in silence, so
    # the caller reads a complete index as if it were a hook-scoped one. The gateway
    # refuses precisely this on GET /messages, in those words -- and this CLI did what
    # that server forbids. Refuse, and name the two things the caller might have meant.
    if args.topic and (args.list or args.search):
        other = "--list" if args.list else "--search"
        print_safe(f"swarph guide: {other} takes no topic, and {args.topic!r} would have "
                   f"been silently ignored.", stream=sys.stderr)
        print_safe(f"  did you mean:  swarph guide --search {args.topic}", stream=sys.stderr)
        print_safe(f"             or:  swarph guide {args.topic}", stream=sys.stderr)
        return 2

    text = _load_guide()
    topics = _split_topics(text)

    if args.list:
        # >>> THE FRONT PAGE IS AN INDEX OF THINGS YOU CAN TYPE. <<< FreeDOS Help's
        # contents screen lists COMMANDS (apropos, fdisk, format), not chapter titles,
        # so "what can I do" and "how do I do it" are one lookup. Each topic is shown
        # with the commands it teaches.
        for name, body in topics.items():
            cmds = _commands_in(body)
            print_safe(f"{name:<22} {', '.join(cmds[:3]) if cmds else '-'}")
        return 0

    if args.search:
        # `apropos`: an LLM arrives with an INTENT ("subscribe to updates"), not the
        # topic's name ("channels"). Requiring the name is the #520 discovery defect
        # reproduced inside its own fix.
        hits = _search(topics, args.search)
        if not hits:
            print_safe(f"swarph guide: nothing matches {args.search!r}. "
                       f"Topics: {', '.join(topics)}", stream=sys.stderr)
            return 2
        for name, line in hits:
            print_safe(f"{name:<22} {line}")
        return 0

    if not args.topic:
        print_safe(text)
        return 0

    key = args.topic.strip().lower().replace(" ", "-").lstrip("#")
    if key in topics:
        print_safe(topics[key])
        return 0

    # Substring fallback before failing: `swarph guide channel` should find `channels`.
    hits = [k for k in topics if key in k]
    if len(hits) == 1:
        print_safe(topics[hits[0]])
        return 0

    # >>> NAME THE ALTERNATIVES. <<< "unknown topic" tells the caller it was wrong and
    # not what to do instead, which is the same defect as an onboarding page that names a
    # destination without a route.
    # Point at --search, not only at the topic list. The caller arrived with an INTENT
    # word that is not a topic name -- which is the exact failure --search was built for,
    # so the error should route them into it rather than list nouns at them. (cursor-win.)
    print_safe(f"swarph guide: no topic {args.topic!r}."
               + (f" Did you mean: {', '.join(hits)}?" if hits else ""),
               stream=sys.stderr)
    print_safe(f"  try:  swarph guide --search {args.topic}", stream=sys.stderr)
    print_safe(f"Topics: {', '.join(topics)}", stream=sys.stderr)
    return 2


def demo() -> None:
    """Self-check: the guide loads from the installed package and splits into topics."""
    text = _load_guide()
    assert text.strip(), "GUIDE.md is empty"
    topics = _split_topics(text)
    assert "channels" in topics, f"expected a 'channels' topic, got {list(topics)}"
    assert "start-here" in topics, f"expected 'start-here', got {list(topics)}"
    # every topic anchor advertised in the table of contents must resolve
    for anchor in ("channels", "dms", "the-board", "check-your-own-setup"):
        assert anchor in topics, f"table of contents links #{anchor}, no such section"
    # the section returned is the section asked for, not the whole file
    assert topics["channels"].startswith("## Channels")
    assert "## Start here" not in topics["channels"]
    print_safe(f"ok - {len(topics)} topics: {', '.join(topics)}")


if __name__ == "__main__":  # pragma: no cover
    demo()
