"""#397 — resolve a cell's GitHub identity from its mesh identity, or REFUSE.

>>> THE DEFECT THIS EXISTS TO END: GITHUB'S GATES KEY ON THE CREDENTIAL, NOT ON
AUTHORSHIP. <<< PR #214 was authored-of-record by `darw007d` purely because that
credential happened to be active at `gh pr create` time — lab wrote every line. So
lab could have approved its own work under a second login and every gate would have
passed green. THE ROUTER IS WHAT MAKES author-of-record MEAN author-in-fact.

Two rules, each earned by a measured failure:

>>> INJECT ``GH_TOKEN`` PER CALL. NEVER RUN ``gh auth switch``. <<< A switch is
GLOBAL TO THE BOX. Measured 2026-08-11: switching to record one review would have
attributed every later `gh` call from another session — including a MERGE — to the
reviewer, until somebody switched back. Same shared-mutable-substrate shape as the
overwritten commit: one setting, several actors, and the actor cannot see its own
scope.

>>> REFUSE ON AN UNMAPPED CELL. NEVER FALL BACK TO THE AMBIENT LOGIN. <<< A router
that silently uses "whoever is logged in" reproduces the identity-substitution class
one layer out, on a surface with no audit trail of its own — which is exactly how
drop-on-meta-edge ran five days as lab-ovh (#360). The request stays internally
consistent, so no server-side control can fire.

THE MAPPING IS OPERATOR DATA, NOT SHIPPED DATA. A wrong mapping is a silent identity
substitution, so this module ships NO defaults and NO built-in table: an absent or
incomplete mapping is a REFUSAL, never a guess.

#1144 — THE CELL CARRIES ITS OWN LOGIN. A cell's yaml (``github: <login>``) is read
FIRST; ``gh-identities.json`` is the fallback for one release and is then retired. The
commander, 2026-10-14: "you are all suppose to have that identity in your cell yaml".
The json held 2 cells for two months while the rest were refused at their first push
(#1096), days after they were created. A field set at ``swarph init`` puts the gap at
creation instead.

>>> A CELL IS FOUND BY ITS ``name:``, NEVER BY ITS FILENAME. <<< ``lab.yaml`` carries
``name: lab-ovh``, and SWARPH_SELF is the mesh name — the same split ``gh-route doctor``
was fixed for (_cells_on_this_box).

>>> YAML AND JSON DISAGREEING IS A REFUSAL, NOT "YAML WINS". <<< During the fallback
release both can name a login for one cell. Picking either silently is the identity
substitution this module exists to end, one layer up; the operator decides which is
right and deletes the other.

#1347 — KNOWN LIMITS. This is a text matcher, not a sandbox. At the shell-string
depth cap the matcher answers True, so a deeper nest is routed or denied rather
than skipped (#360). These idioms still do not route, on main as well as here,
and #1346 did not introduce them:

- gh after the apostrophe idiom inside a -c string:
  bash -c 'echo it'\\''s done; gh pr view 1'
- ANSI-C quoting: bash -c $'gh pr view 1'
- adjacent quoted pieces: bash -c 'echo 1;'"gh pr view 1"
- a here-string: bash <<< "gh pr view 1"
- a pipe into a shell: echo "gh pr view 1" | bash, and the same with | sh -s
- a backslash command word: \\gh pr list
- $(which gh) pr list
- g""h pr list
- bash -c "$(echo gh) pr view 1"
- a variable: CMD="gh pr view 1"; bash -c "$CMD"
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Where the peer -> github-login table lives. Operator-owned, read-only here.
MAPPING_ENV = "SWARPH_GH_IDENTITY_MAP"
DEFAULT_MAPPING_PATH = Path.home() / ".config" / "swarph" / "gh-identities.json"

#: Wrappers that take a command as their argument, so `gh` is argv[1+] rather than
#: argv[0]. grok-researcher enumerated these against the first version, which
#: matched none of them: `timeout 40 gh pr view 249` is the one it had itself run
#: twice while reviewing these PRs, which is as real-world as a corpus gets.
# An option argument may not start with '-'. The old `(?:\s+\S+)?` also
# matched the next flag, so `sudo` / `xargs` backtracked on a run of flags
# (#4469). `env -u NAME` needs the same argument slot or `env -u GH_TOKEN gh`
# is invisible to targets_gh.
# The argument also may not be a wrapper name. Otherwise `env -u env `,
# `sudo -u sudo `, `xargs -a xargs ` and `sudo -u nohup ` each split two
# ways per repetition and targets_gh is exponential. `sudo -u env gh` still
# matches: `env` is the next wrapper, not sudo's user argument.
# The `(?=\s|$)` end keeps the refusal to a whole word. Without it a longer
# argument that only starts with a wrapper name (`envoy`, `timer`, `execs`,
# `timefile`) is refused and the `gh` after it is invisible.
_WRAP_ARG_NOT_NAME = (
    r"(?:\s+(?!(?:env|sudo|command|exec|nohup|time|nice|timeout|stdbuf"
    r"|rlwrap|xargs)(?=\s|$))[^\s-]\S*)?"
)
_WRAPPERS = (
    rf"env(?:\s+-\w+{_WRAP_ARG_NOT_NAME})*|sudo(?:\s+-\w+{_WRAP_ARG_NOT_NAME})*"
    r"|command(?:\s+-\w+)*|exec|nohup"
    r"|time|nice(?:\s+-n\s*-?\d+)?|timeout\s+\S+|stdbuf(?:\s+-\S+)*|rlwrap"
    rf"|xargs(?:\s+-\S+{_WRAP_ARG_NOT_NAME})*"
)

#: Positions from which a command can start: string start, after a separator, inside
#: a substitution, after a block/subshell opener, or after then/do/else.
#: >>> MULTILINE IS LOAD-BEARING. <<< Without it `^` is string-start only, so
#: `cd /tmp\ngh pr list` missed entirely — and multi-line Bash is the DOMINANT shape
#: in this harness. drop-on-meta-edge measured 11 false negatives in 16 realistic
#: shapes against the first version; that was the biggest single class.
# >>> `\bif\b` AND FRIENDS WERE MISSING, AND drop MEASURED THE COST: 1 OF 11 FALSE
# NEGATIVES SURVIVED THE FIX, AND IT WAS `if gh pr view 249; then`. <<< The list had
# then/do/else — the words that FOLLOW a condition — and not the words that OPEN one.
# A command in the CONDITION of an if/while/until is exactly as much a `gh` call as
# one in its body, and it is a shape people write constantly.
#
# Found by re-running drop's own failing corpus rather than the PR's seven cases:
# "0/7 false positives is measured against your seven cases, not mine, and the claim
# as written is wider than its evidence." The token list was, too.
_START = (r"(?:^|[;&|]|\$\(|`|\(|\{"
          r"|\bthen\b|\bdo\b|\belse\b|\bif\b|\bwhile\b|\buntil\b|\belif\b"
          r"|&&|\|\|)")
_LEAD = r"[ \t]*"                       # leading whitespace/tab — also missed before
_ENVPFX = r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\$\([^)]*\)|(?!\$\()\S*)[ \t]+)*"
_WRAPPFX = rf"(?:(?:{_WRAPPERS})[ \t]+)*"
#: `gh`, `./gh`, or any absolute path ending in /gh.
_GH_WORD = r"(?:\.{0,2}/|(?:/[\w.-]+)*/)?gh"

_GH_INVOCATION = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}{_GH_WORD}(?=[ \t]|$)", re.MULTILINE)

#: Already carries an explicit GH_TOKEN — an operator decision; do not double-inject.
#: Card #1476 narrows this to position, not text presence (see already_explicit).


class RouterRefusal(Exception):
    """Raised instead of falling back. Carries the operator-facing reason."""


@dataclass(frozen=True)
class Resolution:
    peer: str
    login: str
    source: str          # where the mapping came from, for the audit line


def mapping_path() -> Path:
    override = os.environ.get(MAPPING_ENV, "").strip()
    return Path(override) if override else DEFAULT_MAPPING_PATH


def load_mapping(path: Optional[Path] = None) -> dict:
    """Read the peer -> login table. A MISSING file is not an empty mapping — it is
    an unconfigured router, and the two must not collapse into the same behaviour
    (one is 'nobody is mapped yet', the other is 'this peer specifically is not')."""
    p = path or mapping_path()
    if not p.exists():
        raise RouterRefusal(
            f"no GitHub identity mapping at {p}.\n"
            f"  The router refuses rather than using whichever `gh` account happens\n"
            f"  to be active — an ambient identity is how a cell runs for days as\n"
            f"  another cell (#360) with every request internally consistent.\n"
            f"  Create it:  {{\"<peer-name>\": \"<github-login>\", ...}}\n"
            f"  Or point {MAPPING_ENV} at an existing file."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RouterRefusal(f"GitHub identity mapping at {p} is unreadable: {exc}. "
                            f"Fix it, or delete it if every cell's yaml carries '{YAML_KEY}:'.")
    if not isinstance(data, dict):
        raise RouterRefusal(f"GitHub identity mapping at {p} must be a JSON object")
    return data


#: The cell-yaml key that names the cell's GitHub login (#1144).
YAML_KEY = "github"

#: GitHub's login grammar. The login is spliced into the hook's shell command, so a
#: value outside it is refused, never quoted-and-hoped (#1144 security review).
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


def _checked(peer: str, login: str, source) -> "Resolution":
    if not LOGIN_RE.fullmatch(login):
        raise RouterRefusal(
            f"{source}: GitHub login for {peer!r} is not a valid GitHub login: {login!r}")
    return Resolution(peer=peer, login=login, source=str(source))


def _cells_dir() -> Path:
    from swarph_cli.cell import cells_dir  # local: cell.py is the one owner of the path
    return cells_dir()


def yaml_claims(peer: str, directory: Optional[Path] = None, key: str = YAML_KEY):
    """Every cell yaml that declares mesh name ``peer`` -> ``([(value, path)], unreadable)``.

    ``key`` names the yaml key to read (default ``github``; stall_alert reads
    ``responsible``, card #1135) so there is ONE yaml reader, not two.
    ``value`` is the raw value (None when the file has no such key), so the
    caller can tell "no yaml for this cell" from "a yaml that maps nothing". A file with
    no ``name:`` is the cell named by its stem — the same rule as _cells_on_this_box.
    ``unreadable`` lists files that could not be parsed: they may be this cell, so a
    refusal names them rather than reading as "not mapped".
    """
    import yaml  # local import — keeps `swarph --version` PyYAML-free (cell.py:254)

    d = directory or _cells_dir()
    claims, unreadable = [], []
    try:
        files = sorted(d.glob("*.yaml"))
    except OSError:
        return claims, unreadable
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            # Not just OSError/YAMLError: a UTF-16 file (a PowerShell 5.1 redirect) raises
            # UnicodeDecodeError and deep nesting raises RecursionError. Either escaping
            # here crashed the hook, and a crashed hook lets `gh` run as the active
            # account (#360) for every cell on the box (drop-on-meta-edge, #1818).
            unreadable.append(f)
            continue
        if not isinstance(doc, dict):
            unreadable.append(f)
            continue
        name = doc.get("name")
        if (name if isinstance(name, str) and name else f.stem) == peer:
            claims.append((doc.get(key), f))
    return claims, unreadable


def _optional_mapping(path: Optional[Path]) -> Optional[dict]:
    """The json table, or None when the file is ABSENT. Absent is now a normal state
    (the yaml carries the login); an UNREADABLE file still refuses via load_mapping."""
    p = path or mapping_path()
    if not p.exists():
        return None
    return load_mapping(p)


def resolve(peer: Optional[str], path: Optional[Path] = None,
            cells: Optional[Path] = None) -> Resolution:
    """peer -> Resolution, or RouterRefusal. Never returns a fallback.

    Order (#1144): the cell's yaml ``github:``, then gh-identities.json. Both present
    and different is a refusal.
    """
    if not peer:
        raise RouterRefusal(
            "SWARPH_SELF is unset, so this cell has no mesh identity to route from.\n"
            "  >>> THE ROUTER WILL NOT GUESS A CELL NAME. <<< Guessing makes a cell\n"
            "  act as whichever identity a default happened to name — measured on\n"
            "  6 of 6 cells (#243) and the direct cause of #360.\n"
            "  Set SWARPH_SELF, or pass GH_TOKEN=... explicitly for a one-off."
        )
    claims, unreadable = yaml_claims(peer, cells)
    mapped = [(v, f) for v, f in claims if v is not None]
    for v, f in mapped:
        if not isinstance(v, str) or not v.strip():
            raise RouterRefusal(
                f"{f}: '{YAML_KEY}:' for {peer!r} must be a non-empty string, "
                f"got {v!r}")
    yaml_logins = sorted({v.strip() for v, _ in mapped})
    if len(yaml_logins) > 1:
        # Two configs claiming one mesh name with different logins: either could be
        # the live one, and picking is a guess.
        raise RouterRefusal(
            f"cell {peer!r} is claimed by {len(mapped)} cell yamls with different "
            f"GitHub logins:\n"
            + "".join(f"    {f}: {YAML_KEY}: {v}\n" for v, f in mapped)
            + "  >>> REFUSING RATHER THAN PICKING ONE. <<< Keep one config per cell.")

    p = path or mapping_path()
    if yaml_logins:
        table = _optional_mapping(path) or {}
        jl = table.get(peer)
        if jl is not None and jl != yaml_logins[0]:
            raise RouterRefusal(
                f"cell {peer!r} has two GitHub identities that disagree:\n"
                f"    {mapped[0][1]}: {YAML_KEY}: {yaml_logins[0]}\n"
                f"    {p}: {jl!r}\n"
                f"  >>> REFUSING RATHER THAN LETTING EITHER WIN. <<< The yaml is the\n"
                f"  source of truth from #1144 on: fix whichever is wrong, then delete\n"
                f"  {peer!r} from {p.name}.")
        return _checked(peer, yaml_logins[0], mapped[0][1])

    table = _optional_mapping(path)
    if table is None:
        raise RouterRefusal(
            f"cell {peer!r} has no '{YAML_KEY}:' in its cell yaml under {cells or _cells_dir()},\n"
            f"  and there is no GitHub identity mapping at {p} either.\n"
            f"  The router refuses rather than using whichever `gh` account happens\n"
            f"  to be active — an ambient identity is how a cell runs for days as\n"
            f"  another cell (#360) with every request internally consistent.\n"
            f"  Add `{YAML_KEY}: <github-login>` to the cell's yaml"
            f" (`swarph init --github` writes it).\n"
            + _unreadable_note(unreadable))
    login = table.get(peer)
    if not login:
        raise RouterRefusal(
            f"cell {peer!r} has no GitHub identity: no '{YAML_KEY}:' in its cell yaml"
            f" under {cells or _cells_dir()}, and none in {p}.\n"
            f"  Known cells in {p.name}: {', '.join(sorted(table)) or '(none)'}\n"
            f"  >>> REFUSING RATHER THAN USING THE ACTIVE ACCOUNT. <<< Falling back\n"
            f"  would attribute {peer}'s action to whoever is logged in, and GitHub's\n"
            f"  own gates key on the CREDENTIAL, not on who did the work.\n"
            f"  Add `{YAML_KEY}: <github-login>` to the cell's yaml, or pass GH_TOKEN=... explicitly.\n"
            + _unreadable_note(unreadable))
    if not isinstance(login, str):
        raise RouterRefusal(f"mapping for {peer!r} must be a string, got {type(login).__name__}")
    return _checked(peer, login, p)


def _unreadable_note(unreadable) -> str:
    if not unreadable:
        return ""
    return ("  NOT READ (may be this cell's config): "
            + ", ".join(str(f) for f in unreadable) + "\n")


_HEREDOC_OP = re.compile(r"<<(-)?(?!<)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_ASSIGN_NAME_AT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

#: Module-level switches so mutant tests can flip comment masking off or
#: fold backslash-LF continuations inside comments and verify each fails.
_MASK_COMMENTS = True
_FOLD_CONTINUATIONS_IN_COMMENTS = False


def _blanks(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def _is_terminator(line: str, delim: str, strip_tabs: bool) -> bool:
    """Exact heredoc terminator: only leading tabs count, and only for ``<<-``."""
    if strip_tabs:
        return line.lstrip("\t") == delim
    return line == delim


def _subst_end(command: str, open_paren: int) -> int:
    """Index of the ``)`` matching ``command[open_paren] == '('``."""
    depth = 1
    i = open_paren + 1
    n = len(command)
    while i < n and depth:
        c = command[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            j = command.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i = i + 1 if i < n else n
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _subst_end_hd(command: str, open_paren: int, *,
                  mask_comments: Optional[bool] = None) -> int:
    """Heredoc- and comment-aware ``_subst_end``: body lines are raw text.

    A ``)`` or quote char inside a heredoc body or a word-start ``#``
    comment neither closes the substitution nor opens a quote. ``<<<`` is a
    here-string, not a heredoc: a ``<<`` whose previous char is ``<`` never
    registers.
    """
    mc = _MASK_COMMENTS if mask_comments is None else mask_comments
    depth = 1
    brace_depth = 0
    i = open_paren + 1
    n = len(command)
    at_word_start = True
    pending: list[tuple[str, bool]] = []
    while i < n and depth:
        c = command[i]
        if c == "\n":
            i += 1
            at_word_start = True
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    nl = command.find("\n", i)
                    lend = n if nl < 0 else nl
                    if _is_terminator(command[i:lend], delim, strip_tabs):
                        i = lend + 1 if nl >= 0 else n
                        break
                    if nl < 0:
                        i = n
                        break
                    i = nl + 1
            continue
        if mc and at_word_start and brace_depth == 0 and c == "#":
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            at_word_start = False
            continue
        if command.startswith("${", i):
            brace_depth += 1
            i += 2
            at_word_start = False
            continue
        if c == "}" and brace_depth > 0:
            brace_depth -= 1
            i += 1
            at_word_start = False
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "'":
            at_word_start = False
            j = i + 2
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if command[j] == "'":
                    break
                j += 1
            i = j + 1 if j < n else n
            continue
        if c == "'":
            at_word_start = False
            j = command.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            at_word_start = False
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i = i + 1 if i < n else n
            continue
        if c == "<" and not (i > 0 and command[i - 1] == "<"):
            if command.startswith("<<<", i):
                i += 3
                at_word_start = True
                continue
            m = _HEREDOC_OP.match(command, i)
            if m is not None:
                pending.append((m.group(3), bool(m.group(1))))
                i = m.end()
                at_word_start = False
                continue
            i += 1
            at_word_start = True
            continue
        if c in " \t":
            i += 1
            at_word_start = True
            continue
        if c in ";|&>":
            i += 1
            at_word_start = True
            continue
        if c == "(":
            depth += 1
            at_word_start = True
        elif c == ")":
            depth -= 1
            at_word_start = True
            if depth == 0:
                return i
        else:
            at_word_start = False
        i += 1
    return n


@dataclass(frozen=True)
class BashScan:
    """Result of the shared bash tokenizer (:func:`_tokenize_bash`)."""
    folded: str
    visible: str
    targets_visible: str
    unterminated_quote: bool
    has_backtick: bool
    has_heredoc: bool
    subst_spans: tuple[tuple[int, int], ...]


def _fold_bash_continuations(
    command: str,
    *,
    mask_comments: bool,
    fold_continuations_in_comments: bool,
) -> str:
    """Fold ``\\<LF>`` as bash does: outside comments, single quotes, and heredocs.

    In bash:
    - A ``#`` starts a comment ONLY at a word start, and the comment ends at
      ``\\n``; a ``\\<LF>`` inside a comment does NOT continue the comment
      onto the next line.
    - Inside ``'...'`` and ``$'...'``, ``\\`` does not fold a newline.
    - Outside single quotes, comments, and heredoc bodies (including inside
      ``"..."`` and ``$(...)``), ``\\<LF>`` is a line continuation and is
      removed without changing word-start state. ``\\<CR><LF>`` is not a
      continuation and is kept in place.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    at_word_start = True
    brace_depth = 0
    pending: list[tuple[str, bool]] = []
    while i < n:
        c = command[i]
        if c == "\n":
            out.append("\n")
            i += 1
            at_word_start = True
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    nl = command.find("\n", i)
                    lend = n if nl < 0 else nl
                    line = command[i:lend]
                    out.append(line)
                    if nl >= 0:
                        out.append("\n")
                        i = lend + 1
                    else:
                        i = n
                    if _is_terminator(line, delim, strip_tabs):
                        break
            continue
        if mask_comments and at_word_start and brace_depth == 0 and c == "#":
            if not fold_continuations_in_comments:
                end = command.find("\n", i)
                end = n if end < 0 else end
                out.append(command[i:end])
                i = end
                continue
            j = i
            while j < n:
                if command[j] == "\\" and j + 1 < n and command[j + 1] == "\n":
                    j += 2
                    continue
                if command[j] == "\n":
                    break
                out.append(command[j])
                j += 1
            i = j
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2
                continue
            out.append(command[i:i + 2])
            i += 2
            at_word_start = False
            continue
        if command.startswith("${", i):
            brace_depth += 1
            out.append("${")
            i += 2
            at_word_start = False
            continue
        if c == "}" and brace_depth > 0:
            brace_depth -= 1
            out.append("}")
            i += 1
            at_word_start = False
            continue
        if c == "<":
            if command.startswith("<<<", i):
                out.append("<<<")
                i += 3
                at_word_start = True
                continue
            m = None if (i > 0 and command[i - 1] == "<") \
                else _HEREDOC_OP.match(command, i)
            if m is not None:
                pending.append((m.group(3), bool(m.group(1))))
                out.append(command[i:m.end()])
                i = m.end()
                at_word_start = False
                continue
            out.append(c)
            i += 1
            at_word_start = True
            continue
        if c in " \t":
            out.append(c)
            i += 1
            at_word_start = True
            continue
        if c in ";|&()>":
            out.append(c)
            i += 1
            at_word_start = True
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "'":
            at_word_start = False
            j = i + 2
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if command[j] == "'":
                    j += 1
                    break
                j += 1
            out.append(command[i:j])
            i = j
            continue
        if c == "'":
            at_word_start = False
            j = command.find("'", i + 1)
            end = n if j < 0 else j + 1
            out.append(command[i:end])
            i = end
            continue
        if c == '"' or (c == "$" and i + 1 < n and command[i + 1] == '"'):
            at_word_start = False
            if c == "$":
                out.append('$"')
                i += 2
            else:
                out.append('"')
                i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    if command[i + 1] == "\n":
                        i += 2
                        continue
                    out.append(command[i:i + 2])
                    i += 2
                    continue
                if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
                    end = _subst_end_hd(command, i + 1, mask_comments=mask_comments)
                    inner = _fold_bash_continuations(
                        command[i + 2:end],
                        mask_comments=mask_comments,
                        fold_continuations_in_comments=fold_continuations_in_comments,
                    )
                    out.append("$(" + inner + (")" if end < n else ""))
                    i = end + 1 if end < n else n
                    continue
                out.append(command[i])
                i += 1
            if i < n and command[i] == '"':
                out.append('"')
                i += 1
            continue
        out.append(c)
        i += 1
        at_word_start = False
    return "".join(out)


def _closed_assignment_value_end(text: str, i: int, *,
                                 mask_comments: bool) -> Optional[int]:
    """End of a closed assignment value word starting at ``i`` in ``text``.

    Handles concatenated bare chars, ``'...'``, ``$'...'``, ``"..."``,
    ``$"..."``, ``$(...)``, ``$((...))``, ``${...}``, and backticks `` `...` ``.
    Returns ``None`` if any quote or substitution in the value is unclosed.
    """
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t;\n&|()<>":
            break
        if text.startswith("$(", i):
            end = _subst_end_hd(text, i + 1, mask_comments=mask_comments)
            if end >= n:
                return None
            i = end + 1
            continue
        if text.startswith("${", i):
            end = text.find("}", i + 2)
            if end < 0:
                return None
            i = end + 1
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                return None
            i = j + 1
            continue
        if text.startswith("$'", i):
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            if j >= n:
                return None
            i = j + 1
            continue
        if text.startswith('$"', i):
            j = i + 2
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j >= n:
                return None
            i = j + 1
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j >= n:
                return None
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return i


def _normalize_assignment_values(folded: str, visible: str, *,
                                 mask_comments: bool) -> str:
    """Collapse closed env-prefix values (``$(...)``, ``$((...))``, `` `...` ``,
    quotes) into ``x`` runs of identical length so ``_ENVPFX`` never hides a
    following ``gh`` call, while preserving any inner ``gh`` invocation.
    """
    if "=" not in visible:
        return visible
    chars = list(visible)
    n = len(visible)
    i = 0
    while i < n:
        if i == 0 or visible[i - 1] in " \t\n;|&({":
            am = _ASSIGN_NAME_AT.match(visible, i)
            if am is not None:
                vstart = am.end()
                vend = _closed_assignment_value_end(
                    folded, vstart, mask_comments=mask_comments)
                if vend is not None and vend > vstart:
                    span_vis = "".join(chars[vstart:vend])
                    if not _GH_INVOCATION.search(span_vis):
                        for k in range(vstart, vend):
                            chars[k] = "x"
                    i = vend
                    continue
        i += 1
    return "".join(chars)


def _scan_subst_only_for_targets(segment: str, *, mask_comments: bool) -> str:
    """Blank ``segment`` while preserving any ``$(...)`` or `` `...` `` inside it."""
    if "$(" not in segment and "`" not in segment:
        return _blanks(segment)
    res: list[str] = []
    k = 0
    m = len(segment)
    while k < m:
        if segment[k] == "\\" and k + 1 < m:
            res.append("  ")
            k += 2
            continue
        if segment.startswith("$(", k):
            end = _subst_end_hd(segment, k + 1, mask_comments=mask_comments)
            stop = end + 1 if end < m else m
            sub = _scan_folded(segment[k + 2:end], 0, mask_comments=mask_comments)
            close_p = ")" if end < m else ""
            res.append("$(" + sub.targets_visible + close_p)
            k = stop
            continue
        if segment[k] == "`":
            j = segment.find("`", k + 1)
            if j < 0:
                sub = _scan_folded(segment[k + 1:], 0, mask_comments=mask_comments)
                res.append("`" + sub.targets_visible)
                k = m
                continue
            sub = _scan_folded(segment[k + 1:j], 0, mask_comments=mask_comments)
            res.append("`" + sub.targets_visible + "`")
            k = j + 1
            continue
        res.append("\n" if segment[k] == "\n" else " ")
        k += 1
    return "".join(res)


def _scan_folded(text: str, base: int = 0, *, mask_comments: bool = True) -> BashScan:
    """Single-pass bash scanner on continuation-folded text."""
    out: list[str] = []
    out_targets: list[str] = []
    i = 0
    n = len(text)
    at_word_start = True
    brace_depth = 0
    pending: list[tuple[str, bool, bool]] = []
    unterminated = False
    has_tick = False
    has_hd = False
    spans: list[tuple[int, int]] = []

    while i < n:
        c = text[i]
        if c == "\n":
            out.append("\n")
            out_targets.append("\n")
            i += 1
            at_word_start = True
            while pending:
                delim, strip_tabs, quoted_delim = pending.pop(0)
                while i < n:
                    nl = text.find("\n", i)
                    lend = n if nl < 0 else nl
                    line = text[i:lend]
                    if _is_terminator(line, delim, strip_tabs):
                        out.append(line)
                        out_targets.append(line)
                        if nl >= 0:
                            out.append("\n")
                            out_targets.append("\n")
                            i = lend + 1
                        else:
                            i = n
                        break
                    blank_line = _blanks(line)
                    out.append(blank_line)
                    if quoted_delim:
                        out_targets.append(blank_line)
                    else:
                        out_targets.append(
                            _scan_subst_only_for_targets(line, mask_comments=mask_comments)
                        )
                    if nl < 0:
                        i = n
                        break
                    out.append("\n")
                    out_targets.append("\n")
                    i = nl + 1
            continue
        if mask_comments and at_word_start and brace_depth == 0 and c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            seg = text[i:end]
            out.append(_blanks(seg))
            out_targets.append(
                _scan_subst_only_for_targets(seg, mask_comments=mask_comments)
            )
            i = end
            continue
        if c == "\\" and i + 1 < n:
            esc = text[i:i + 2]
            out.append(esc)
            out_targets.append(esc)
            i += 2
            at_word_start = False
            continue
        if text.startswith("${", i):
            brace_depth += 1
            out.append("${")
            out_targets.append("${")
            i += 2
            at_word_start = False
            continue
        if c == "}" and brace_depth > 0:
            brace_depth -= 1
            out.append("}")
            out_targets.append("}")
            i += 1
            at_word_start = False
            continue
        if c == "<":
            if text.startswith("<<<", i):
                has_hd = True
                out.append("<<<")
                out_targets.append("<<<")
                i += 3
                at_word_start = True
                continue
            m = None if (i > 0 and text[i - 1] == "<") \
                else _HEREDOC_OP.match(text, i)
            if m is not None:
                has_hd = True
                pending.append((m.group(3), bool(m.group(1)), bool(m.group(2))))
                op_text = text[i:m.end()]
                out.append(op_text)
                out_targets.append(op_text)
                i = m.end()
                at_word_start = False
                continue
            out.append(c)
            out_targets.append(c)
            i += 1
            at_word_start = True
            continue
        if c in " \t":
            out.append(c)
            out_targets.append(c)
            i += 1
            at_word_start = True
            continue
        if c in ";|&()>":
            out.append(c)
            out_targets.append(c)
            i += 1
            at_word_start = True
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            at_word_start = False
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            if j >= n:
                unterminated = True
                out.append(_blanks(text[i:]))
                out_targets.append(text[i:])
                i = n
                continue
            masked_q = "$'" + _blanks(text[i + 2:j]) + "'"
            out.append(masked_q)
            out_targets.append(masked_q)
            i = j + 1
            continue
        if c == "'":
            at_word_start = False
            j = text.find("'", i + 1)
            if j < 0:
                unterminated = True
                out.append(_blanks(text[i:]))
                out_targets.append(text[i:])
                i = n
                continue
            masked_q = "'" + _blanks(text[i + 1:j]) + "'"
            out.append(masked_q)
            out_targets.append(masked_q)
            i = j + 1
            continue
        if c == '"' or (c == "$" and i + 1 < n and text[i + 1] == '"'):
            at_word_start = False
            quote_start, output_start = i, len(out)
            output_targets_start = len(out_targets)
            subst_seen = False
            if c == "$":
                out.append('$"')
                out_targets.append('$"')
                i += 2
            else:
                out.append('"')
                out_targets.append('"')
                i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    out_targets.append("  ")
                    i += 2
                    continue
                if text[i] == "$" and i + 1 < n and text[i + 1] == "(":
                    subst_seen = True
                    end = _subst_end_hd(text, i + 1, mask_comments=mask_comments)
                    stop = end + 1 if end < n else n
                    spans.append((base + i, base + stop))
                    sub = _scan_folded(
                        text[i + 2:end], base + i + 2, mask_comments=mask_comments)
                    unterminated |= sub.unterminated_quote
                    has_tick |= sub.has_backtick
                    has_hd |= sub.has_heredoc
                    spans.extend(sub.subst_spans)
                    close_p = ")" if end < n else ""
                    out.append("$(" + sub.visible + close_p)
                    out_targets.append("$(" + sub.targets_visible + close_p)
                    i = stop
                    continue
                if text[i] == "`":
                    subst_seen = True
                    has_tick = True
                    j = text.find("`", i + 1)
                    if j < 0:
                        unterminated = True
                        tail = _blanks(text[i:])
                        out.append(tail)
                        out_targets.append(tail)
                        i = n
                        break
                    sub = _scan_folded(
                        text[i + 1:j], base + i + 1, mask_comments=mask_comments)
                    unterminated |= sub.unterminated_quote
                    has_hd |= sub.has_heredoc
                    spans.extend(sub.subst_spans)
                    out.append("`" + sub.visible + "`")
                    out_targets.append("`" + sub.targets_visible + "`")
                    i = j + 1
                    continue
                ch_blank = "\n" if text[i] == "\n" else " "
                out.append(ch_blank)
                out_targets.append(ch_blank)
                i += 1
            if i < n and text[i] == '"':
                out.append('"')
                out_targets.append('"')
                i += 1
            else:
                unterminated = True
                tail = _blanks(text[quote_start:])
                out[output_start:] = [tail]
                if not subst_seen:
                    out_targets[output_targets_start:] = [text[quote_start:]]
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            at_word_start = False
            end = _subst_end_hd(text, i + 1, mask_comments=mask_comments)
            stop = end + 1 if end < n else n
            spans.append((base + i, base + stop))
            sub = _scan_folded(
                text[i + 2:end], base + i + 2, mask_comments=mask_comments)
            unterminated |= sub.unterminated_quote
            has_tick |= sub.has_backtick
            has_hd |= sub.has_heredoc
            spans.extend(sub.subst_spans)
            close_p = ")" if end < n else ""
            out.append("$(" + sub.visible + close_p)
            out_targets.append("$(" + sub.targets_visible + close_p)
            i = stop
            continue
        if c == "`":
            at_word_start = False
            has_tick = True
            j = text.find("`", i + 1)
            if j < 0:
                sub = _scan_folded(
                    text[i + 1:], base + i + 1, mask_comments=mask_comments)
                unterminated |= sub.unterminated_quote
                has_hd |= sub.has_heredoc
                spans.extend(sub.subst_spans)
                out.append("`" + sub.visible)
                out_targets.append("`" + sub.targets_visible)
                i = n
                continue
            sub = _scan_folded(
                text[i + 1:j], base + i + 1, mask_comments=mask_comments)
            unterminated |= sub.unterminated_quote
            has_hd |= sub.has_heredoc
            spans.extend(sub.subst_spans)
            out.append("`" + sub.visible + "`")
            out_targets.append("`" + sub.targets_visible + "`")
            i = j + 1
            continue
        out.append(c)
        out_targets.append(c)
        i += 1
        at_word_start = False

    visible = "".join(out)
    targets_visible = _normalize_assignment_values(
        text, "".join(out_targets), mask_comments=mask_comments)
    return BashScan(
        folded=text,
        visible=visible,
        targets_visible=targets_visible,
        unterminated_quote=unterminated,
        has_backtick=has_tick,
        has_heredoc=has_hd,
        subst_spans=tuple(spans),
    )


def _tokenize_bash(
    command: str,
    *,
    mask_comments: Optional[bool] = None,
    fold_continuations_in_comments: Optional[bool] = None,
) -> BashScan:
    """The one shared bash tokenizer for ``targets_gh``, ``already_explicit``
    and ``_strip_allowlisted_gh_token`` (card #1555).

    - ``#`` starts a comment ONLY at a word start and the comment ends at ``\\n``.
    - ``\\<LF>`` is folded as bash does: outside comments, single quotes, and
      heredoc bodies; a ``\\<LF>`` inside a comment does NOT continue the line.
    - Heredoc bodies are masked at each newline so quotes inside them never pair
      with later syntax.
    - ``$(...)``, backticks `` `...` ``, and arithmetic ``$((...))`` inside an
      env-prefix value are normalized in ``targets_visible`` so they never hide
      a following ``gh`` call.
    """
    mc = _MASK_COMMENTS if mask_comments is None else mask_comments
    fcc = (_FOLD_CONTINUATIONS_IN_COMMENTS
           if fold_continuations_in_comments is None
           else fold_continuations_in_comments)
    folded = _fold_bash_continuations(
        command or "",
        mask_comments=mc,
        fold_continuations_in_comments=fcc,
    )
    return _scan_folded(folded, 0, mask_comments=mc)


def _mask_quoted(command: str, *, mask_comments: Optional[bool] = None) -> str:
    """Mask quotes, heredocs, and word-start comments via :func:`_tokenize_bash`."""
    return _tokenize_bash(command, mask_comments=mask_comments).visible


def _mask_explicit(command: str) -> str:
    """Mask ``command`` via :func:`_tokenize_bash`."""
    return _tokenize_bash(command).visible


# Card #1346. A shell-executing string is command text, not a mention of gh.
# The depth cap bounds the walk. At the cap the answer is True (#1347): a False
# there is a bypass, and a false positive only denies an unmapped cell. Quote
# masking stays card #1340's: a quoted mention is still not a command word.
_MAX_SHELL_DEPTH = 32
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_GH_EXACT = re.compile(rf"{_GH_WORD}\Z")
_AT_WORD = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}\Z", re.MULTILINE)
_SHELL_BIN = r"(?:(?:/[\w.-]+)*/)?(?:bash|zsh|dash|sh)"
_SHELL_AT = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}(?P<bin>{_SHELL_BIN})(?=[ \t]|$)",
    re.MULTILINE)
_EVAL_AT = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}(?:(?:/[\w.-]+)*/)?eval(?=[ \t]|$)",
    re.MULTILINE)
_WATCH_AT = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}(?:(?:/[\w.-]+)*/)?watch(?=[ \t]|$)",
    re.MULTILINE)
_XARGS_AT = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}(?:(?:/[\w.-]+)*/)?xargs(?=[ \t]|$)",
    re.MULTILINE)
_BASH_TAKES_ARG = frozenset({"-o", "--rcfile", "--init-file"})
_WATCH_TAKES_ARG = frozenset({"-n", "--interval", "-e", "--errexit"})
_XARGS_TAKES_ARG = frozenset({
    "-I", "-i", "-n", "-P", "-a", "-d", "-E", "-L", "-s", "-J",
    "--replace", "--max-args", "--max-procs", "--arg-file", "--delimiter",
    "--eof", "--max-lines", "--max-chars",
})


def _quoted_body(command: str, i: int) -> tuple[str, int]:
    """Unquoted text of the quote at ``i``, and the index just after it.

    An unterminated quote runs to the end and still returns. It does not raise.
    """
    q = command[i]
    n = len(command)
    j = i + 1
    if q == "'":
        k = command.find("'", j)
        if k < 0:
            return command[j:], n
        return command[j:k], k + 1
    buf: list[str] = []
    while j < n and command[j] != '"':
        if command[j] == "\\" and j + 1 < n:
            buf.append(command[j + 1])
            j += 2
            continue
        buf.append(command[j])
        j += 1
    if j >= n:
        return "".join(buf), n
    return "".join(buf), j + 1


def _next_token(command: str, i: int) -> Optional[tuple]:
    n = len(command)
    while i < n and command[i] in " \t":
        i += 1
    if i >= n:
        return None
    if _MASK_COMMENTS and command[i] == "#":
        end = command.find("\n", i)
        end = n if end < 0 else end + 1
        return ("sep", i, end, "")
    if command[i] in ";\n&|":
        return ("sep", i, i + 1, "")
    if command[i] in "'\"":
        body, end = _quoted_body(command, i)
        return ("word", i, end, body)
    buf: list[str] = []
    j = i
    while j < n and command[j] not in " \t;\n&|":
        if command[j] in "'\"":
            break
        if command[j] == "\\" and j + 1 < n:
            buf.append(command[j + 1])
            j += 2
            continue
        buf.append(command[j])
        j += 1
    if j == i:
        return None
    return ("word", i, j, "".join(buf))


def _is_shell_word(word: str) -> bool:
    return word.rsplit("/", 1)[-1] in _SHELLS


def _c_argument(command: str, i: int) -> Optional[str]:
    """The command string of ``shell -c``, or None when this is ``shell script``."""
    while True:
        tok = _next_token(command, i)
        if tok is None or tok[0] == "sep":
            return None
        word = tok[3]
        i = tok[2]
        if word == "--":
            continue
        if word.startswith("-"):
            short = not word.startswith("--")
            if word == "-c" or (short and "c" in word[1:]):
                nxt = _next_token(command, i)
                if nxt is None or nxt[0] != "word":
                    return None
                return nxt[3]
            base = word.split("=", 1)[0]
            if base in _BASH_TAKES_ARG and "=" not in word:
                nxt = _next_token(command, i)
                if nxt is not None and nxt[0] == "word":
                    i = nxt[2]
            continue
        return None


def _joined_words(command: str, i: int) -> Optional[str]:
    parts: list[str] = []
    while True:
        tok = _next_token(command, i)
        if tok is None or tok[0] == "sep":
            break
        if not parts and tok[3] == "--":
            i = tok[2]
            continue
        parts.append(tok[3])
        i = tok[2]
    if not parts:
        return None
    return " ".join(parts)


def _watch_command(command: str, i: int) -> Optional[str]:
    parts: list[str] = []
    while True:
        tok = _next_token(command, i)
        if tok is None or tok[0] == "sep":
            break
        word = tok[3]
        i = tok[2]
        if not parts and word.startswith("-"):
            base = word.split("=", 1)[0]
            attached_n = (
                word.startswith("-n") and not word.startswith("--") and len(word) > 2
            )
            if base in _WATCH_TAKES_ARG and "=" not in word and not attached_n:
                nxt = _next_token(command, i)
                if nxt is not None and nxt[0] == "word":
                    i = nxt[2]
            continue
        parts.append(word)
    if not parts:
        return None
    return " ".join(parts)


def _xargs_shell(command: str, i: int) -> Optional[str]:
    while True:
        tok = _next_token(command, i)
        if tok is None or tok[0] == "sep":
            return None
        word = tok[3]
        i = tok[2]
        if word.startswith("-"):
            base = word.split("=", 1)[0]
            if base in _XARGS_TAKES_ARG and "=" not in word:
                nxt = _next_token(command, i)
                if nxt is not None and nxt[0] == "word":
                    i = nxt[2]
            continue
        if _is_shell_word(word):
            return _c_argument(command, i)
        return None


def _shell_texts(command: str, visible: str) -> list[str]:
    """String arguments of the shell forms card #1346 names, outside quotes."""
    found: list[str] = []
    for match in _SHELL_AT.finditer(visible):
        inner = _c_argument(command, match.end())
        if inner:
            found.append(inner)
    for match in _EVAL_AT.finditer(visible):
        inner = _joined_words(command, match.end())
        if inner:
            found.append(inner)
    for match in _WATCH_AT.finditer(visible):
        inner = _watch_command(command, match.end())
        if inner:
            found.append(inner)
    for match in _XARGS_AT.finditer(visible):
        inner = _xargs_shell(command, match.end())
        if inner:
            found.append(inner)
    return found


def _quoted_gh_command_word(command: str, visible: str) -> bool:
    """``"gh" pr view`` and ``'gh' pr list``: the command word is gh once unquoted."""
    i = 0
    n = len(command)
    while i < n:
        if command[i] not in "'\"":
            i += 1
            continue
        body, end = _quoted_body(command, i)
        if _GH_EXACT.match(body) and _AT_WORD.search(visible[:i]) is not None:
            return True
        i = end if end > i else i + 1
    return False


def targets_gh(command: str) -> bool:
    """True when the command actually invokes `gh`, not when a quote or comment mentions it."""
    return _targets_gh(command or "", 0)


def _targets_gh(command: str, depth: int) -> bool:
    scan = _tokenize_bash(command)
    if _GH_INVOCATION.search(scan.visible) or _GH_INVOCATION.search(scan.targets_visible):
        return True
    if depth >= _MAX_SHELL_DEPTH:
        return True
    if _quoted_gh_command_word(scan.folded, scan.targets_visible):
        return True
    for inner in _shell_texts(scan.folded, scan.targets_visible):
        if inner != command and _targets_gh(inner, depth + 1):
            return True
    return False


_PLAIN_WORD = r"[A-Za-z0-9._-]+"
_GH_AUTH_TOKEN_INNER = re.compile(
    rf"^(?:(?:/[\w.-]+)*/)?gh[ \t]+auth[ \t]+token"
    rf"(?:[ \t]+--user[ \t]+{_PLAIN_WORD}"
    rf"|[ \t]+--hostname[ \t]+{_PLAIN_WORD})*$")

#: Exact r9 occurrence: optional ``export``, then
#: ``GH_TOKEN=$(gh auth token [--user W] [--hostname W])``, ended by
#: whitespace, ``;``, ``&``, ``|``, a newline, or end of text. W is a plain
#: word. Either flag order, any count. Spaces or tabs (never a newline) may
#: sit after ``$(`` and before ``)``. The left boundary and the end
#: lookahead are load-bearing: ``XGH_TOKEN=`` and ``...)x`` are not stripped.
_ALLOWLISTED_GH_TOKEN_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])export[ \t]+)?"
    r"(?<![A-Za-z0-9_])GH_TOKEN=\$\([ \t]*"
    r"gh[ \t]+auth[ \t]+token"
    rf"(?:[ \t]+--user[ \t]+{_PLAIN_WORD}"
    rf"|[ \t]+--hostname[ \t]+{_PLAIN_WORD})*"
    r"[ \t]*\)"
    r"(?=[ \t;&|\n]|$)")


def _subst_inner_ok(inner: str) -> bool:
    """A value substitution holds no gh call, except ``gh auth token``."""
    if "$(" in inner or "`" in inner:
        return False
    if ";" in inner or "|" in inner or "&" in inner or "\n" in inner:
        return False
    if not re.search(r"\bgh\b", inner):
        return True
    # Same blanks as ``[ \t]`` in the allowlist. ``str.strip()`` would also
    # drop CR, NBSP, VT and FF, which are not padding.
    return bool(_GH_AUTH_TOKEN_INNER.match(inner.strip(" \t")))


def _has_unterminated_quote(text: str) -> bool:
    """True when any quote in ``text`` is unterminated (via :func:`_tokenize_bash`)."""
    return _tokenize_bash(text).unterminated_quote


def _scan_constructs(text: str, base: int = 0):
    """Live backtick/heredoc flags and ``$(...)`` spans in ``text`` (via :func:`_tokenize_bash`)."""
    scan = _scan_folded(_fold_bash_continuations(
        text or "",
        mask_comments=_MASK_COMMENTS,
        fold_continuations_in_comments=_FOLD_CONTINUATIONS_IN_COMMENTS,
    ), base, mask_comments=_MASK_COMMENTS)
    return {"tick": scan.has_backtick, "heredoc": scan.has_heredoc}, list(scan.subst_spans)


def _value_end(text: str, i: int) -> Optional[int]:
    """End index of one GH_TOKEN value at ``i``, or None when not allowed.

    Allowed: a bare word, a quoted string, or ONE ``$(...)`` (checked by
    :func:`_subst_inner_ok`). Mixed forms like ``a$(cmd)`` are not allowed.
    """
    n = len(text)
    if i >= n:
        return None
    if text.startswith("$(", i):
        end = _subst_end(text, i + 1)
        if end >= n:
            return None
        if not text[i + 2:end].strip():
            return None
        if not _subst_inner_ok(text[i + 2:end]):
            return None
        return end + 1
    if text[i] in "'\"":
        body, end = _quoted_body(text, i)
        if not body:
            return None
        if "$(" in body or "`" in body:
            return None
        return end
    j = i
    while j < n and text[j] not in " \t;\n&|":
        if text.startswith("$(", j) or text[j] in "'\"`(){}<>":
            return None
        if text[j] == "\\" and j + 1 < n:
            j += 2
            continue
        j += 1
    return j if j > i else None


def _read_word(text: str, i: int) -> Optional[tuple[str, int]]:
    """One bare or quoted word at/after ``i`` -> (unquoted text, end)."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i >= n:
        return None
    if text[i] in "'\"":
        body, end = _quoted_body(text, i)
        return body, end
    j = i
    buf: list[str] = []
    while j < n and text[j] not in " \t;\n&|":
        if text[j] in "'\"":
            break
        if text[j] == "\\" and j + 1 < n:
            buf.append(text[j + 1])
            j += 2
            continue
        buf.append(text[j])
        j += 1
    if j == i:
        return None
    return "".join(buf), j


def _parse_gh_simple(text: str, i: int, *, prefix: bool,
                     spans: Optional[list] = None) -> Optional[int]:
    """Parse ONE gh simple command at ``i``; return end index or None.

    With ``prefix``, leading ``NAME=value`` assignments are consumed and at
    least one must be ``GH_TOKEN=<value>``. Arguments may not contain
    separators, pipes, substitutions or parens: anything else ends the
    allowlist match.
    """
    n = len(text)
    saw_gh_token = False
    if prefix:
        while True:
            while i < n and text[i] in " \t":
                i += 1
            am = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", text[i:])
            if not am:
                break
            vend = _value_end(text, i + am.end())
            if vend is None:
                return None
            if spans is not None and text.startswith("$(", i + am.end()):
                spans.append((i + am.end(), vend))
            if am.group(1) == "GH_TOKEN":
                if not _is_allowlisted_value(text, i + am.end(), vend):
                    return None
                saw_gh_token = True
            i = vend
        if not saw_gh_token:
            return None
    word = _read_word(text, i)
    if word is None:
        return None
    wtext, wend = word
    if not _GH_EXACT.match(wtext):
        return None
    i = wend
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return i
        c = text[i]
        if c in "<>":
            i += 1
            continue
        if c in ";\n&|(){}`$":
            return None
        tok = _read_word(text, i)
        if tok is None:
            return None
        i = tok[1]


def _skip_join(text: str, i: int) -> Optional[int]:
    """Consume one joiner (``;``, ``&&`` or newline, plus blanks)."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if text.startswith("&&", i):
        i += 2
    elif i < n and text[i] == ";":
        i += 1
    elif i < n and text[i] == "\n":
        while i < n and text[i] in "\n \t":
            i += 1
        return i
    else:
        return None
    while i < n and text[i] in " \t\n":
        i += 1
    return i


def _is_allowlisted_value(text: str, start: int, end: int) -> bool:
    """True when text[start:end] is exactly $(gh auth token [--user W] [--hostname W])."""
    raw = text[start:end]
    if not raw.startswith("$(") or not raw.endswith(")"):
        return False
    # Space and tab only, the same class as ``[ \t]*`` in
    # ``_ALLOWLISTED_GH_TOKEN_RE``. ``str.strip()`` also drops CR, NBSP,
    # VT and FF, which would mark a non-allowlisted value explicit.
    return bool(_GH_AUTH_TOKEN_INNER.match(raw[2:-1].strip(" \t")))


def already_explicit(command: str) -> bool:
    """True ONLY for the card #1476 allowlist (build #4433).

    (i) ONE simple gh command with a ``GH_TOKEN=<value>`` assignment prefix
    and nothing else; or (ii) ``export GH_TOKEN=<value>`` (optionally
    ``export -- GH_TOKEN=<value>``) as a simple command, joined by ``;``,
    ``&&`` or a newline to EXACTLY ONE following gh simple command, and
    nothing else. ``<value>`` is a bare word, a quoted string, or ONE
    ``$(...)`` with no gh inside (``gh auth token`` excepted, as the
    router's own injected form). Anything else -- a second gh call, any
    pipe, ``||``, a single ``&``, subshell, brace group, control keyword,
    function definition, heredoc or here-string, a backtick anywhere, or
    ``$(`` outside the prefix value -- is False.

    #1555: shares :func:`_tokenize_bash` with :func:`targets_gh` and
    :func:`_strip_allowlisted_gh_token`, so a ``\\<LF>`` inside a ``#``
    comment does NOT continue the comment onto the next line.
    """
    scan = _tokenize_bash(command or "")
    if scan.unterminated_quote:
        return False
    raw = scan.folded
    visible = scan.visible
    if not visible.strip():
        return False
    if "`" in visible or "<<" in visible:
        return False
    if scan.has_backtick or scan.has_heredoc:
        return False
    substs = scan.subst_spans

    def substs_allowed(spans: list) -> bool:
        if not substs:
            return True
        if len(visible) != len(raw):
            return False
        return all(any(s >= a and e <= b for a, b in spans)
                   for s, e in substs)

    text = visible
    start = 0
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    spans: list = []
    end = _parse_gh_simple(text, start, prefix=True, spans=spans)
    if end is not None and not text[end:].strip(" \t\r\n"):
        return substs_allowed(spans)
    word = _read_word(text, start)
    if word is None or word[0] != "export":
        return False
    i = word[1]
    nxt = _read_word(text, i)
    if nxt is not None and nxt[0] == "--":
        i = nxt[1]
    while i < len(text) and text[i] in " \t":
        i += 1
    if not text.startswith("GH_TOKEN=", i):
        return False
    vstart = i + len("GH_TOKEN=")
    vend = _value_end(text, vstart)
    if vend is None:
        return False
    if not _is_allowlisted_value(text, vstart, vend):
        return False
    spans = []
    if text.startswith("$(", vstart):
        spans.append((vstart, vend))
    i = _skip_join(text, vend)
    if i is None:
        return False
    end = _parse_gh_simple(text, i, prefix=False, spans=spans)
    if end is not None and not text[end:].strip(" \t\r\n"):
        return substs_allowed(spans)
    end = _parse_gh_simple(text, i, prefix=True, spans=spans)
    return (end is not None and not text[end:].strip(" \t\r\n")
            and substs_allowed(spans))


_EMPTY_GH_VALUE = ('""', "''")
_WRAPPER_WORDS = frozenset({
    "env", "sudo", "command", "exec", "nohup", "time", "nice",
    "timeout", "stdbuf", "rlwrap", "xargs",
})
_LEAD_KEYWORDS = frozenset({"then", "do", "else", "if", "while", "until", "elif", "!"})


def has_empty_gh_token(command: str) -> bool:
    """True when an empty ``GH_TOKEN`` would shadow the routed token (#4450)."""
    raw = command or ""
    if "GH_TOKEN" not in raw:
        return False
    visible = _tokenize_bash(raw).visible
    for m in re.finditer(r"GH_TOKEN=", visible):
        vpos = m.end()
        if visible[vpos:vpos + 2] in _EMPTY_GH_VALUE:
            vend = vpos + 2
        elif visible.startswith("$(", vpos):
            # Statically empty substitution: $() / $( ) (#4459).
            subst_end = _subst_end(visible, vpos + 1)
            if subst_end >= len(visible) or visible[vpos + 2:subst_end].strip():
                continue
            vend = subst_end + 1
        elif vpos >= len(visible) or visible[vpos] in " \t;\n&|":
            vend = vpos
        else:
            continue
        seg_start = max(visible.rfind(ch, 0, m.start()) for ch in ";\n&|({") + 1
        before_words = visible[seg_start:m.start()].split()
        while before_words and before_words[0] in _LEAD_KEYWORDS:
            before_words.pop(0)
        if before_words and before_words[0] == "export":
            return True
        # Prefix form: only assignments (and wrappers) may precede it.
        if any(not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w)
               and w not in _WRAPPER_WORDS and not re.fullmatch(r"-\w+", w)
               and not re.fullmatch(r"-?\d+", w)
               for w in before_words):
            continue
        i = vend
        while True:
            while i < len(visible) and visible[i] in " \t":
                i += 1
            am = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", visible[i:])
            if am:
                j = i + am.end()
                if visible[j:j + 2] in _EMPTY_GH_VALUE:
                    i = j + 2
                    continue
                if j < len(visible) and visible[j] in "'\"":
                    _, i = _quoted_body(visible, j)
                    continue
                if visible.startswith("$(", j):
                    i = _subst_end(visible, j + 1) + 1
                    continue
                k = j
                while k < len(visible) and visible[k] not in " \t;\n&|":
                    k += 1
                if k == j:
                    break
                i = k
                continue
            wm = re.match(r"([A-Za-z]+)(?=[ \t]|$)", visible[i:])
            if wm and wm.group(1) in _WRAPPER_WORDS:
                i += wm.end()
                if wm.group(1) == "timeout":
                    while i < len(visible) and visible[i] in " \t":
                        i += 1
                    tok = _read_word(visible, i)
                    if tok is not None:
                        i = tok[1]
                continue
            break
        word = _read_word(visible, i)
        if word is not None and _GH_EXACT.match(word[0]):
            return True
    return False


_DECL_FORM_WORDS = frozenset({"export", "declare", "typeset", "readonly", "local"})


def _value_word_end(text: str, i: int) -> int:
    """End of the whole value word at ``i`` (quote removal is the caller's job).

    Concatenated quoted, ``$'...'``/``$"..."``, ``$(...)`` and backtick
    segments belong to the one word; whitespace and separators end it.
    """
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t;\n&|(){}":
            break
        if text.startswith("$(", i):
            end = _subst_end(text, i + 1)
            i = end + 1 if end < n else n
            continue
        if c == "`":
            j = text.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("$'", i) or text.startswith('$"', i):
            _, end = _quoted_body(text, i + 1)
            i = end
            continue
        if c in "'\"":
            _, end = _quoted_body(text, i)
            i = end
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return i


def _consume_visible_value(text: str, i: int) -> int:
    n = len(text)
    if i >= n:
        return i
    if text.startswith("$(", i):
        end = _subst_end(text, i + 1)
        return end + 1 if end < n else n
    if text[i] == "`":
        j = text.find("`", i + 1)
        return n if j < 0 else j + 1
    if text.startswith("$'", i) or text.startswith('$"', i):
        _, end = _quoted_body(text, i + 1)
        return end
    if text[i] in "'\"":
        _, end = _quoted_body(text, i)
        return end
    while i < n and text[i] not in " \t":
        if text.startswith("$(", i):
            end = _subst_end(text, i + 1)
            i = end + 1 if end < n else n
            continue
        if text[i] in "'\"":
            _, i = _quoted_body(text, i)
            continue
        i += 1
    return i


def _is_executable_assignment_before(before: str) -> bool:
    """True when ``before`` (one segment, masked) is only assignment context."""
    i = 0
    n = len(before)
    while i < n and before[i] in " \t":
        i += 1
    while True:
        m = re.match(r"([A-Za-z]+)(?=[ \t]|$)", before[i:])
        if m and m.group(1) in _LEAD_KEYWORDS:
            i += m.end()
            while i < n and before[i] in " \t":
                i += 1
            continue
        break
    if i >= n:
        return True
    m = re.match(r"([A-Za-z]+)(?=[ \t]|$)", before[i:])
    if m and m.group(1) in _DECL_FORM_WORDS:
        i += m.end()
        while True:
            while i < n and before[i] in " \t":
                i += 1
            if i >= n:
                return True
            am = re.match(r"[A-Za-z_][A-Za-z0-9_]*=", before[i:])
            if am:
                i = _consume_visible_value(before, i + am.end())
                continue
            tok = _read_word(before, i)
            if tok is None:
                return False
            if tok[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok[0]):
                i = tok[1]
                continue
            return False
    while True:
        while i < n and before[i] in " \t":
            i += 1
        if i >= n:
            return True
        am = re.match(r"[A-Za-z_][A-Za-z0-9_]*=", before[i:])
        if am:
            i = _consume_visible_value(before, i + am.end())
            continue
        wm = re.match(r"([A-Za-z]+)(?=[ \t]|$)", before[i:])
        if wm and wm.group(1) in _WRAPPER_WORDS:
            i += wm.end()
            while True:
                while i < n and before[i] in " \t":
                    i += 1
                tok = _read_word(before, i)
                if tok is not None and (tok[0].startswith("-")
                                        or re.fullmatch(r"-?\d+", tok[0])):
                    i = tok[1]
                    continue
                break
            continue
        return False


def _strip_allowlisted_gh_token(command: str) -> str:
    """Drop each exact allowlisted ``GH_TOKEN=$(gh auth token ...)`` occurrence.

    #1555: shares :func:`_tokenize_bash` with :func:`targets_gh` and
    :func:`already_explicit`, so backslash-LF continuations are folded only
    where bash folds them (never inside a ``#`` comment, single quote or
    heredoc body), and only live unmasked occurrences are stripped.
    """
    scan = _tokenize_bash(command or "")
    return _ALLOWLISTED_GH_TOKEN_RE.sub(
        lambda m: (
            ""
            if scan.visible[m.start():m.end()] == scan.folded[m.start():m.end()]
            else m.group(0)
        ),
        scan.folded,
    )


def has_disallowed_gh_token_assignment(command: str) -> bool:
    """True when ``GH_TOKEN`` survives the exact allowlisted form (#4479).

    Before any rewrite, for a command that targets ``gh``: fold a
    backslash-LF continuation (not backslash-CR-LF), then strip each
    occurrence of ``[export
    ]GH_TOKEN=$(gh auth token [--user W] [--hostname W])`` with optional
    spaces or tabs inside the ``$( )`` (never a newline), followed by
    whitespace, ``;``, ``&``, ``|``, a newline, or end of text (W is a
    plain word). If ``GH_TOKEN`` is still in the text anywhere — quoted,
    a heredoc, ``bash -c``, ``unset``, ``env -u``, ``export -n``, a
    second assignment, a glued suffix, a longer name — deny. A non-exact
    value is not stripped.
    """
    stripped = _strip_allowlisted_gh_token(command or "")
    return "GH_TOKEN" in stripped


def inject(command: str, login: str) -> str:
    """Export the credential for this invocation. Leaves global `gh` state untouched.

    `gh auth token --user <login>` reads the stored OAuth token for that account
    WITHOUT switching the active one — the property the whole card rests on.

    >>> `export VAR=x; cmd`, NOT `VAR=x cmd`. THE FIRST VERSION USED THE PREFIX FORM
    AND WAS INERT FOR EVERY COMPOSED COMMAND. <<< In bash a `VAR=x cmd` prefix scopes
    VAR to `cmd` ALONE, so the token landed on `cd`, on `echo`, on whatever came
    first — never on `gh`. Found by drop-on-meta-edge, verified here:

        bash -c 'GH_TOKEN=FAKE cd /tmp && echo "[${GH_TOKEN}]"'   ->  []
        bash -c 'export GH_TOKEN=OK; cd /tmp && echo "[${GH_TOKEN}]"'  ->  [OK]

    `cd /tmp && gh pr list` was a DOCUMENTED SUPPORTED EXAMPLE and it did nothing.

    >>> AND IT WAS THE WORST AVAILABLE FAILURE MODE, because it REPORTED SUCCESS:
    rc=0 plus a systemMessage naming the resolved identity, while the gh process ran
    under the ambient account. The router's own output became the evidence that it
    had worked. <<<

    The suite could not see it: the test asserted the STRING SHAPE (startswith /
    endswith) and nothing executed. That is the proxy detector pointed at our own
    tests — same class as a 409 handler that is confident, well-tested, and about
    the wrong thing. test_the_injected_token_REACHES_a_composed_command now runs a
    real shell.

    The export is scoped to this Bash tool call's own shell, which is exactly the
    per-invocation property wanted — it is not `gh auth switch`, and it touches no
    global state and no other session.
    """
    return f'export GH_TOKEN=$(gh auth token --user {shlex.quote(login)}); {command}'
