"""Credential file parsing — ONE reader, shared by every verb.

WHY THIS MODULE EXISTS (#332). `--token-file` was read by two different parsers
living in two command modules, and a third verb delegated across that boundary.
droplet closed the parser half of it on 2026-07-26 ("one parser behind one flag")
inside `commands/mesh.py` — but leaving the reader in a COMMAND module meant the
other verbs either reimplemented it or imported a private helper out of a sibling
command. gpt-ops named the fix while reviewing #332: extract the reader to a
neutral home rather than have `onboard` depend on `mesh`'s internals.

The parser is unchanged. Only its address is.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from swarph_cli.console_safe import print_safe

# Accepted env-style keys, in no priority order — the first one found wins,
# because a file carrying two different token keys is already ambiguous and
# ranking them would hide that rather than surface it.
TOKEN_KEYS = ("MESH_GATEWAY_TOKEN", "SWARPH_TOKEN", "TOKEN")


def validated_token(token: str, path: Path, lineno: int) -> str:
    """Reject a token that cannot go in an HTTP header, NAMING THE SOURCE.

    droplet's hardening, and it is worth more than the parse fix: a non-ASCII
    byte in an outbound header surfaced as a latin-1 codec traceback twelve
    frames deep in http/client.py, which points every reader at the HTTP layer
    for what is a malformed config FILE. Fail at the boundary where the operator
    can act, naming the file and the line.
    """
    try:
        token.encode("latin-1")
    except UnicodeEncodeError as exc:
        bad = token[exc.start:exc.start + 1]
        raise RuntimeError(
            f"token file {path} line {lineno}: token contains a non-ASCII "
            f"character {bad!r} (U+{ord(bad):04X}) at position {exc.start}, "
            f"which cannot be sent in an HTTP Authorization header. "
            f"If this line is a comment, prefix it with '#'; if the file is "
            f"env-style, use MESH_GATEWAY_TOKEN=<token>."
        ) from None
    return token


def read_token_file(path: Path) -> str:
    """Read a bearer token from a bare-token file OR an env-style file.

    ONE PARSER BEHIND ONE FLAG (droplet, 2026-07-26). This previously did
    `read_text().strip()` and returned the ENTIRE FILE, so `--token-file
    /root/.mesh.env` -- the shape the systemd unit itself documents -- put
    comments and other variables into the Authorization header. droplet's
    monitor died with

        UnicodeEncodeError: 'latin-1' codec can't encode character '\\u2014'

    from an em-dash in a COMMENT on line 5. Meanwhile `swarph daemon` read the
    same flag through onboard's resolver, which has always skipped comments and
    parsed KEY=VALUE. Two parsers, one flag -- the same one-thing-two-meanings
    shape as the cursor that meant both observed and woken.

    Accepts, in order:
      1. an env-style line `MESH_GATEWAY_TOKEN=...` (quotes stripped), or
      2. the first non-comment, non-blank, non-KEY=VALUE line as a bare token.

    RAISES rather than returning empty when the path is unreadable or holds no
    token. A caller that named a file has made a DECISION; handing it back an
    empty string invites a fallback to ambient state, which is exactly the #332
    defect this module was extracted to close.
    """
    # Mode check BEFORE the read, and it warns rather than refuses. This moved
    # here with the parser: it used to live only on onboard's secrets.toml path,
    # so `--token-file` reached through the OTHER parser was never checked at
    # all. A credential file readable by every account on the host is worth
    # saying out loud on EVERY path that reads one, not just the one path whose
    # author happened to think of it.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    if mode is not None and mode != 0o600:
        print_safe(
            f"swarph: WARNING: {path} mode is {oct(mode)}, expected 0600. "
            f"Continuing — fix manually with `chmod 600 {path}`.",
            file=sys.stderr,
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read token file {path}: {exc}") from exc

    bare: Optional[str] = None
    bare_lineno = 0
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip().upper() in TOKEN_KEYS:
                return validated_token(value.strip().strip('"').strip("'"),
                                       path, lineno)
            continue
        if bare is None:
            bare = line
            bare_lineno = lineno
    if bare is not None:
        return validated_token(bare, path, bare_lineno)
    raise RuntimeError(
        f"token file {path} contains no token: expected a bare token line or "
        f"one of {', '.join(TOKEN_KEYS)}=<token>"
    )


def peer_token_path(self_name: str) -> Path:
    """Where a cell's own minted credential lives (#243).

    Deliberately has NO default for `self_name`: guessing a name makes a cell
    hunt ANOTHER CELL'S token, find nothing, and blame the credential — measured
    on 6 of 6 cells 2026-07-29.
    """
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


def legacy_peer_token_path(self_name: str) -> Path:
    """The pre-migration location, still present on deployed boxes.

    workstation-lc carries BOTH this and the canonical path, byte-identical —
    which is luck, not design. Rotate one and they diverge silently, and which
    one wins depends on which tool you happen to run: that box's lead-contagion
    scripts read this path while the CLI reads the canonical one, so
    `send_mesh_dm` worked all day while `swarph mesh inbox` returned 401 under
    the SAME CLAIMED IDENTITY.
    """
    return Path.home() / ".swarph" / f"{self_name}.peer_token"


class TokenResolution:
    """The token plus WHERE it came from.

    The source is carried because "which credential did I actually send" is the
    question every one of these incidents turned on, and a bare string cannot
    answer it.

    `source` says WHERE the credential came from. It says nothing about what the
    credential IS — a peer-token file may hold any credential, as may an env var
    (both inversions measured 2026-08-06). Do not derive privilege from it.
    """

    __slots__ = ("token", "source", "detail")

    def __init__(self, token: str, source: str, detail: str = "") -> None:
        self.token = token
        self.source = source   # token-file | peer-token | legacy-peer-token | env | secrets
        self.detail = detail   # path, or env var name

    def __str__(self) -> str:  # pragma: no cover - diagnostic sugar
        return f"{self.source}({self.detail})"


def _warn(message: str, warn: Optional[Callable[[str], None]]) -> None:
    """print_safe, never bare print — this module is reachable from the daemon.

    Caught by test_daemon_console_encoding rather than by me. A bare print on a
    Windows console defaults to the system codepage (cp1252 on a French box), so
    any character outside it raises UnicodeEncodeError and takes the daemon down.
    The warnings below contain em-dashes: the guard fired on exactly the
    character class it exists for.
    """
    if warn is not None:
        warn(message)
    else:
        print_safe(f"swarph: {message}", file=sys.stderr)


def _try_read(path: Path, warn: Optional[Callable[[str], None]]) -> str:
    """read_token_file, downgraded to "" on failure, because at THIS layer an
    unreadable candidate means "not a usable source", not "abort".

    An explicitly NAMED file is different and callers must keep raising there —
    that is #332's rule and it is not weakened here.
    """
    try:
        return read_token_file(path)
    except RuntimeError as exc:
        _warn(str(exc), warn)
        return ""


def resolve_token(
    self_name: Optional[str],
    token_file_arg: Optional[str] = None,
    *,
    env_keys: Sequence[str] = ("MESH_GATEWAY_TOKEN",),
    identity_is_explicit: bool = False,
    allow_peer_token: bool = True,
    secrets_path: Optional[Path] = None,
    warn: Optional[Callable[[str], None]] = None,
) -> Optional[TokenResolution]:
    """ONE resolution order: explicit flag > per-identity file > ambient env.

    #332 established the principle for `--token-file`: an explicit argument is a
    decision, not a hint, so a value nobody named must not override one that was
    named.

    >>> `--as <cell>` / SWARPH_SELF IS THE SAME KIND OF STATEMENT. <<< It names
    which cell this is, so that cell's own credential should outrank an ambient
    one for exactly #332's reason. Two consequences of the old order, the second
    of which reads GREEN and is therefore the dangerous one:

      * when the ambient value is STALE, the verb 401s while a usable credential
        sits unread on disk (workstation-lc, 0.41.6 — the visible symptom);
      * when the ambient value is USABLE, the verb authenticates with it while
        the operator believes it used the named cell's. And since one
        process-global variable cannot be the credential for more than one cell,
        on a multi-cell host naming a cell selected an IDENTITY WITHOUT CARRYING
        ITS CREDENTIAL. workstation-lc runs three ratified cells whose token
        files hold three pairwise-distinct values.

    >>> WHAT THIS FUNCTION DOES NOT AND CANNOT KNOW: WHICH PRIVILEGES ANY OF
    THESE CREDENTIALS CARRY. <<< An earlier revision of this docstring called the
    env value "shared/root" and the peer file "scoped". gpt-ops blocked that on
    #190. Source category and file location DO NOT establish bearer class —
    measured five ways on 2026-08-06, including a shared-named variable holding a
    peer credential and a peer-named file holding what its owner took for the
    shared one. This resolver changes WHICH credential is SELECTED. It does not
    change, and cannot report, what that credential IS. Only the gateway knows
    (mesh-gateway PR #95 / card #335).

    Deliberately NARROW: the flip applies only when the identity is explicit. An
    operator who names no cell keeps the old behaviour, so #243's additivity
    guarantee survives for that population.

    Returns None rather than raising, so each caller keeps its own error text —
    onboard's enumerated "tried, in order" refusal is better than anything
    generic this module could produce, and #243 earned it.
    """
    if token_file_arg:
        p = Path(token_file_arg).expanduser()
        tok = read_token_file(p)          # raises: an explicit file is a decision
        return TokenResolution(tok, "token-file", str(p)) if tok else None

    peer_tok, peer_src, peer_label = "", None, "peer-token"
    peer_present = False   # a file EXISTED, whether or not it yielded a token
    # >>> peer_seen IS SEPARATE FROM peer_src ON PURPOSE. <<< peer_src is only set
    # once a candidate YIELDS a token, so it is None on every path where reading
    # failed — which is exactly the refusal branch below, whose whole job is to
    # name the file to fix. Tracking the path independently of the read
    # succeeding is what lets that message say WHICH file. (lab-ovh, review of
    # #190, reproduced by execution: the message rendered "(None)".)
    peer_seen = None
    if allow_peer_token and self_name:
        for candidate, label in ((peer_token_path(self_name), "peer-token"),
                                 (legacy_peer_token_path(self_name), "legacy-peer-token")):
            if not candidate.exists():
                continue
            peer_present = True
            if peer_seen is None:
                peer_seen = candidate
            tok = _try_read(candidate, warn)
            if not tok:
                continue
            peer_tok, peer_src, peer_label = tok, candidate, label
            if label == "legacy-peer-token":
                _warn(
                    f"{candidate} is the pre-migration credential location and is "
                    f"DEPRECATED; the canonical path is {peer_token_path(self_name)}. "
                    f"Two copies drift silently on rotation — move it.",
                    warn,
                )
            break

    env_tok, env_key = "", ""
    for key in env_keys:
        val = (os.environ.get(key) or "").strip()
        if val:
            env_tok, env_key = val, key
            break

    # The state in which somebody is about to lose an afternoon, whichever we
    # pick. With the resolution rule stated in several places, this warning is
    # the only thing that shows an investigator the real shape.
    if peer_tok and env_tok and peer_tok != env_tok:
        winner = "peer-token file" if identity_is_explicit else f"${env_key}"
        # SELECTION FACTS ONLY — no class claim. This message used to say
        # "$VAR is the SHARED credential and resolves to root; the peer file is
        # scoped to this cell". gpt-ops blocked that on #190 and was right: SOURCE
        # CATEGORY AND FILE LOCATION DO NOT ESTABLISH BEARER CLASS. Measured five
        # ways on 2026-08-06 — a shared-NAMED env var held a peer credential, a
        # peer-NAMED file held what its owner took for the shared one, and four
        # cells inferred class from filename, variable name, path+mode and token
        # LENGTH, all four wrong. A resolver cannot know the class and must not
        # imply it. Ask the gateway (mesh-gateway PR #95 / card #335).
        _warn(
            f"credential conflict for identity {self_name!r}: {peer_src} and "
            f"${env_key} hold DIFFERENT tokens; selecting the {winner} because "
            f"{'the identity was named explicitly' if identity_is_explicit else 'no identity was named'}. "
            f"Which privileges either token carries is NOT determined by where it "
            f"was found — ask the gateway if you need to know.",
            warn,
        )

    if identity_is_explicit and peer_tok:
        return TokenResolution(peer_tok, peer_label, str(peer_src))

    # >>> NO AMBIENT FALLBACK WHEN THE NAMED IDENTITY'S CREDENTIAL EXISTS BUT IS
    # UNUSABLE. <<< Caught by lab-ovh's review lens on #190, in my own code, and
    # it is #332's rule violated in miniature: naming a cell is a DECISION, so an
    # empty or unreadable <cell>.peer_token must not silently hand the caller the
    # ambient credential instead. That fallback SUCCEEDS — the call works, the
    # operator believes it ran as the named cell, and the only evidence is a
    # warning nobody reads. It is the exact escalation shape this PR exists to
    # close, reintroduced one branch lower down.
    #
    # A file that does NOT EXIST is different and still falls through: nothing
    # was named-and-present, so there is no decision to honour.
    if identity_is_explicit and peer_present:
        _warn(
            f"identity {self_name!r} was named and its credential file exists but "
            f"yields no usable token ({peer_seen}). NO AMBIENT FALLBACK WAS "
            f"ATTEMPTED — naming a cell is a decision, not a hint. Fix the file, "
            f"or unset the identity to use ambient credentials.",
            warn,
        )
        return None

    if env_tok:
        return TokenResolution(env_tok, "env", env_key)
    if peer_tok:
        return TokenResolution(peer_tok, peer_label, str(peer_src))
    if secrets_path is not None and secrets_path.exists():
        tok = _try_read(secrets_path, warn)
        if tok:
            return TokenResolution(tok, "secrets", str(secrets_path))
    return None
