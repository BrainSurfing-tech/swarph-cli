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


_SHELL_ACTIVE = (
    ("`", "a backtick (`)"),
    ("$(", "a command substitution ($(...))"),
)


def add_content_args(parser, flag: str = "--content", *, required: bool = True) -> None:
    """Add ``FLAG`` and ``FLAG-file`` as a mutually exclusive pair."""
    dest = flag.lstrip("-").replace("-", "_")
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        flag,
        dest=dest,
        default=None,
        help=f"message body; '-' reads stdin. Refused if it contains shell-active "
             f"characters — use {flag}-file for those.",
    )
    group.add_argument(
        f"{flag}-file",
        dest=f"{dest}_file",
        default=None,
        metavar="PATH",
        help="read the body verbatim from PATH (no shell involved)",
    )


def resolve_content(value, file_path, flag: str = "--content"):
    """Return the body from ``FLAG-file``, stdin, or ``FLAG`` — or None if unset.

    File and stdin content is returned exactly as read. Only a ``FLAG`` value is
    guarded, because only that one came through a shell.
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
    for token, name in _SHELL_ACTIVE:
        if token in value:
            raise ContentError(
                f"{flag} contains {name}. Your shell evaluates that before the CLI "
                f"sees it, so part of this body may already have been EXECUTED or "
                f"replaced by an empty string — silently, with the send still "
                f"reporting success (board #458). Refusing to send. Use "
                f"{flag}-file <path>, or {flag} - to read the body from stdin."
            )
    return value
