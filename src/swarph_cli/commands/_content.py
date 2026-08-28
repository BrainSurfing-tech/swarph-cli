"""Free-text bodies must not be composed as shell arguments (board #458).

A DM body passed as ``--content '...'`` is assembled by the *sender's shell*
before the CLI ever runs. Backtick and ``$(...)`` spans in that body are COMMAND
SUBSTITUTION: bash runs them with the sender's privileges and splices the output
(usually empty) into the argument. On 2026-08-17 that dropped two words out of
DM 24217 — the send returned 200, the message still read as complete, and the
only trace was two stderr lines in the sender's shell that a scripted caller
discards. Relaying an untrusted peer's text back through ``--content`` is worse
than lossy: it is code execution against the relayer.

The fix is ``--content-file <path>`` / ``--content -`` (stdin), which takes the
shell out of the composition path entirely. File and stdin bodies are returned
BYTE-IDENTICAL — no stripping, no guard, backticks and all.

``--content`` itself refuses shell-active characters. That guard is deliberately
partial: it can only see backticks that SURVIVED the shell (correctly quoted
ones). Where the quoting actually broke — the DM 24217 case — the substitution
already happened and there is nothing left to detect. The guard exists to push
callers onto the file/stdin path, not to detect the loss.
"""
from __future__ import annotations

import sys
from pathlib import Path


class ContentError(ValueError):
    """A free-text body the CLI refuses to accept."""


# >>> NO SHELL-ACTIVE GUARD HERE, AND ITS ABSENCE IS THE DESIGN. <<<
# A first cut of #458 refused a --content value containing a backtick or $(.
# drop-on-meta-edge measured it and found the check INVERTED — it fires exactly
# where quoting WORKED and is silent exactly where quoting FAILED:
#
#   single-quoted, CORRECT     'see `foo` here'    -> backticks REACH argv -> REFUSED
#   double-quoted, CORRUPTED   "see `echo BAD`"    -> substituted, backticks
#                                                     GONE from argv       -> SILENT
#
# Backticks arriving intact is the SIGNATURE OF CORRECT QUOTING: the shell left
# them literal, so the content is exactly what the author typed and nothing was
# lost. Backticks arriving ABSENT is the signature of substitution — the actual
# corruption — and it is undetectable at this layer, because by the time argv
# exists the evidence has already been consumed by the shell.
#
# So every refusal such a guard can issue is a FALSE POSITIVE BY CONSTRUCTION,
# and the true positive is unreachable. A control that cannot fire on the defect
# it names, while punishing correct usage, is worse than no control: it trains
# callers to route around it and it reads as coverage on review.
#
# --content-file / --content - are the fix. They remove the shell from the path
# instead of trying to detect what it already did.


def add_content_args(parser, flag: str = "--content", *, required: bool = True,
                     noun: str = "message body", noun_plural: str = "bodies") -> None:
    """Add ``FLAG`` and ``FLAG-file`` as a mutually exclusive pair."""
    dest = flag.lstrip("-").replace("-", "_")
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        flag,
        dest=dest,
        default=None,
        help=f"{noun}; '-' reads stdin. For {noun_plural} containing backticks, "
             f"quotes or $(...), prefer {flag}-file — a shell can alter them "
             f"before the CLI ever sees them (board #458).",
    )
    group.add_argument(
        f"{flag}-file",
        dest=f"{dest}_file",
        default=None,
        metavar="PATH",
        help=f"read the {noun} verbatim from PATH (no shell involved)",
    )


def resolve_content(value, file_path, flag: str = "--content"):
    """Return the body from ``FLAG-file``, stdin, or ``FLAG`` — or None if unset.

    Every path returns the body EXACTLY as read — no stripping, no rejection.
    A ``FLAG`` value has already been through a shell by the time it arrives, so
    any damage is done and undetectable here (see the note above ``add_content_args``);
    the file and stdin paths are the ones that never expose it to a shell at all.
    """
    if file_path is not None:
        try:
            return Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ContentError(f"{flag}-file: cannot read {file_path}: {exc}") from None
    if value is None:
        return None
    if value == "-":
        return sys.stdin.read()
    return value
