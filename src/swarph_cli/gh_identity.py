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
_WRAPPERS = (
    r"env(?:\s+-\w+)*|sudo(?:\s+-\w+(?:\s+\S+)?)*|command(?:\s+-\w+)*|exec|nohup"
    r"|time|nice(?:\s+-n\s*-?\d+)?|timeout\s+\S+|stdbuf(?:\s+-\S+)*|rlwrap|xargs(?:\s+-\S+(?:\s+\S+)?)*"
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
_ENVPFX = r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\$\([^)]*\)|\S*)[ \t]+)*"
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


def _subst_end_hd(command: str, open_paren: int) -> int:
    """Heredoc-aware ``_subst_end``: body lines are raw text.

    A ``)`` or quote char inside a heredoc body neither closes the
    substitution nor opens a quote. ``<<<`` is a here-string, not a
    heredoc: a ``<<`` whose previous char is ``<`` never registers.
    """
    depth = 1
    i = open_paren + 1
    n = len(command)
    pending: list[tuple[str, bool]] = []
    while i < n and depth:
        c = command[i]
        if c == "\n":
            i += 1
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
        if c == "<" and not (i > 0 and command[i - 1] == "<"):
            m = _HEREDOC_OP.match(command, i)
            if m is not None:
                pending.append((m.group(2), bool(m.group(1))))
                i = m.end()
                continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _mask_quoted(command: str, *, mask_comments: bool = False) -> str:
    """Blank the insides of quotes. A quoted ``gh`` is not a command (card #1340).

    ``$(...)`` and backticks stay, including inside double quotes, because those
    are command substitutions. Their contents are masked the same way, so a
    quote inside a substitution does not hide a real ``gh`` and a quoted
    substitution does not invent one.
    Comments are masked only for explicit-token detection: a hash in a heredoc
    or expansion is not a reliable comment boundary for command detection.
    """
    out: list[str] = []
    i = 0
    n = len(command)

    def blanks(text: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in text)

    while i < n:
        c = command[i]
        if mask_comments and c == "#" and (i == 0 or command[i - 1].isspace() or command[i - 1] in ";|&()"):
            end = command.find("\n", i)
            end = n if end < 0 else end
            out.append(blanks(command[i:end]))
            i = end
            continue
        if c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                # An unmatched quote may be comment text; keep later commands visible.
                out.append(command[i:])
                break
            out.append(c)
            out.append(blanks(command[i + 1:j]))
            out.append("'")
            i = j + 1
            continue
        if c == '"':
            quote_start, output_start = i, len(out)
            subst_seen = False
            out.append(c)
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
                    subst_seen = True
                    end = _subst_end_hd(command, i + 1)
                    inner = _mask_quoted(command[i + 2:end], mask_comments=mask_comments)
                    out.append("$(" + inner + (")" if end < n else ""))
                    i = end + 1 if end < n else n
                    continue
                if command[i] == "`":
                    j = command.find("`", i + 1)
                    if j < 0:
                        out.append("`")
                        out.append(_mask_quoted(command[i + 1:], mask_comments=mask_comments))
                        i = n
                        break
                    out.append("`" + _mask_quoted(command[i + 1:j], mask_comments=mask_comments) + "`")
                    i = j + 1
                    continue
                out.append("\n" if command[i] == "\n" else " ")
                i += 1
            if i < n and command[i] == '"':
                out.append('"')
                i += 1
            elif subst_seen:
                # Fail closed (see _mask_explicit): a substitution was
                # seen, so this region held command text; never restore
                # it raw. A bare unmatched quote may be comment text,
                # so without a substitution later commands stay visible.
                out[output_start:] = [blanks(command[quote_start:])]
            else:
                out[output_start:] = [command[quote_start:]]
            continue
        out.append(c)
        i += 1
    return "".join(out)


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
    """True when the command actually invokes `gh`, not when a quote mentions it."""
    return _targets_gh(command or "", 0)


def _targets_gh(command: str, depth: int) -> bool:
    visible = _mask_quoted(command, mask_comments=False)
    if _GH_INVOCATION.search(visible):
        return True
    if depth >= _MAX_SHELL_DEPTH:
        return True
    if _quoted_gh_command_word(command, visible):
        return True
    for inner in _shell_texts(command, visible):
        if inner != command and _targets_gh(inner, depth + 1):
            return True
    return False


_HEREDOC_OP = re.compile(r"<<(-)?(?!<)[ \t]*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _is_terminator(line: str, delim: str, strip_tabs: bool) -> bool:
    """Exact heredoc terminator: only leading tabs count, and only for ``<<-``."""
    if strip_tabs:
        return line.lstrip("\t") == delim
    return line == delim


def _mask_heredoc_bodies(command: str, visible: str) -> str:
    """Blank heredoc bodies in ``visible`` (positions align with ``command``)."""
    if "<<" not in visible:
        return visible
    chars = list(visible)
    n = len(command)
    pos = 0
    while pos < n:
        m = _HEREDOC_OP.search(command, pos)
        if m is None:
            break
        if visible[m.start():m.start() + 2] != "<<":
            pos = m.end()
            continue
        line_end = command.find("\n", m.start())
        if line_end < 0:
            break
        delims = [(mm.group(2), bool(mm.group(1)))
                  for mm in _HEREDOC_OP.finditer(command[m.start():line_end])]
        cursor = line_end + 1
        for delim, strip_tabs in delims:
            body_start = cursor
            scan = cursor
            body_end = n
            after = n
            while scan <= n:
                nl = command.find("\n", scan)
                lend = n if nl < 0 else nl
                if _is_terminator(command[scan:lend], delim, strip_tabs):
                    body_end = scan
                    after = lend + 1 if nl >= 0 else n
                    break
                if nl < 0:
                    break
                scan = nl + 1
            else:
                after = n
            for k in range(body_start, min(body_end, len(chars))):
                if chars[k] != "\n":
                    chars[k] = " "
            cursor = after
            if after >= n:
                break
        pos = max(cursor, line_end + 1)
    return "".join(chars)


def _mask_explicit(command: str) -> str:
    """Mask ``command`` for explicit-token detection, heredocs FIRST.

    #4423: masking quotes before heredoc bodies lets a quote inside a body
    pair across the body end and hide later real syntax. Here body lines are
    consumed raw at each newline, so their quotes never enter quote state.
    ``$'...'`` honours backslash escapes; plain ``'...'`` does not.

    >>> INVARIANT: hiding text can only route (fail closed). <<< Any
    region the masker cannot close — an unmatched single quote, ``$''``
    quote or double quote, or an unclosed ``$(`` or backtick — blanks
    the rest of the text and is NEVER restored raw: restored raw text
    could expose a hidden ``export GH_TOKEN=`` as real syntax, while
    blanked text can only make the call route (or deny), never falsely
    explicit.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    pending: list[tuple[str, bool]] = []

    def blanks(text: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in text)

    while i < n:
        c = command[i]
        if c == "\n":
            out.append("\n")
            i += 1
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    nl = command.find("\n", i)
                    lend = n if nl < 0 else nl
                    line = command[i:lend]
                    if _is_terminator(line, delim, strip_tabs):
                        out.append(line)
                        if nl >= 0:
                            out.append("\n")
                            i = lend + 1
                        else:
                            i = n
                        break
                    out.append(blanks(line))
                    if nl < 0:
                        i = n
                        break
                    out.append("\n")
                    i = nl + 1
            continue
        if c == "#" and (i == 0 or command[i - 1].isspace() or command[i - 1] in ";|&()"):
            end = command.find("\n", i)
            end = n if end < 0 else end
            out.append(blanks(command[i:end]))
            i = end
            continue
        if c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "<":
            # `<<<` is a here-string, not a heredoc: a `<<` whose
            # previous char is `<` never registers a body.
            m = None if (i > 0 and command[i - 1] == "<") \
                else _HEREDOC_OP.match(command, i)
            if m is not None:
                pending.append((m.group(2), bool(m.group(1))))
                out.append(command[i:m.end()])
                i = m.end()
                continue
            out.append(c)
            i += 1
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "'":
            j = i + 2
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if command[j] == "'":
                    break
                j += 1
            if j >= n:
                out.append(blanks(command[i:]))
                i = n
                continue
            out.append("$'")
            out.append(blanks(command[i + 2:j]))
            out.append("'")
            i = j + 1
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                out.append(blanks(command[i:]))
                i = n
                continue
            out.append(c)
            out.append(blanks(command[i + 1:j]))
            out.append("'")
            i = j + 1
            continue
        if c == '"':
            quote_start, output_start = i, len(out)
            out.append(c)
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
                    end = _subst_end(command, i + 1)
                    inner = _mask_explicit(command[i + 2:end])
                    out.append("$(" + inner + (")" if end < n else ""))
                    i = end + 1 if end < n else n
                    continue
                if command[i] == "`":
                    j = command.find("`", i + 1)
                    if j < 0:
                        out.append(blanks(command[i:]))
                        i = n
                        break
                    out.append("`" + _mask_explicit(command[i + 1:j]) + "`")
                    i = j + 1
                    continue
                out.append("\n" if command[i] == "\n" else " ")
                i += 1
            if i < n and command[i] == '"':
                out.append('"')
                i += 1
            else:
                out[output_start:] = [blanks(command[quote_start:])]
            continue
        out.append(c)
        i += 1
    return "".join(out)


_PLAIN_WORD = r"[A-Za-z0-9._-]+"
_GH_AUTH_TOKEN_INNER = re.compile(
    rf"^(?:(?:/[\w.-]+)*/)?gh[ \t]+auth[ \t]+token"
    rf"(?:[ \t]+--user[ \t]+{_PLAIN_WORD}"
    rf"|[ \t]+--hostname[ \t]+{_PLAIN_WORD})*$")


def _subst_inner_ok(inner: str) -> bool:
    """A value substitution holds no gh call, except ``gh auth token``."""
    if "$(" in inner or "`" in inner:
        return False
    if ";" in inner or "|" in inner or "&" in inner or "\n" in inner:
        return False
    if not re.search(r"\bgh\b", inner):
        return True
    return bool(_GH_AUTH_TOKEN_INNER.match(inner.strip()))


def _has_unterminated_quote(text: str) -> bool:
    """True when any quote in ``text`` is unterminated.

    Heredoc bodies and comments are not live quote text. Double-quoted
    interiors are live for ``$(...)``, whose inner text is checked too.
    """
    pending: list[tuple[str, bool]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    nl = text.find("\n", i)
                    lend = n if nl < 0 else nl
                    if _is_terminator(text[i:lend], delim, strip_tabs):
                        i = lend + 1 if nl >= 0 else n
                        break
                    i = n if nl < 0 else nl + 1
            continue
        if c == "#" and (i == 0 or text[i - 1].isspace()
                         or text[i - 1] in ";|&()"):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "<":
            if text.startswith("<<<", i):
                i += 3
                continue
            m = None if (i > 0 and text[i - 1] == "<") \
                else _HEREDOC_OP.match(text, i)
            if m is not None:
                pending.append((m.group(2), bool(m.group(1))))
                i = m.end()
                continue
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            if j >= n:
                return True
            i = j + 1
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return True
            i = j + 1
            continue
        if c == '"':
            i += 1
            closed = False
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == '"':
                    closed = True
                    i += 1
                    break
                if text.startswith("$(", i):
                    end = _subst_end_hd(text, i + 1)
                    if _has_unterminated_quote(text[i + 2:end]):
                        return True
                    i = end + 1 if end < n else n
                    continue
                i += 1
            if not closed:
                return True
            continue
        if text.startswith("$(", i):
            end = _subst_end_hd(text, i + 1)
            if _has_unterminated_quote(text[i + 2:end]):
                return True
            i = end + 1 if end < n else n
            continue
        i += 1
    return False


def _scan_constructs(text: str, base: int = 0):
    """Live backtick/heredoc flags and ``$(...)`` spans in ``text``.

    Single-quoted text and heredoc bodies are not live. Double-quoted
    interiors are live for substitutions and backticks. Spans are
    absolute (offset by ``base``) so they can be checked against the
    one allowed prefix-value substitution.
    """
    flags = {"tick": False, "heredoc": False}
    spans: list[tuple[int, int]] = []
    pending: list[tuple[str, bool]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    nl = text.find("\n", i)
                    lend = n if nl < 0 else nl
                    if _is_terminator(text[i:lend], delim, strip_tabs):
                        i = lend + 1 if nl >= 0 else n
                        break
                    i = n if nl < 0 else nl + 1
            continue
        if c == "#" and (i == 0 or text[i - 1].isspace()
                        or text[i - 1] in ";|&()"):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "<":
            if text.startswith("<<<", i):
                flags["heredoc"] = True
                i += 3
                continue
            m = None if (i > 0 and text[i - 1] == "<") \
                else _HEREDOC_OP.match(text, i)
            if m is not None:
                flags["heredoc"] = True
                pending.append((m.group(2), bool(m.group(1))))
                i = m.end()
                continue
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            i = j + 1 if j < n else n
            continue
        if c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text.startswith("$(", i):
                    end = _subst_end_hd(text, i + 1)
                    stop = end + 1 if end < n else n
                    spans.append((base + i, base + stop))
                    sub_flags, sub_spans = _scan_constructs(
                        text[i + 2:end], base + i + 2)
                    flags["tick"] |= sub_flags["tick"]
                    flags["heredoc"] |= sub_flags["heredoc"]
                    spans.extend(sub_spans)
                    i = stop
                    continue
                if text[i] == "`":
                    flags["tick"] = True
                    j = text.find("`", i + 1)
                    i = n if j < 0 else j + 1
                    continue
                i += 1
            i = i + 1 if i < n else n
            continue
        if text.startswith("$(", i):
            end = _subst_end_hd(text, i + 1)
            stop = end + 1 if end < n else n
            spans.append((base + i, base + stop))
            sub_flags, sub_spans = _scan_constructs(
                text[i + 2:end], base + i + 2)
            flags["tick"] |= sub_flags["tick"]
            flags["heredoc"] |= sub_flags["heredoc"]
            spans.extend(sub_spans)
            i = stop
            continue
        if c == "`":
            flags["tick"] = True
            j = text.find("`", i + 1)
            if j < 0:
                i = n
            else:
                _, sub_spans = _scan_constructs(text[i + 1:j], base + i + 1)
                spans.extend(sub_spans)
                i = j + 1
            continue
        i += 1
    return flags, spans


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
    ``$(`` outside the prefix value -- is False. Masking stays a
    fail-closed pre-pass (:func:`_mask_explicit`), so hidden text can
    only route.
    """
    raw = command or ""
    if _has_unterminated_quote(raw):
        return False
    visible = _mask_explicit(raw)
    if not visible.strip():
        return False
    if "`" in visible or "<<" in visible:
        return False
    # _mask_explicit blanks heredoc bodies inside quotes, hiding their
    # markers, so live constructs are scanned on the raw text as well.
    flags, substs = _scan_constructs(raw)
    if flags["tick"] or flags["heredoc"]:
        return False

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
    """True when an empty ``GH_TOKEN`` would shadow the routed token (#4450).

    An empty assignment (``GH_TOKEN=""``, ``GH_TOKEN=''``, bare
    ``GH_TOKEN=`` or a statically empty substitution ``GH_TOKEN=$()`` /
    ``GH_TOKEN=$( )``) on the gh command itself, or an empty
    ``export GH_TOKEN=...`` anywhere in the same command list, cannot be
    rescued by the router's rewrite: the prefix form shadows the router's
    export for that command, the export form runs after it, and ``gh``
    treats an empty ``GH_TOKEN`` as unset, falling back to the ambient
    hosts-file login. Detection runs on :func:`_mask_explicit` output, so
    quoted mentions, comments and heredoc bodies do not count; on masked
    text a non-empty quoted value holds at least one blank, so adjacent
    quotes are exactly the empty quoted form.
    """
    raw = command or ""
    if "GH_TOKEN" not in raw:
        return False
    visible = _mask_explicit(raw)
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
