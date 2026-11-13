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
- adjacent quoted pieces: bash -c 'echo 1;'"gh pr view 1"
- a dynamic pipe into a shell: echo "$CMD" | bash
- unsupported constant pipelines: printf, echo -e / echo -n; receivers
  bash -x / bash -e / bash -, env bash, sh -s --; intermediate tee,
  a cat here-string or cat heredoc piped to bash, or xargs. Plain echo into bash/sh -s
  is supported, including a trailing output redirect or comment.
- $(which gh) pr list
- bash -c "$(echo gh) pr view 1"
- a variable: CMD="gh pr view 1"; bash -c "$CMD"
"""
from __future__ import annotations

import json
import os
import re
import shlex
from bisect import bisect_left
from collections import deque
from functools import wraps
from contextvars import ContextVar
from dataclasses import dataclass, field
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
_START = (r"(?:^|[;&|]|\$\(|`|\(|\)"
          r"|(?<![^\s;&|(){}])(?:\{|\b(?:then|do|else|if|while|until|elif)\b)"
          r"|&&|\|\|)")
_LEAD = r"[ \t]*"                       # leading whitespace/tab — also missed before
_BANGPFX = r"(?:![ \t]+)*"
# Prefix matching must not swallow command separators or keyword-looking assignment data.
_ENV_VALUE = r"(?:\\[^\s]|[^\s;|&\\<>]|[<>]&|&>|>\||[<>](?![&|]))*"
_ENVPFX = rf"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\$\([^)]*\)|(?!\$\(){_ENV_VALUE})[ \t]+)*"
_WRAPPFX = rf"(?:(?:{_WRAPPERS})[ \t]+)*"
_WRAPPFX_PLUS = rf"(?:(?:{_WRAPPERS})[ \t]+)+"
_CMD_PREFIX = rf"{_BANGPFX}{_ENVPFX}(?:{_WRAPPFX_PLUS}{_ENVPFX})?"
#: `gh`, `./gh`, or any absolute path ending in /gh.
_GH_WORD = r"(?:\.{0,2}/|(?:/[\w.-]+)*/)?gh"

_GH_INVOCATION = re.compile(
    rf"{_START}{_LEAD}{_CMD_PREFIX}{_GH_WORD}(?=[ \t]|$)", re.MULTILINE)
_GH_INVOCATION_LEGACY = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}{_GH_WORD}(?=[ \t]|$)", re.MULTILINE)

#: Already carries an explicit GH_TOKEN — an operator decision; do not double-inject.
#: Card #1476 narrows this to position, not text presence (see already_explicit).


class RouterRefusal(Exception):
    """Raised instead of falling back. Carries the operator-facing reason."""


@dataclass
class _RoutingWork:
    limit: int
    steps: int = 0
    cache: dict = field(default_factory=dict, repr=False)


_ROUTING_WORK = ContextVar("gh_routing_work", default=None)
_HOOK_STATE = ContextVar("gh_hook_state", default=None)
_PARSER_NESTING = ContextVar("gh_parser_nesting", default=0)
_WORK_PER_CHARACTER = 1280
_WORK_FIXED_ALLOWANCE = 65536
_WORK_ABSOLUTE_LIMIT = 132000000
# A completed memo hit avoids the work of constructing a new cached match.
_CACHE_WORK = 128
_CACHE_HIT_WORK = 32
_CURSOR_WORK = 16


def _bounded_parser(parse):
    @wraps(parse)
    def bounded(*args, **kwargs):
        depth = _PARSER_NESTING.get()
        if depth >= 256:
            raise RouterRefusal("command too complex to route; split the command and retry")
        token = _PARSER_NESTING.set(depth + 1)
        try:
            return parse(*args, **kwargs)
        finally:
            _PARSER_NESTING.reset(token)
    return bounded


def _charge_scan(characters: Optional[int] = None) -> None:
    """Count actual visits/ranges, with weighted completed cache operations."""
    work = _ROUTING_WORK.get()
    if work is not None:
        work.steps += _CURSOR_WORK if characters is None else characters
        if work.steps > work.limit:
            raise RouterRefusal("command too complex to route; split the command and retry")


def _scan_find(text: str, needle: str, start: int) -> int:
    end = text.find(needle, start)
    _charge_scan(max(0, (len(text) if end < 0 else end + len(needle)) - start))
    return end


def _span_origin(text: str) -> tuple[str, int]:
    work = _ROUTING_WORK.get()
    if work is None:
        return text, 0
    entry = work.cache.setdefault(("origin", id(text)), (text, text, 0))
    return entry[1], entry[2]


def _subspan(text: str, start: int, stop: int) -> str:
    work = _ROUTING_WORK.get()
    if work is None:
        return text[start:stop]
    root, offset = _span_origin(text)
    start, stop, _ = slice(start, stop).indices(len(text))
    key = ("slice", id(root), offset + start, offset + stop)
    if key not in work.cache:
        value = text[start:stop]
        if value is not text:
            _charge_scan(len(value))
        work.cache[key] = value
        work.cache[("origin", id(value))] = (value, root, offset + start)
    return work.cache[key]


def _separator_positions(text: str, characters: str) -> tuple:
    work = _ROUTING_WORK.get()
    root, offset = _span_origin(text)
    key = ("separators", id(root), characters)
    if key not in work.cache:
        positions = tuple(m.start() for m in re.finditer("[" + re.escape(characters) + "]", root))
        _charge_scan(len(root))
        work.cache[key] = positions
    return work.cache[key], offset


def _previous_separator(text: str, stop: int, characters: str) -> int:
    if _ROUTING_WORK.get() is None:
        return max(text.rfind(c, 0, stop) for c in characters)
    positions, offset = _separator_positions(text, characters)
    index = bisect_left(positions, offset + stop) - 1
    _charge_scan(_CACHE_WORK)
    return positions[index] - offset if index >= 0 and positions[index] >= offset else -1


def _next_separator(text: str, start: int, characters: str) -> int:
    if _ROUTING_WORK.get() is None:
        return min((p for c in characters if (p := text.find(c, start)) >= 0), default=-1)
    positions, offset = _separator_positions(text, characters)
    index = bisect_left(positions, offset + start)
    _charge_scan(_CACHE_WORK)
    return positions[index] - offset if index < len(positions) and positions[index] < offset + len(text) else -1


def _match_lookup(text: str, start: int, kind: tuple) -> tuple:
    work = _ROUTING_WORK.get()
    if work is None:
        return None, 0, None
    root, offset = _span_origin(text)
    key = ("match", kind, id(root), offset + start)
    found = work.cache.get(key)
    if found is not None:
        _charge_scan(_CACHE_HIT_WORK)
        return key, offset, min(len(text), found - offset)
    if ("miss", key, offset + len(text)) in work.cache:
        _charge_scan(_CACHE_HIT_WORK)
        return key, offset, len(text)
    _charge_scan(_CACHE_WORK)
    return key, offset, None


def _match_return(key: tuple, offset: int, text: str, end: int) -> int:
    work = _ROUTING_WORK.get()
    if work is not None:
        if end < len(text):
            work.cache[key] = offset + end
        else:
            work.cache[("miss", key, offset + len(text))] = True
        _charge_scan(_CACHE_WORK)
    return end


_QUOTE_SPECIAL = re.compile(r'["\\$`]')


def _quote_run_end(text: str, start: int) -> int:
    key, offset, cached = _match_lookup(text, start, ("quote-run",))
    if cached is not None:
        return cached
    match = _QUOTE_SPECIAL.search(text, start)
    end = len(text) if match is None else match.start()
    _charge_scan(end - start + (match is not None))
    return _match_return(key, offset, text, end)


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


_HEREDOC_OP_ID_ONLY = re.compile(
    r"<<(-)?(?!<)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2(?=[ \t\n;|&()<>]|$)"
)
_ASSIGN_NAME_AT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_OUTPUT_REDIRECT_AT = re.compile(r"(?:[0-9]*>>?\|?|&>>?)(?!&)")
_FD_REDIRECT_AT = re.compile(r"[0-9]*(?:>&[0-9-]+|<&?[0-9-]*)")
_WORD_AT = re.compile(r"([A-Za-z]+)(?=[ \t]|$)")
_FD_OUTPUT_AT = re.compile(r"[0-9]+>")

#: Module-level switches so mutant tests can flip each behavior and verify a test fails.
_MASK_COMMENTS = True
_FOLD_CONTINUATIONS_IN_COMMENTS = False
_HEREDOC_IDENTIFIER_ONLY = False
_SCAN_SHELL_HEREDOC = True
_SCAN_COMMENTS_FOR_TARGETS = False
_ENABLE_BANG_AND_ENV_ASSIGN = True
_SCAN_WRITE_THEN_RUN = True
_RESOLVE_ANSI_C = True

_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _unescape_ansi_c(text: str, start: int) -> tuple[str, int, bool]:
    """Parse ``$'...'`` starting after ``$'`` at ``start`` -> (unescaped, next_idx, closed)."""
    # Bash finds the word boundary before decoding escapes such as backslash-c.
    end = start
    while end < len(text) and text[end] != "'":
        _charge_scan()
        end += 2 if text[end] == "\\" and end + 1 < len(text) else 1
    closed = end < len(text)
    n = end
    j = start
    buf: list[str] = []
    while j < n:
        _charge_scan()
        c = text[j]
        if c == "\\" and j + 1 < n:
            nxt = text[j + 1]
            if nxt in _ANSI_C_SIMPLE:
                buf.append(_ANSI_C_SIMPLE[nxt])
                j += 2
                continue
            if nxt == "x" and j + 2 < n:
                k = j + 2
                while k < min(n, j + 4) and text[k] in "0123456789abcdefABCDEF":
                    _charge_scan()
                    k += 1
                if k > j + 2:
                    buf.append(chr(int(text[j + 2:k], 16)))
                    j = k
                    continue
            if nxt in ("u", "U") and j + 2 < n:
                max_hex = 4 if nxt == "u" else 8
                k = j + 2
                while k < min(n, j + 2 + max_hex) and text[k] in "0123456789abcdefABCDEF":
                    _charge_scan()
                    k += 1
                if k > j + 2:
                    cp = int(text[j + 2:k], 16)
                    if 0 <= cp <= 0x10FFFF:
                        buf.append(chr(cp))
                        j = k
                        continue
            if nxt == "c" and j + 2 < n:
                buf.append(chr(ord(text[j + 2]) & 0x1F))
                j += 3
                continue
            if nxt in "01234567":
                k = j + 1
                while k < min(n, j + 4) and text[k] in "01234567":
                    _charge_scan()
                    k += 1
                buf.append(chr(int(text[j + 1:k], 8) & 0xFF))
                j = k
                continue
            buf.append(text[j:j + 2])
            j += 2
            continue
        buf.append(c)
        j += 1
    return "".join(buf), end + 1 if closed else end, closed


def _parse_heredoc_op(
    text: str,
    pos: int,
    *,
    identifier_only: Optional[bool] = None,
) -> Optional[tuple[str, bool, bool, int, bool]]:
    """Parse ``<<[-] word`` at ``pos`` as bash does (any word, after quote removal).

    Returns ``(delim, strip_tabs, quoted_delim, end_pos, bad_delim)`` or ``None``
    when ``text[pos:]`` is not a heredoc operator.
    """
    if not text.startswith("<<", pos) or text.startswith("<<<", pos):
        return None
    if pos > 0 and text[pos - 1] == "<":
        return None
    id_only = _HEREDOC_IDENTIFIER_ONLY if identifier_only is None else identifier_only
    if id_only:
        m = _HEREDOC_OP_ID_ONLY.match(text, pos)
        if m is None:
            return None
        return (m.group(3), bool(m.group(1)), bool(m.group(2)), m.end(), False)
    strip_tabs = text.startswith("<<-", pos)
    j = pos + (3 if strip_tabs else 2)
    n = len(text)
    while j < n and text[j] in " \t":
        _charge_scan()
        j += 1
    if j >= n or text[j] in "\n;|&()<>#":
        return ("", strip_tabs, False, j, True)
    parts: list[str] = []
    quoted = False
    bad = False
    while j < n and text[j] not in " \t\n;|&()<>":
        _charge_scan()
        c = text[j]
        if c == "\\":
            quoted = True
            if j + 1 < n:
                parts.append(text[j + 1])
                j += 2
            else:
                bad = True
                j = n
            continue
        if text.startswith("$'", j):
            quoted = True
            val, j, closed = _unescape_ansi_c(text, j + 2)
            parts.append(val)
            if not closed:
                bad = True
                break
            continue
        if c == "'":
            quoted = True
            k = _scan_find(text, "'", j + 1)
            if k < 0:
                bad = True
                j = n
                break
            parts.append(text[j + 1:k])
            j = k + 1
            continue
        if c == '"' or text.startswith('$"', j):
            quoted = True
            k = j + (2 if c == "$" else 1)
            while k < n and text[k] != '"':
                _charge_scan()
                if text[k] == "\\" and k + 1 < n:
                    if text[k + 1] in '"\\$`\n':
                        parts.append(text[k + 1])
                    else:
                        parts.append(text[k:k + 2])
                    k += 2
                    continue
                parts.append(text[k])
                k += 1
            if k >= n:
                bad = True
                j = n
                break
            j = k + 1
            continue
        parts.append(c)
        j += 1
    return ("".join(parts), strip_tabs, quoted, j, bad)


def _unquote_word_at(text: str, pos: int) -> tuple[Optional[str], int, bool]:
    """Read one shell word at ``pos`` and perform bash quote/escape removal.

    Returns ``(unquoted_or_None_if_dynamic, end_pos, had_quote_or_escape)``.
    If the word contains unescaped ``$`` (other than ``$'...'`` / ``$"..."``)
    or backticks, ``unquoted`` is ``None``.
    """
    n = len(text)
    j = pos
    parts: list[str] = []
    dynamic = False
    had_qe = False
    while j < n and text[j] not in " \t\n;|&()<>":
        _charge_scan()
        c = text[j]
        if c == "\\":
            had_qe = True
            if j + 1 < n:
                parts.append(text[j + 1])
                j += 2
            else:
                dynamic = True
                j = n
            continue
        if _RESOLVE_ANSI_C and text.startswith("$'", j):
            had_qe = True
            val, j, closed = _unescape_ansi_c(text, j + 2)
            if not closed:
                dynamic = True
                break
            parts.append(val)
            continue
        if c == "'":
            had_qe = True
            k = _scan_find(text, "'", j + 1)
            if k < 0:
                dynamic = True
                j = n
                break
            parts.append(text[j + 1:k])
            j = k + 1
            continue
        if c == '"' or text.startswith('$"', j):
            had_qe = True
            k = j + (2 if c == "$" else 1)
            while k < n and text[k] != '"':
                _charge_scan()
                run_end = _quote_run_end(text, k)
                if run_end > k:
                    parts.append(_subspan(text, k, run_end))
                    k = run_end
                    continue
                if text[k] == "\\" and k + 1 < n:
                    if text[k + 1] in '"\\$`\n':
                        parts.append(text[k + 1])
                    else:
                        parts.append(text[k:k + 2])
                    k += 2
                    continue
                if text[k] in "$`":
                    dynamic = True
                parts.append(text[k])
                k += 1
            if k >= n:
                dynamic = True
                j = n
                break
            j = k + 1
            continue
        if c in "$`":
            dynamic = True
            if text.startswith("$(", j):
                end = _subst_end_hd(text, j + 1)
                j = end + 1 if end < n else n
                continue
            if text.startswith("${", j):
                end = _scan_find(text, "}", j + 2)
                j = n if end < 0 else end + 1
                continue
            if c == "`":
                end = _scan_find(text, "`", j + 1)
                j = n if end < 0 else end + 1
                continue
        parts.append(c)
        j += 1
    return (None if dynamic else "".join(parts)), j, had_qe


def _blanks(text: str) -> str:
    work = _ROUTING_WORK.get()
    key = ("blanks", id(text))
    if work is not None and key in work.cache:
        return work.cache[key][1]
    result = "".join("\n" if ch == "\n" else " " for ch in text)
    _charge_scan(len(text))
    if work is not None:
        work.cache[key] = (text, result)
    return result


def _is_terminator(line: str, delim: str, strip_tabs: bool) -> bool:
    """Exact heredoc terminator: only leading tabs count, and only for ``<<-``."""
    if strip_tabs:
        return line.lstrip("\t") == delim
    return line == delim


@_bounded_parser
def _quote_end(command: str, start: int, *, mask_comments: Optional[bool] = None,
               heredocs: bool = True) -> int:
    """Match a quote while keeping nested substitutions in their own quote scope."""
    mc = _MASK_COMMENTS if mask_comments is None else mask_comments
    kind = ("quote", mc, heredocs, _COMMANDS_READING.get())
    key, offset, cached = _match_lookup(command, start, kind)
    if cached is not None:
        return cached
    quote = command[start]
    n = len(command)
    if quote == "'":
        end = _scan_find(command, "'", start + 1)
        return _match_return(key, offset, command, end if end >= 0 else n)
    i = start + 1
    while i < n:
        _charge_scan()
        if quote == '"':
            run_end = _quote_run_end(command, i)
            if run_end > i:
                i = run_end
                continue
        if command[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if command[i] == quote:
            return _match_return(key, offset, command, i)
        if quote == '"' and command.startswith("$(", i):
            end = (_subst_end_hd(command, i + 1, mask_comments=mask_comments)
                   if heredocs else _subst_end(command, i + 1))
            i = end + 1
            continue
        if quote == '"' and command[i] == "`":
            i = _quote_end(command, i, mask_comments=mask_comments, heredocs=heredocs) + 1
            continue
        i += 1
    return _match_return(key, offset, command, n)


_COMMANDS_READING = ContextVar("gh_commands_reading", default=False)
_BOUNDED_COMMANDS = ContextVar("gh_bounded_commands", default=False)
_ARITHMETIC_SEEN = ContextVar("gh_arithmetic_seen", default=None)
_STANDALONE_BODIES = ContextVar("gh_standalone_bodies", default=None)
_INNER_HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)-?")


def _is_arithmetic(command: str, open_paren: int, end: int) -> bool:
    arithmetic = (not _COMMANDS_READING.get()
                  and command.startswith("((", open_paren) and end < len(command)
                  and _subst_end(command, open_paren + 1) == end - 1)
    if arithmetic and _ARITHMETIC_SEEN.get() is not None:
        _ARITHMETIC_SEEN.set(True)
        bodies = _STANDALONE_BODIES.get()
        if bodies is not None:
            body = _subspan(command, open_paren + 2, end - 1)
            if "<<" in body:
                bodies.setdefault(body, None)
    return arithmetic


def _subst_end(command: str, open_paren: int) -> int:
    """Index of the ``)`` matching ``command[open_paren] == '('``."""
    key, offset, cached = _match_lookup(command, open_paren, ("paren",))
    if cached is not None:
        return cached
    depth = 1
    opened = [open_paren]
    i = open_paren + 1
    n = len(command)
    while i < n and depth:
        _charge_scan()
        c = command[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if command.startswith("$'", i):
            _, i, _ = _unescape_ansi_c(command, i + 2)
            continue
        if c == "'":
            j = _scan_find(command, "'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c in ('"', "`"):
            i = _quote_end(command, i, heredocs=False) + 1
            continue
        if c == "(":
            depth += 1
            opened.append(i)
        elif c == ")":
            depth -= 1
            opening = opened.pop()
            if key is not None:
                _match_return((*key[:-1], offset + opening), offset, command, i)
            if depth == 0:
                return i
        i += 1
    return _match_return(key, offset, command, n)


_CASE_WORD_RE = re.compile(r"case(?=[ \t\n])")
_ESAC_WORD_RE = re.compile(r"esac(?=[ \t\n;|&)]|$)")
_IN_WORD_RE = re.compile(r"in(?=[ \t\n;|&()]|$)")


def _subst_end_hd(command: str, open_paren: int, *,
                  mask_comments: Optional[bool] = None) -> int:
    """Heredoc-, comment-, and ``case``-aware ``_subst_end``: body lines are raw text.

    A ``)`` or quote char inside a heredoc body or a word-start ``#``
    comment neither closes the substitution nor opens a quote, and a ``)``
    ending an unparenthesized ``case`` arm pattern ``pat)`` does not close
    the enclosing ``$(...)``.
    """
    mc = _MASK_COMMENTS if mask_comments is None else mask_comments
    kind = ("heredoc", mc, _COMMANDS_READING.get())
    key, offset, cached = _match_lookup(command, open_paren, kind)
    if cached is not None:
        return cached
    if command.startswith("((", open_paren):
        candidate = _subst_end(command, open_paren)
        if _is_arithmetic(command, open_paren, candidate):
            return _match_return(key, offset, command, candidate)
    depth = 1
    opened: dict[int, int] = {}
    brace_depth = 0
    i = open_paren + 1
    n = len(command)
    at_word_start = True
    pending: deque[tuple[str, bool]] = deque()
    # Stack of [paren_depth_at_case, state] where state is one of:
    # "pre_in", "expect_pat", "in_pat", "in_pat_open", "in_body".
    case_stack: list[list] = []
    while i < n and depth:
        _charge_scan()
        c = command[i]
        if c == "\n":
            i += 1
            at_word_start = True
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "pre_in":
                pass
            while pending:
                _charge_scan()
                delim, strip_tabs = pending.popleft()
                while i < n:
                    _charge_scan()
                    nl = _scan_find(command, "\n", i)
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
            end = _scan_find(command, "\n", i)
            i = n if end < 0 else end
            continue
        if c == "\\" and i + 1 < n:
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
            i += 2
            at_word_start = False
            continue
        if command.startswith("${", i):
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
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
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
            at_word_start = False
            _, i, _ = _unescape_ansi_c(command, i + 2)
            continue
        if c == "'":
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
            at_word_start = False
            j = _scan_find(command, "'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c in ('"', "`"):
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
            at_word_start = False
            i = _quote_end(command, i, mask_comments=mc) + 1
            continue
        if c == "<" and not (i > 0 and command[i - 1] == "<"):
            if command.startswith("<<<", i):
                i += 3
                at_word_start = True
                continue
            hd = _parse_heredoc_op(command, i)
            if hd is not None:
                delim, strip_tabs, _, end_op, bad_delim = hd
                if not bad_delim:
                    pending.append((delim, strip_tabs))
                i = end_op
                at_word_start = False
                continue
            i += 1
            at_word_start = True
            continue
        if c in " \t":
            i += 1
            at_word_start = True
            continue
        if c == ";" and case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "in_body":
            if command.startswith(";;&", i):
                case_stack[-1][1] = "expect_pat"
                i += 3
                at_word_start = True
                continue
            if command.startswith(";;", i) or command.startswith(";&", i):
                case_stack[-1][1] = "expect_pat"
                i += 2
                at_word_start = True
                continue
        if c in ";|&>":
            i += 1
            at_word_start = True
            continue
        if c == "(":
            # Reuse only substitutions entered with this matcher's initial parser state.
            cacheable = (key is not None and brace_depth == 0 and not case_stack and not pending
                         and i > 0 and command[i - 1] == "$" and not command.startswith("((", i))
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat_open"
            depth += 1
            if cacheable:
                opened[depth] = i
            at_word_start = True
            i += 1
            continue
        if c == ")":
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "in_pat":
                case_stack[-1][1] = "in_body"
                at_word_start = True
                i += 1
                continue
            if case_stack and case_stack[-1][0] + 1 == depth and case_stack[-1][1] == "in_pat_open":
                depth -= 1
                case_stack[-1][1] = "in_body"
                at_word_start = True
                i += 1
                continue
            opening = opened.pop(depth, None)
            if opening is not None:
                _match_return((*key[:-1], offset + opening), offset, command, i)
            depth -= 1
            at_word_start = True
            if depth == 0:
                return _match_return(key, offset, command, i)
            i += 1
            continue
        if at_word_start and brace_depth == 0:
            if case_stack and case_stack[-1][0] == depth:
                st = case_stack[-1][1]
                if st in ("expect_pat", "in_body") and _ESAC_WORD_RE.match(command, i):
                    case_stack.pop()
                    i += 4
                    at_word_start = False
                    continue
                if st == "pre_in" and _IN_WORD_RE.match(command, i):
                    case_stack[-1][1] = "expect_pat"
                    i += 2
                    at_word_start = False
                    continue
            if _CASE_WORD_RE.match(command, i):
                case_stack.append([depth, "pre_in"])
                i += 4
                at_word_start = False
                continue
            if case_stack and case_stack[-1][0] == depth and case_stack[-1][1] == "expect_pat":
                case_stack[-1][1] = "in_pat"
        at_word_start = False
        i += 1
    return _match_return(key, offset, command, n)


def _arithmetic_span(command: str, pos: int, *, mask_comments: bool) -> Optional[tuple[int, int]]:
    """Body-end and stop of a standalone ((...)) or legacy $[...] expression."""
    n = len(command)
    if command.startswith("((", pos):
        if _BOUNDED_COMMANDS.get() and (pos == 0 or command[pos - 1] != "$"):
            end = _subst_end(command, pos)
            if end < n and _subst_end(command, pos + 1) == end - 1:
                return end - 1, end + 1
        end = _subst_end_hd(command, pos, mask_comments=mask_comments)
        return (end - 1, end + 1) if _is_arithmetic(command, pos, end) else None
    if not command.startswith("$[", pos):
        return None
    depth, i = 1, pos + 2
    while i < n:
        _charge_scan()
        if command[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if command[i] in "'\"`":
            i = _quote_end(command, i, mask_comments=mask_comments) + 1
            continue
        if command.startswith("$(", i):
            i = _subst_end_hd(command, i + 1, mask_comments=mask_comments) + 1
            continue
        if command[i] == "[":
            depth += 1
        elif command[i] == "]":
            depth -= 1
            if depth == 0:
                return i, i + 1
        i += 1
    return n, n


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
    unterminated_heredoc: bool = False
    heredoc_shell_texts: tuple[str, ...] = ()


@_bounded_parser
def _fold_bash_continuations(
    command: str,
    *,
    mask_comments: bool,
    fold_continuations_in_comments: bool,
    arithmetic: bool = False,
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
    if _plain_bash(command):
        return command
    n = len(command)
    at_word_start = True
    brace_depth = 0
    pending: deque[tuple[str, bool]] = deque()
    while i < n:
        _charge_scan()
        c = command[i]
        if c == "\n":
            out.append("\n")
            i += 1
            at_word_start = True
            while pending:
                _charge_scan()
                delim, strip_tabs = pending.popleft()
                while i < n:
                    _charge_scan()
                    nl = _scan_find(command, "\n", i)
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
                end = _scan_find(command, "\n", i)
                end = n if end < 0 else end
                out.append(command[i:end])
                i = end
                continue
            j = i
            while j < n:
                _charge_scan()
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
        if command.startswith("$((", i):
            end = _subst_end_hd(command, i + 1, mask_comments=mask_comments)
            if _is_arithmetic(command, i + 1, end):
                inner = _fold_bash_continuations(
                    _subspan(command, i + 2, end), mask_comments=mask_comments,
                    fold_continuations_in_comments=fold_continuations_in_comments,
                    arithmetic=True,
                )
                out.append("$(" + inner + ")")
                i = end + 1
                at_word_start = False
                continue
        math = (_arithmetic_span(command, i, mask_comments=mask_comments)
                if not arithmetic and c in "($" and not (i > 0 and command[i - 1] == "(")
                else None)
        if math is not None:
            body_end, stop = math
            inner = _fold_bash_continuations(
                _subspan(command, i + 2, body_end), mask_comments=mask_comments,
                fold_continuations_in_comments=fold_continuations_in_comments,
                arithmetic=not (_BOUNDED_COMMANDS.get() and command.startswith("((", i)),
            )
            out.append(command[i:i + 2] + inner + command[body_end:stop])
            i = stop
            at_word_start = False
            continue
        if c == "<" and not arithmetic:
            if command.startswith("<<<", i):
                out.append("<<<")
                i += 3
                at_word_start = True
                continue
            hd = None if (i > 0 and command[i - 1] == "<") \
                else _parse_heredoc_op(command, i)
            if hd is not None:
                delim, strip_tabs, _, end_op, bad_delim = hd
                if not bad_delim:
                    pending.append((delim, strip_tabs))
                out.append(command[i:end_op])
                i = end_op
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
        if not arithmetic and c == "$" and i + 1 < n and command[i + 1] == "'":
            at_word_start = False
            j = i + 2
            while j < n:
                _charge_scan()
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
        if c == "'" and not arithmetic:
            at_word_start = False
            j = _scan_find(command, "'", i + 1)
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
                _charge_scan()
                run_end = _quote_run_end(command, i)
                if run_end > i:
                    out.append(_subspan(command, i, run_end))
                    i = run_end
                    continue
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
                        _subspan(command, i + 2, end),
                        mask_comments=mask_comments,
                        fold_continuations_in_comments=fold_continuations_in_comments,
                        arithmetic=_is_arithmetic(command, i + 1, end),
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
    folded = "".join(out)
    if folded is not command:
        _charge_scan(len(folded))
    work = _ROUTING_WORK.get()
    if work is not None and folded == command:
        root, offset = _span_origin(command)
        work.cache[("origin", id(folded))] = (folded, root, offset)
    return folded


def _closed_assignment_value_end(text: str, i: int, *,
                                 mask_comments: bool) -> Optional[int]:
    """End of a closed assignment value word starting at ``i`` in ``text``.

    Handles concatenated bare chars, ``'...'``, ``$'...'``, ``"..."``,
    ``$"..."``, ``$(...)``, ``$((...))``, ``${...}``, and backticks `` `...` ``.
    Returns ``None`` if any quote or substitution in the value is unclosed.
    """
    n = len(text)
    while i < n:
        _charge_scan()
        c = text[i]
        if c in " \t;\n&|()<>":
            break
        if text.startswith("$(", i):
            end = _subst_end_hd(text, i + 1, mask_comments=mask_comments)
            if end >= n:
                return None
            i = end + 1
            continue
        if text.startswith("$[", i):
            _, i = _arithmetic_span(text, i, mask_comments=mask_comments)
            continue
        if text.startswith("${", i):
            end = _scan_find(text, "}", i + 2)
            if end < 0:
                return None
            i = end + 1
            continue
        if c == "`":
            j = _scan_find(text, "`", i + 1)
            if j < 0:
                return None
            i = j + 1
            continue
        if text.startswith("$'", i):
            j = i + 2
            while j < n:
                _charge_scan()
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
        if c == "'":
            j = _scan_find(text, "'", i + 1)
            if j < 0:
                return None
            i = j + 1
            continue
        if c == '"' or text.startswith('$"', i):
            j = _quote_end(text, i + (1 if c == "$" else 0), mask_comments=mask_comments)
            if j >= n:
                return None
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return i


def _normalize_assignment_values(
    folded: str,
    visible: str,
    *,
    mask_comments: bool,
    quoted_gh_words: tuple[tuple[int, int], ...] = (),
) -> str:
    """Collapse closed env-prefix values (``$(...)``, ``$((...))``, `` `...` ``,
    quotes) into ``x`` runs of identical length so ``_ENVPFX`` never hides a
    following ``gh`` call, and normalize quote/escape-removed ``gh`` command
    words at command start positions.
    """
    if "=" not in visible and not quoted_gh_words:
        return visible
    chars = list(visible)
    _charge_scan(len(chars))
    n = len(visible)
    gh_inv = _GH_INVOCATION if _ENABLE_BANG_AND_ENV_ASSIGN else _GH_INVOCATION_LEGACY
    if "=" in visible:
        i = 0
        while i < n:
            _charge_scan()
            if i == 0 or visible[i - 1] in " \t\n;|&({":
                am = _ASSIGN_NAME_AT.match(visible, i)
                if am is not None:
                    vstart = am.end()
                    vend = _closed_assignment_value_end(
                        folded, vstart, mask_comments=mask_comments)
                    if vend is not None and vend > vstart:
                        span_vis = "".join(chars[vstart:vend])
                        _charge_scan(len(span_vis))
                        if not (gh_inv.search(span_vis) or any(
                            pattern.search(span_vis)
                            for pattern in (_SHELL_AT, _EVAL_AT, _WATCH_AT, _XARGS_AT)
                        ) or ("|" in span_vis and _ECHO_AT.search(span_vis))):
                            for k in range(vstart, vend):
                                _charge_scan()
                                chars[k] = "x"
                        i = vend
                        continue
            i += 1
    if quoted_gh_words:
        at_word = _AT_WORD if _ENABLE_BANG_AND_ENV_ASSIGN else _AT_WORD_LEGACY
        cur = "".join(chars)
        _charge_scan(len(cur))
        for wstart, wend in quoted_gh_words:
            _charge_scan()
            if wend - wstart >= 2 and at_word.search(_subspan(cur, 0, wstart)) is not None:
                rep = "gh" + (" " * (wend - wstart - 2))
                for k, ch in enumerate(rep):
                    _charge_scan()
                    chars[wstart + k] = ch
                cur = "".join(chars)
                _charge_scan(len(cur))
    return "".join(chars)


def _scan_subst_only_for_targets(segment: str, *, mask_comments: bool) -> str:
    """Blank ``segment`` while preserving any ``$(...)`` or `` `...` `` inside it."""
    if "$(" not in segment and "`" not in segment:
        return _blanks(segment)
    res: list[str] = []
    k = 0
    m = len(segment)
    while k < m:
        _charge_scan()
        if segment[k] == "\\" and k + 1 < m:
            res.append("  ")
            k += 2
            continue
        if segment.startswith("$(", k):
            end = _subst_end_hd(segment, k + 1, mask_comments=mask_comments)
            stop = end + 1 if end < m else m
            sub = _scan_folded(_subspan(segment, k + 2, end), 0, mask_comments=mask_comments)
            close_p = ")" if end < m else ""
            res.append("$(" + sub.targets_visible + close_p)
            k = stop
            continue
        if segment[k] == "`":
            j = _scan_find(segment, "`", k + 1)
            if j < 0:
                sub = _scan_folded(_subspan(segment, k + 1, m), 0, mask_comments=mask_comments)
                res.append("`" + sub.targets_visible)
                k = m
                continue
            sub = _scan_folded(_subspan(segment, k + 1, j), 0, mask_comments=mask_comments)
            res.append("`" + sub.targets_visible + "`")
            k = j + 1
            continue
        res.append("\n" if segment[k] == "\n" else " ")
        k += 1
    return "".join(res)


_SCRIPT_RUNNERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh",
    "python", "python2", "python3",
    ".", "source",
})


def _normalize_script_path(path: str) -> str:
    p = path.strip()
    start = 0
    while p.startswith("./", start):
        _charge_scan()
        start += 2
    return p[start:]


def _split_top_level_segments(text: str) -> list[tuple[int, int, str]]:
    """Split ``text`` into simple-command segments outside quotes and comments."""
    work = _ROUTING_WORK.get()
    if work is not None:
        _span_origin(text)
        key = ("segments", id(text), _MASK_COMMENTS)
        if key in work.cache:
            return work.cache[key]
    segs = list(_iter_top_level_segments(text))
    if work is not None:
        work.cache[key] = segs
    return segs


def _iter_top_level_segments(text: str, start: int = 0):
    i = start
    n = len(text)
    seg_start = start
    at_word_start = True
    while i < n:
        _charge_scan()
        c = text[i]
        if c in "\n;|&(){}":
            if i > seg_start:
                yield seg_start, i, _subspan(text, seg_start, i)
            i += 1
            seg_start = i
            at_word_start = True
            continue
        if _MASK_COMMENTS and at_word_start and c == "#":
            if i > seg_start:
                yield seg_start, i, _subspan(text, seg_start, i)
            end = _scan_find(text, "\n", i)
            i = n if end < 0 else end + 1
            seg_start = i
            at_word_start = True
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            at_word_start = False
            continue
        if text.startswith("$'", i):
            _, i, _ = _unescape_ansi_c(text, i + 2)
            at_word_start = False
            continue
        if c == "'":
            k = _scan_find(text, "'", i + 1)
            i = n if k < 0 else k + 1
            at_word_start = False
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                _charge_scan()
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i = i + 1 if i < n else n
            at_word_start = False
            continue
        if c in " \t":
            i += 1
            at_word_start = True
            continue
        i += 1
        at_word_start = False
    if n > seg_start:
        yield seg_start, n, _subspan(text, seg_start, n)


def _extract_simple_cmd_words_and_redirs(
    seg: str,
) -> tuple[list[str], set[str]]:
    """Extract ``(command_words, redirected_output_paths)`` from one simple command."""
    words: list[str] = []
    redirs: set[str] = set()
    j = 0
    n = len(seg)
    while j < n:
        _charge_scan()
        while j < n and seg[j] in " \t":
            _charge_scan()
            j += 1
        if j >= n:
            break
        if _MASK_COMMENTS and seg[j] == "#":
            break
        if seg.startswith("<<<", j):
            j += 3
            while j < n and seg[j] in " \t":
                _charge_scan()
                j += 1
            _, wend, _ = _unquote_word_at(seg, j)
            j = max(j + 1, wend)
            continue
        hd = _parse_heredoc_op(seg, j)
        if hd is not None:
            j = hd[3]
            continue
        rm = _OUTPUT_REDIRECT_AT.match(seg, j)
        if rm is not None:
            j = rm.end()
            while j < n and seg[j] in " \t":
                _charge_scan()
                j += 1
            if j < n:
                unq, wend, _ = _unquote_word_at(seg, j)
                target = unq if unq is not None else seg[j:wend]
                norm = _normalize_script_path(target)
                if norm and norm not in ("/dev/null", "-"):
                    redirs.add(norm)
                j = max(j + 1, wend)
            continue
        om = _FD_REDIRECT_AT.match(seg, j)
        if om is not None:
            j = om.end()
            if not seg[j - 1].isdigit() and seg[j - 1] != "-":
                while j < n and seg[j] in " \t":
                    _charge_scan()
                    j += 1
                if j < n:
                    _, wend, _ = _unquote_word_at(seg, j)
                    j = max(j + 1, wend)
            continue
        unq, wend, _ = _unquote_word_at(seg, j)
        if wend <= j:
            j += 1
            continue
        w = unq if unq is not None else seg[j:wend]
        words.append(w)
        j = wend
    idx = 0
    while idx < len(words):
        _charge_scan()
        w = words[idx]
        if w in _LEAD_KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            idx += 1
            continue
        if w in _WRAPPER_WORDS:
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                _charge_scan()
                idx += 1
            continue
        break
    rem = words[idx:]
    if rem and rem[0].rsplit("/", 1)[-1] == "tee":
        for arg in rem[1:]:
            _charge_scan()
            if not arg.startswith("-"):
                norm = _normalize_script_path(arg)
                if norm and norm not in ("/dev/null", "-"):
                    redirs.add(norm)
    return rem, redirs


def _executed_paths(segment: str):
    words, _ = _extract_simple_cmd_words_and_redirs(segment)
    if words:
        yield _normalize_script_path(words[0])
        if words[0].rsplit("/", 1)[-1] in _SCRIPT_RUNNERS:
            yield from (_normalize_script_path(arg) for arg in words[1:])


def _later_executes_paths(text: str, start: int, paths: set[str]) -> bool:
    work = _ROUTING_WORK.get()
    if work is not None:
        _span_origin(text)
        key = ("executions", id(text), _MASK_COMMENTS,
               _COMMANDS_READING.get(), _BOUNDED_COMMANDS.get())
        saved = work.cache.get(key)
        if saved is not None and start in saved[0]:
            for path in sorted(paths):
                executed = saved[1].get(path, -1) >= start
                _charge_scan(_CACHE_WORK)
                if executed:
                    return True
            return False
    boundaries = {start, len(text)}
    last_use: dict[str, int] = {}
    for begin, end, segment in _iter_top_level_segments(text, start):
        _charge_scan()
        boundaries.add(begin)
        if end < len(text) and text[end] in "\n;|&(){}":
            boundaries.add(end + 1)
        for path in _executed_paths(segment):
            _charge_scan()
            if path in paths:
                return True
            last_use[path] = begin
    if work is not None:
        work.cache[key] = boundaries, last_use
    return False


def _heredoc_writes_executed_file(
    text: str, op_start: int, after_heredoc_pos: int
) -> bool:
    """True when the heredoc at ``op_start`` writes to a file path (via ``>``,
    ``>>``, or ``tee``) and that path later appears in the same command as a
    command word or as an argument of ``bash``/``sh``/``zsh``/``python``/``.``/``source``.
    """
    line_start = _previous_separator(text, op_start, "\n") + 1
    line_end = _next_separator(text, op_start, "\n")
    if line_end < 0:
        line_end = len(text)
    line_segs = _split_top_level_segments(_subspan(text, line_start, line_end))
    rel_op = op_start - line_start
    owner_idx = -1
    for idx, (s, e, _) in enumerate(line_segs):
        _charge_scan()
        if s <= rel_op <= e:
            owner_idx = idx
            break
    if owner_idx < 0:
        return False
    work = _ROUTING_WORK.get()
    paths_key = ("heredoc-paths", id(line_segs), owner_idx,
                 _COMMANDS_READING.get(), _BOUNDED_COMMANDS.get())
    written_paths = work.cache.get(paths_key) if work is not None else None
    if written_paths is None:
        _, written_paths = _extract_simple_cmd_words_and_redirs(line_segs[owner_idx][2])
        if owner_idx + 1 < len(line_segs):
            next_rem, next_redirs = _extract_simple_cmd_words_and_redirs(line_segs[owner_idx + 1][2])
            if next_rem and next_rem[0].rsplit("/", 1)[-1] == "tee":
                written_paths |= next_redirs
        if work is not None:
            work.cache[paths_key] = frozenset(written_paths)
    if not written_paths:
        return False
    for _, _, lseg in line_segs[owner_idx + 1:]:
        _charge_scan()
        if any(path in written_paths for path in _executed_paths(lseg)):
            return True
    return _later_executes_paths(text, after_heredoc_pos, written_paths)


def _heredoc_feeds_shell(text: str, op_start: int) -> bool:
    """True when the simple command owning ``<<`` / ``<<<`` at ``op_start`` invokes a shell."""
    seg_start = _previous_separator(text, op_start, ";\n&|(){}") + 1
    j = seg_start
    while j < op_start:
        _charge_scan()
        while j < op_start and text[j] in " \t":
            _charge_scan()
            j += 1
        if j >= op_start:
            break
        if text[j] in "<>":
            hd = _parse_heredoc_op(text, j)
            if hd is not None:
                j = hd[3]
                continue
            j += 1
            continue
        unq, wend, _ = _unquote_word_at(text, j)
        if wend <= j:
            j += 1
            continue
        w = unq if unq is not None else text[j:wend]
        j = wend
        if w in _LEAD_KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            continue
        if w in _WRAPPER_WORDS:
            continue
        if w.startswith("-"):
            continue
        return w.rsplit("/", 1)[-1] in _SHELLS
    return False


@_bounded_parser
def _scan_folded(text: str, base: int = 0, *, mask_comments: bool = True,
                 arithmetic: bool = False) -> BashScan:
    """Single-pass bash scanner on continuation-folded text."""
    if _plain_bash(text):
        return _tokenize_bash(text)
    out: list[str] = []
    out_targets: list[str] = []
    i = 0
    n = len(text)
    at_word_start = True
    brace_depth = 0
    pending: deque[tuple[str, bool, bool, bool, int]] = deque()
    unterminated = False
    unterminated_hd = False
    has_tick = False
    has_hd = False
    spans: list[tuple[int, int]] = []
    quoted_gh_words: list[tuple[int, int]] = []
    heredoc_shell_texts: list[str] = []

    while i < n:
        _charge_scan()
        c = text[i]
        if c == "\n":
            out.append("\n")
            out_targets.append("\n")
            i += 1
            at_word_start = True
            while pending:
                _charge_scan()
                delim, strip_tabs, quoted_delim, feeds_shell, op_pos = pending.popleft()
                body_lines: list[str] = []
                # Truncated inner heredocs must not invent a newline and shift later metadata.
                body_has_nl = False
                term_line: Optional[str] = None
                term_has_nl = False
                while i < n:
                    _charge_scan()
                    nl = _scan_find(text, "\n", i)
                    lend = n if nl < 0 else nl
                    line = text[i:lend]
                    if _is_terminator(line, delim, strip_tabs):
                        term_line = line
                        term_has_nl = nl >= 0
                        i = lend + 1 if nl >= 0 else n
                        break
                    body_lines.append(line)
                    body_has_nl = nl >= 0
                    if nl < 0:
                        i = n
                        break
                    i = nl + 1
                if term_line is None:
                    unterminated_hd = True
                scan_as_shell = feeds_shell or (
                    _SCAN_WRITE_THEN_RUN
                    and _heredoc_writes_executed_file(text, op_pos, i)
                )
                if scan_as_shell and body_lines:
                    body_raw = "\n".join(body_lines)
                    body_folded = _fold_bash_continuations(
                        body_raw,
                        mask_comments=mask_comments,
                        fold_continuations_in_comments=_FOLD_CONTINUATIONS_IN_COMMENTS,
                    )
                    sub = _scan_folded(body_folded, 0, mask_comments=mask_comments)
                    unterminated |= sub.unterminated_quote
                    unterminated_hd |= sub.unterminated_heredoc
                    has_tick |= sub.has_backtick
                    has_hd |= sub.has_heredoc
                    heredoc_shell_texts.extend(sub.heredoc_shell_texts)
                    # Parse against this body's own folded offsets, not its parent's.
                    heredoc_shell_texts.extend(_shell_texts(body_folded, sub.targets_visible))
                    out.append(_blanks(body_raw))
                    # Folding inside this body must not shift later source offsets.
                    out_targets.append(sub.targets_visible.ljust(len(body_raw)))
                    if body_has_nl:
                        out.append("\n")
                        out_targets.append("\n")
                else:
                    # Substitutions can span lines; ordinary heredoc text stays masked.
                    body_raw = "\n".join(body_lines)
                    out.append(_blanks(body_raw))
                    out_targets.append(
                        _blanks(body_raw) if quoted_delim else
                        _scan_subst_only_for_targets(body_raw, mask_comments=mask_comments)
                    )
                    if body_has_nl:
                        out.append("\n")
                        out_targets.append("\n")
                if term_line is not None:
                    out.append(term_line)
                    out_targets.append(term_line)
                    if term_has_nl:
                        out.append("\n")
                        out_targets.append("\n")
            continue
        if mask_comments and at_word_start and brace_depth == 0 and c == "#":
            end = _scan_find(text, "\n", i)
            end = n if end < 0 else end
            seg = text[i:end]
            blank_seg = _blanks(seg)
            out.append(blank_seg)
            if _SCAN_COMMENTS_FOR_TARGETS:
                out_targets.append(
                    _scan_subst_only_for_targets(seg, mask_comments=mask_comments)
                )
            else:
                out_targets.append(blank_seg)
            i = end
            continue
        if at_word_start and brace_depth == 0 and c not in " \t\n;|&()<>#":
            unq, wend, had_qe = _unquote_word_at(text, i)
            if had_qe and unq is not None and _GH_EXACT.match(unq):
                quoted_gh_words.append((i, wend))
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
        math = (_arithmetic_span(text, i, mask_comments=mask_comments)
                if not arithmetic and c in "($" and not (i > 0 and text[i - 1] == "(")
                else None)
        if math is not None:
            body_end, stop = math
            sub = _scan_folded(_subspan(text, i + 2, body_end), base + i + 2,
                               mask_comments=mask_comments,
                               arithmetic=not (_BOUNDED_COMMANDS.get() and text.startswith("((", i)))
            unterminated |= sub.unterminated_quote
            unterminated_hd |= sub.unterminated_heredoc
            has_tick |= sub.has_backtick
            has_hd |= sub.has_heredoc
            spans.extend(sub.subst_spans)
            heredoc_shell_texts.extend(sub.heredoc_shell_texts)
            out.append(text[i:i + 2] + sub.visible + text[body_end:stop])
            out_targets.append(text[i:i + 2] + sub.targets_visible + text[body_end:stop])
            i = stop
            at_word_start = False
            continue
        if c == "<" and not arithmetic:
            if text.startswith("<<<", i):
                has_hd = True
                feeds_sh = _SCAN_SHELL_HEREDOC and _heredoc_feeds_shell(text, i)
                out.append("<<<")
                out_targets.append("<<<")
                i += 3
                if feeds_sh:
                    while i < n and text[i] in " \t":
                        _charge_scan()
                        out.append(text[i])
                        out_targets.append(text[i])
                        i += 1
                    unq, wend, _ = _unquote_word_at(text, i)
                    if unq is not None and wend > i:
                        sub = _scan_folded(
                            _fold_bash_continuations(
                                unq,
                                mask_comments=mask_comments,
                                fold_continuations_in_comments=_FOLD_CONTINUATIONS_IN_COMMENTS,
                            ),
                            0,
                            mask_comments=mask_comments,
                        )
                        heredoc_shell_texts.extend(sub.heredoc_shell_texts)
                        out.append(_blanks(text[i:wend]))
                        out_targets.append("(" + sub.targets_visible + ")")
                        i = wend
                        at_word_start = False
                        continue
                at_word_start = True
                continue
            hd = None if (i > 0 and text[i - 1] == "<") \
                else _parse_heredoc_op(text, i)
            if hd is not None:
                has_hd = True
                delim, strip_tabs, quoted_delim, end_op, bad_delim = hd
                if bad_delim:
                    unterminated_hd = True
                else:
                    feeds_sh = _SCAN_SHELL_HEREDOC and _heredoc_feeds_shell(text, i)
                    pending.append((delim, strip_tabs, quoted_delim, feeds_sh, i))
                op_text = text[i:end_op]
                out.append(op_text)
                out_targets.append(op_text)
                i = end_op
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
        if not arithmetic and c == "$" and i + 1 < n and text[i + 1] == "'":
            at_word_start = False
            j = i + 2
            while j < n:
                _charge_scan()
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
        if c == "'" and not arithmetic:
            at_word_start = False
            j = _scan_find(text, "'", i + 1)
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
                _charge_scan()
                run_end = _quote_run_end(text, i)
                if run_end > i:
                    blank = _blanks(_subspan(text, i, run_end))
                    out.append(blank)
                    out_targets.append(blank)
                    i = run_end
                    continue
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
                        _subspan(text, i + 2, end), base + i + 2, mask_comments=mask_comments,
                        arithmetic=_is_arithmetic(text, i + 1, end))
                    unterminated |= sub.unterminated_quote
                    unterminated_hd |= sub.unterminated_heredoc
                    has_tick |= sub.has_backtick
                    has_hd |= sub.has_heredoc
                    spans.extend(sub.subst_spans)
                    heredoc_shell_texts.extend(sub.heredoc_shell_texts)
                    close_p = ")" if end < n else ""
                    out.append("$(" + sub.visible + close_p)
                    out_targets.append("$(" + sub.targets_visible + close_p)
                    i = stop
                    continue
                if text[i] == "`":
                    subst_seen = True
                    has_tick = True
                    j = _scan_find(text, "`", i + 1)
                    if j < 0:
                        unterminated = True
                        tail = _blanks(text[i:])
                        out.append(tail)
                        out_targets.append(tail)
                        i = n
                        break
                    sub = _scan_folded(
                        _subspan(text, i + 1, j), base + i + 1, mask_comments=mask_comments)
                    unterminated |= sub.unterminated_quote
                    unterminated_hd |= sub.unterminated_heredoc
                    has_hd |= sub.has_heredoc
                    spans.extend(sub.subst_spans)
                    heredoc_shell_texts.extend(sub.heredoc_shell_texts)
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
                _subspan(text, i + 2, end), base + i + 2, mask_comments=mask_comments,
                arithmetic=_is_arithmetic(text, i + 1, end))
            unterminated |= sub.unterminated_quote
            unterminated_hd |= sub.unterminated_heredoc
            has_tick |= sub.has_backtick
            has_hd |= sub.has_heredoc
            spans.extend(sub.subst_spans)
            heredoc_shell_texts.extend(sub.heredoc_shell_texts)
            close_p = ")" if end < n else ""
            out.append("$(" + sub.visible + close_p)
            out_targets.append("$(" + sub.targets_visible + close_p)
            i = stop
            continue
        if c == "`":
            at_word_start = False
            has_tick = True
            j = _scan_find(text, "`", i + 1)
            if j < 0:
                sub = _scan_folded(
                    _subspan(text, i + 1, n), base + i + 1, mask_comments=mask_comments)
                unterminated |= sub.unterminated_quote
                unterminated_hd |= sub.unterminated_heredoc
                has_hd |= sub.has_heredoc
                spans.extend(sub.subst_spans)
                heredoc_shell_texts.extend(sub.heredoc_shell_texts)
                out.append("`" + sub.visible)
                out_targets.append("`" + sub.targets_visible)
                i = n
                continue
            sub = _scan_folded(
                _subspan(text, i + 1, j), base + i + 1, mask_comments=mask_comments)
            unterminated |= sub.unterminated_quote
            unterminated_hd |= sub.unterminated_heredoc
            has_hd |= sub.has_heredoc
            spans.extend(sub.subst_spans)
            heredoc_shell_texts.extend(sub.heredoc_shell_texts)
            out.append("`" + sub.visible + "`")
            out_targets.append("`" + sub.targets_visible + "`")
            i = j + 1
            continue
        out.append(c)
        out_targets.append(c)
        i += 1
        at_word_start = False

    if pending:
        unterminated_hd = True
    visible = "".join(out)
    targets_visible = _normalize_assignment_values(
        text,
        "".join(out_targets),
        mask_comments=mask_comments,
        quoted_gh_words=tuple(quoted_gh_words),
    )
    return BashScan(
        folded=text,
        visible=visible,
        targets_visible=targets_visible,
        unterminated_quote=unterminated,
        has_backtick=has_tick,
        has_heredoc=has_hd,
        subst_spans=tuple(spans),
        unterminated_heredoc=unterminated_hd,
        heredoc_shell_texts=tuple(heredoc_shell_texts),
    )


_PLAIN_BASH = re.compile(r"[A-Za-z0-9 _./:,@+-]*")


def _plain_bash(command: str) -> bool:
    if _PLAIN_BASH.fullmatch(command):
        _charge_scan(len(command))
        return True
    return False


def _tokenize_bash(
    command: str,
    *,
    mask_comments: Optional[bool] = None,
    fold_continuations_in_comments: Optional[bool] = None,
) -> BashScan:
    state = _HOOK_STATE.get()
    if state is None:
        return _tokenize_bash_uncached(command, mask_comments=mask_comments,
            fold_continuations_in_comments=fold_continuations_in_comments)
    key = (command, mask_comments, fold_continuations_in_comments,
           _COMMANDS_READING.get(), _BOUNDED_COMMANDS.get())
    if key not in state["scans"]:
        state["scans"][key] = _tokenize_bash_uncached(command, mask_comments=mask_comments,
            fold_continuations_in_comments=fold_continuations_in_comments)
    return state["scans"][key]


def _tokenize_bash_uncached(
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
    - Heredoc delimiters are parsed as bash words (with quote removal); an
      unterminated heredoc sets ``unterminated_heredoc=True`` so the command is
      denied rather than treated as not-gh.
    - ``$(...)``, backticks `` `...` ``, and arithmetic ``$((...))`` inside an
      env-prefix value are normalized in ``targets_visible`` so they never hide
      a following ``gh`` call.
    """
    mc = _MASK_COMMENTS if mask_comments is None else mask_comments
    command = command or ""
    # Without syntax there is no folding, masking, or substitution metadata to collect.
    if _plain_bash(command):
        return BashScan(
            folded=command, visible=command,
            targets_visible=command,
            unterminated_quote=False, has_backtick=False, has_heredoc=False, subst_spans=(),
        )
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
    rf"{_START}{_LEAD}{_CMD_PREFIX}\Z", re.MULTILINE)
_AT_WORD_LEGACY = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}\Z", re.MULTILINE)
_SHELL_BIN = r"(?:(?:/[\w.-]+)*/)?(?:bash|zsh|dash|sh)"
_SHELL_AT = re.compile(
    rf"{_START}{_LEAD}{_CMD_PREFIX}(?P<bin>{_SHELL_BIN})(?=[ \t]|$)",
    re.MULTILINE)
_EVAL_AT = re.compile(
    rf"{_START}{_LEAD}{_CMD_PREFIX}(?:(?:/[\w.-]+)*/)?eval(?=[ \t]|$)",
    re.MULTILINE)
_WATCH_AT = re.compile(
    rf"{_START}{_LEAD}{_CMD_PREFIX}(?:(?:/[\w.-]+)*/)?watch(?=[ \t]|$)",
    re.MULTILINE)
_XARGS_AT = re.compile(
    rf"{_START}{_LEAD}{_CMD_PREFIX}(?:(?:/[\w.-]+)*/)?xargs(?=[ \t]|$)",
    re.MULTILINE)
_ECHO_AT = re.compile(rf"{_START}{_LEAD}{_CMD_PREFIX}echo(?=[ \t]|$)", re.MULTILINE)
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
    n = len(command)
    if _RESOLVE_ANSI_C and command.startswith("$'", i):
        val, end, _ = _unescape_ansi_c(command, i + 2)
        return val, end
    if command.startswith('$"', i):
        i += 1
    q = command[i]
    j = i + 1
    if q == "'":
        k = _scan_find(command, "'", j)
        if k < 0:
            return command[j:], n
        return command[j:k], k + 1
    buf: list[str] = []
    while j < n and command[j] != '"':
        _charge_scan()
        run_end = _quote_run_end(command, j)
        if run_end > j:
            buf.append(_subspan(command, j, run_end))
            j = run_end
            continue
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
        _charge_scan()
        i += 1
    if i >= n:
        return None
    if _MASK_COMMENTS and command[i] == "#":
        end = _scan_find(command, "\n", i)
        end = n if end < 0 else end + 1
        return ("sep", i, end, "")
    if command[i] in ";\n&|":
        return ("sep", i, i + 1, "")
    if (_RESOLVE_ANSI_C and command.startswith("$'", i)) or command.startswith('$"', i):
        body, end = _quoted_body(command, i)
        return ("word", i, end, body)
    if command[i] in "'\"":
        body, end = _quoted_body(command, i)
        return ("word", i, end, body)
    buf: list[str] = []
    j = i
    while j < n and command[j] not in " \t;\n&|":
        _charge_scan()
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
    if _PLAIN_BASH.fullmatch(command, i):
        _charge_scan(len(command) - i)
        tail = _subspan(command, i, len(command))
        words = tail.split()
        _charge_scan(len(tail))
        first = 0
        while first < len(words) and words[first] == "--":
            _charge_scan()
            first += 1
        joined = " ".join(words[first:])
        _charge_scan(len(joined))
        return joined or None
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


def _echo_pipeline_text(command: str, i: int) -> Optional[str]:
    """Only constant echo words into a shell on stdin, never dynamic expansion."""
    parts: list[str] = []
    n = len(command)
    while i < n:
        _charge_scan()
        while i < n and command[i] in " \t":
            _charge_scan()
            i += 1
        if i < n and command[i] == "|":
            break
        word, end, _ = _unquote_word_at(command, i)
        if word is None or end <= i or (not parts and word.startswith("-")):
            return None
        parts.append(word)
        i = end
    if not parts or i >= n or command.startswith("||", i):
        return None
    i += 1
    while i < n and command[i] in " \t":
        _charge_scan()
        i += 1
    shell, i, _ = _unquote_word_at(command, i)
    if shell is None or not _is_shell_word(shell):
        return None
    while i < n and command[i] in " \t":
        _charge_scan()
        i += 1
    if command.startswith("-s", i):
        i += 2
        while i < n and command[i] in " \t":
            _charge_scan()
            i += 1
    if i < n and not (command[i] in ";\n&|)>#" or _FD_OUTPUT_AT.match(command, i)):
        return None
    return " ".join(parts)


def _shell_texts(command: str, visible: str) -> list[str]:
    """String arguments of the shell forms card #1346 names, outside quotes."""
    found: list[str] = []
    for match in _SHELL_AT.finditer(visible) if "sh" in visible else ():
        _charge_scan()
        inner = _c_argument(command, match.end())
        if inner:
            found.append(inner)
    for match in _EVAL_AT.finditer(visible) if "eval" in visible else ():
        _charge_scan()
        inner = _joined_words(command, match.end())
        if inner:
            found.append(inner)
    for match in _WATCH_AT.finditer(visible) if "watch" in visible else ():
        _charge_scan()
        inner = _watch_command(command, match.end())
        if inner:
            found.append(inner)
    for match in _XARGS_AT.finditer(visible) if "xargs" in visible else ():
        _charge_scan()
        inner = _xargs_shell(command, match.end())
        if inner:
            found.append(inner)
    for match in _ECHO_AT.finditer(visible) if "echo" in visible else ():
        _charge_scan()
        inner = _echo_pipeline_text(command, match.end())
        if inner:
            found.append(inner)
    return found


def _quoted_gh_command_word(command: str, visible: str) -> bool:
    """``"gh" pr view`` and ``'gh' pr list``: the command word is gh once unquoted."""
    if "'" not in command and '"' not in command:
        return False
    at_word = _AT_WORD if _ENABLE_BANG_AND_ENV_ASSIGN else _AT_WORD_LEGACY
    i = 0
    n = len(command)
    while i < n:
        _charge_scan()
        if (_RESOLVE_ANSI_C and command.startswith("$'", i)) or command.startswith('$"', i):
            body, end = _quoted_body(command, i)
            if _GH_EXACT.match(body) and at_word.search(_subspan(visible, 0, i)) is not None:
                return True
            i = end if end > i else i + 2
            continue
        if command[i] not in "'\"":
            i += 1
            continue
        body, end = _quoted_body(command, i)
        if _GH_EXACT.match(body) and at_word.search(_subspan(visible, 0, i)) is not None:
            return True
        i = end if end > i else i + 1
    return False


def targets_gh(command: str) -> bool:
    """True when the command actually invokes `gh`, not when a quote or comment mentions it."""
    command = command or ""
    state = _HOOK_STATE.get()
    if state is not None:
        state["scans"].clear()
    work = _RoutingWork(min(
        _WORK_PER_CHARACTER * len(command) + _WORK_FIXED_ALLOWANCE, _WORK_ABSOLUTE_LIMIT))
    work_token = _ROUTING_WORK.set(work)
    if state is not None:
        state["work"] = work
    seen_token = _ARITHMETIC_SEEN.set(False)
    # Visit distinct bodies in discovery order so the work budget is hash-seed independent.
    bodies_token = _STANDALONE_BODIES.set({})
    try:
        if _targets_gh(command, 0):
            return True
        # Folding or decoding a nested script can create arithmetic absent from raw text.
        if not _ARITHMETIC_SEEN.get():
            return False
        # A speculative commands reading finds calls, never supplies denial flags.
        token = _COMMANDS_READING.set(True)
        try:
            if _targets_gh(command, 0):
                return True
            # Keep old targets; a bounded fallback cannot let a shift consume later commands.
            bounded_token = _BOUNDED_COMMANDS.set(True)
            try:
                if _targets_gh(command, 0):
                    return True
                # Only add targets: an inner heredoc must not hide the construct's remainder.
                return any(_targets_gh(_INNER_HEREDOC_OP.sub("", body), 0)
                           for body in _STANDALONE_BODIES.get())
            finally:
                _BOUNDED_COMMANDS.reset(bounded_token)
        finally:
            _COMMANDS_READING.reset(token)
    finally:
        _STANDALONE_BODIES.reset(bodies_token)
        _ARITHMETIC_SEEN.reset(seen_token)
        _ROUTING_WORK.reset(work_token)


def _targets_gh(command: str, depth: int) -> bool:
    scan = _tokenize_bash(command)
    if scan.unterminated_heredoc and not _COMMANDS_READING.get():
        return True
    gh_inv = _GH_INVOCATION if _ENABLE_BANG_AND_ENV_ASSIGN else _GH_INVOCATION_LEGACY
    if gh_inv.search(scan.visible) or gh_inv.search(scan.targets_visible):
        return True
    if depth >= _MAX_SHELL_DEPTH:
        return True
    if _quoted_gh_command_word(scan.folded, scan.targets_visible):
        return True
    for inner in (*scan.heredoc_shell_texts, *_shell_texts(scan.folded, scan.targets_visible)):
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
    if text.startswith("$'", i) or text.startswith('$"', i) or text[i] in "'\"":
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
    if (_RESOLVE_ANSI_C and text.startswith("$'", i)) or text.startswith('$"', i):
        body, end = _quoted_body(text, i)
        return body, end
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
            am = _ASSIGN_NAME_AT.match(text, i)
            if not am:
                break
            vend = _value_end(text, am.end())
            if vend is None:
                return None
            if spans is not None and text.startswith("$(", am.end()):
                spans.append((am.end(), vend))
            if am.group(1) == "GH_TOKEN":
                if not _is_allowlisted_value(text, am.end(), vend):
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
    if scan.unterminated_quote or scan.unterminated_heredoc:
        return False
    raw = scan.folded
    visible = scan.visible
    if "GH_TOKEN=" not in visible:
        return False
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
        seg_start = _previous_separator(visible, m.start(), ";\n&|({") + 1
        before_words = deque(visible[seg_start:m.start()].split())
        while before_words and before_words[0] in _LEAD_KEYWORDS:
            before_words.popleft()
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
            am = _ASSIGN_NAME_AT.match(visible, i)
            if am:
                j = am.end()
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
            wm = _WORD_AT.match(visible, i)
            if wm and wm.group(1) in _WRAPPER_WORDS:
                i = wm.end()
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
            _, end = _quoted_body(text, i)
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
        _, end = _quoted_body(text, i)
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
        m = _WORD_AT.match(before, i)
        if m and m.group(1) in _LEAD_KEYWORDS:
            i = m.end()
            while i < n and before[i] in " \t":
                i += 1
            continue
        break
    if i >= n:
        return True
    m = _WORD_AT.match(before, i)
    if m and m.group(1) in _DECL_FORM_WORDS:
        i = m.end()
        while True:
            while i < n and before[i] in " \t":
                i += 1
            if i >= n:
                return True
            am = _ASSIGN_NAME_AT.match(before, i)
            if am:
                i = _consume_visible_value(before, am.end())
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
        am = _ASSIGN_NAME_AT.match(before, i)
        if am:
            i = _consume_visible_value(before, am.end())
            continue
        wm = _WORD_AT.match(before, i)
        if wm and wm.group(1) in _WRAPPER_WORDS:
            i = wm.end()
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


def has_unterminated_heredoc(command: str) -> bool:
    """True when ``command`` contains an unterminated or unclassifiable heredoc (#1555)."""
    return _tokenize_bash(command or "").unterminated_heredoc


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
