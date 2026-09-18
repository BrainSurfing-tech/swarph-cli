"""Separate what source CODE does from what source PROSE says, before counting either.

>>> A GREP OVER SOURCE COUNTS WHAT THE SOURCE *SAYS* AS IF IT WERE WHAT THE SOURCE
*DOES*. <<< Three instances on three surfaces in one week (board #874), each producing
a confident wrong number that read as a finding:

    2026-09-16  lab-ovh   `exec:%` "found" in schedule_selectors.py — it was in a DOCSTRING
    2026-09-17  science-claude  #868 audit scored 2 of 5 packs "HAS A CHANNEL" from their
                                own subject matter
    2026-09-18  lab-ovh   a unit-file sweep reported 16 invocations / 9 ambient; five of the
                          nine were COMMENT TEXT, one of them the fragment
                          "status`), not by being typed at."
    2026-09-18  science-claude  pack_stale_resident measured a FastAPI server against
                                swarph_cli's mtime — the only "swarph" in its source is a
                                docstring, a CORS origin and a prompt string

The splitter existed on 2026-09-16, in one function, in one pack, on one box, with one
caller. It was not reached for on the next three surfaces — including by the cell that
wrote it. THE_TEN's preamble says a law that can become a mechanism should stop being a
sentence; this module is that move.

>>> STRIPPING ALL STRING LITERALS IS WRONG AND A SELFTEST CAUGHT IT. <<< A value in real
code IS a string literal — `EXEC_DISPATCHER_PREFIXES = ("exec:", "dm:")` — so a blanket
strip erases exactly the evidence a search exists to find. The distinction that matters is
a string used as a VALUE (evidence) versus a string standing alone as PROSE (none).

A docstring is a STRING token standing alone as a statement: its previous significant token
is NEWLINE/INDENT/DEDENT/start-of-file and its next is NEWLINE. Everything else is an
expression, and stays.
"""
from __future__ import annotations

import io
import os
import tokenize

__all__ = ["code_text", "python_code_text", "hash_comment_code_text", "CANNOT_VERIFY"]

#: Returned as the `parsed` flag when the text could not be separated. NOT False —
#: "I could not look" and "I looked and found nothing" are different answers and a
#: caller that cannot tell them apart will report the second for the first.
CANNOT_VERIFY = None


def python_code_text(text: str):
    """(code_only, parsed) for PYTHON source. Drops comments and docstrings; keeps
    every other string literal.

    `parsed` is CANNOT_VERIFY when the text does not tokenize — never False, and
    `code_only` is "" in that case so a substring test cannot accidentally succeed.
    An unparseable file and a parsed file holding nothing produced the IDENTICAL
    evidence string in the original (science-claude, PASS verdict residual 2026-09-16);
    the flag is what tells them apart.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return "", CANNOT_VERIFY
    sig = [i for i, t in enumerate(toks)
           if t.type not in (tokenize.NL, tokenize.COMMENT)]
    drop = set()
    for pos, i in enumerate(sig):
        t = toks[i]
        if t.type != tokenize.STRING:
            continue
        prev = toks[sig[pos - 1]].type if pos else tokenize.NEWLINE
        nxt = toks[sig[pos + 1]].type if pos + 1 < len(sig) else tokenize.NEWLINE
        if prev in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                    tokenize.ENCODING) and nxt == tokenize.NEWLINE:
            drop.add(i)                       # docstring: prose, not evidence
    return " ".join(t.string for i, t in enumerate(toks)
                    if i not in drop and t.type != tokenize.COMMENT), True


def hash_comment_code_text(text: str):
    """(code_only, parsed) for `#`-COMMENT formats with NO parser — systemd units,
    shell, YAML, TOML, .env, Dockerfile.

    >>> THIS IS THE SURFACE THAT BIT ME AND THE ONE A PYTHON-ONLY LIFT WOULD HAVE LEFT
    UNCOVERED. <<< My 2026-09-18 miscount was over systemd unit files, where there is no
    AST to lean on, and unit files, shell, YAML and TOML are most of what a fleet greps.

    Deliberately NAIVE and deliberately SAID SO: it drops a line from the first
    unquoted-looking `#` onward. It does NOT understand a `#` inside a quoted value
    (`Environment=MSG="a # b"` loses the tail). That is a KNOWN CEILING, not an
    oversight — it fails toward LESS text, so it can only cause a MISS, never a false
    hit, and a miss in a search that already over-reports is the safe direction.
    `parsed` is True because the operation is defined for any text; it is the CEILING,
    not the parse, that is the caveat.
    """
    out = []
    for line in (text or "").splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue                          # whole-line comment
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line.split("#", 1)[0]      # trailing comment, balanced quotes only
        if line.strip():
            out.append(line)
    return "\n".join(out), True


#: Extensions handled by the hash-comment strategy. Anything else is CANNOT_VERIFY
#: rather than guessed — a format we do not know is not a format with no comments.
_HASH_SUFFIXES = (".service", ".timer", ".path", ".socket", ".mount", ".target",
                  ".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".env", ".cfg",
                  ".ini", ".conf")
_HASH_BASENAMES = ("Dockerfile", "Makefile", "crontab")


def code_text(path: str, text: str | None = None):
    """(code_only, full, parsed) for a FILE, strategy chosen by its name.

    Returns CANNOT_VERIFY for `parsed` on a format with no strategy — NOT a fallback
    to raw text. A caller that greps the raw text of an unknown format has made exactly
    the mistake this module exists to prevent, and silently handing it that text would
    be the module participating.
    """
    if text is None:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    base = os.path.basename(path)
    if path.endswith(".py"):
        code, parsed = python_code_text(text)
    elif path.endswith(_HASH_SUFFIXES) or base in _HASH_BASENAMES:
        code, parsed = hash_comment_code_text(text)
    else:
        code, parsed = "", CANNOT_VERIFY
    return code, text, parsed
