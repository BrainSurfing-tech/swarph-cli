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

import sys
from pathlib import Path
from typing import Optional

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
    # Windows exposes synthetic POSIX mode bits for NTFS files. Security there
    # is enforced by ACLs, so a chmod instruction is misleading and inactionable.
    if sys.platform != "win32":
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
