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
import sys

# >>> THE GUIDE IS PROSE, SO ITS OUTPUT IS THE MOST LIKELY IN THE WHOLE CLI TO CARRY A
# CHARACTER AN 8-BIT CONSOLE CANNOT REPRESENT. <<< On cp1252 (a French Windows box) a bare
# `print` of an em-dash raises UnicodeEncodeError, and the verb dies.
#
# `--list` survived and `--search` did not, which is why this shipped: --list emits topic
# anchors and `swarph ...` commands, all ASCII. --search emits matched PROSE LINES. The
# code path was identical; only the DATA flowing through it differed, so a Linux test of
# both said nothing about either.
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
    out: list[str] = []
    for line in section.splitlines():
        s = line.strip().lstrip("$ ").rstrip("\\").strip()
        if not s.startswith("swarph "):
            continue
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
    needle = term.strip().lower()
    hits: list[tuple[str, str]] = []
    for name, body in topics.items():
        for line in body.splitlines():
            if needle in line.lower():
                clean = line.strip().lstrip("#").strip().lstrip("> ").strip()
                if clean:
                    hits.append((name, clean[:96]))
                    break
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
    args = p.parse_args(argv)

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
    print_safe(f"swarph guide: no topic {args.topic!r}."
               + (f" Did you mean: {', '.join(hits)}?" if hits else ""),
               stream=sys.stderr)
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
