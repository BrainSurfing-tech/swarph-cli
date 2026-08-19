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
    args = p.parse_args(argv)

    text = _load_guide()
    topics = _split_topics(text)

    if args.list:
        for name in topics:
            print(name)
        return 0

    if not args.topic:
        print(text)
        return 0

    key = args.topic.strip().lower().replace(" ", "-").lstrip("#")
    if key in topics:
        print(topics[key])
        return 0

    # Substring fallback before failing: `swarph guide channel` should find `channels`.
    hits = [k for k in topics if key in k]
    if len(hits) == 1:
        print(topics[hits[0]])
        return 0

    # >>> NAME THE ALTERNATIVES. <<< "unknown topic" tells the caller it was wrong and
    # not what to do instead, which is the same defect as an onboarding page that names a
    # destination without a route.
    print(f"swarph guide: no topic {args.topic!r}."
          + (f" Did you mean: {', '.join(hits)}?" if hits else ""),
          file=sys.stderr)
    print(f"Topics: {', '.join(topics)}", file=sys.stderr)
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
    print(f"ok — {len(topics)} topics: {', '.join(topics)}")


if __name__ == "__main__":  # pragma: no cover
    demo()
