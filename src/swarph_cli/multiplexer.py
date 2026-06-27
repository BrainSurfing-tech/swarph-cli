"""Multiplexer detection + the checksum-verified psmux fetcher.

swarph's session machinery (spawn/cell/watchdog) shells out to a ``tmux``-
compatible binary. On Linux/macOS that's ``tmux`` from the package manager; on
Windows it's **psmux** (https://github.com/psmux/psmux — a Rust tmux-for-Windows,
MIT) which installs a ``tmux`` command on PATH. psmux isn't on pip/npm — it ships
as per-arch GitHub release zips, so we fetch the pinned, **checksum-verified**
binary ourselves.

Security core (``install_psmux``): the order is download -> verify SHA-256 ->
(reject on mismatch, OR extract). A bad download NEVER reaches extraction, and a
malicious archive member name can never escape ``dest_dir`` (zip-slip guard:
only ``os.path.basename`` is ever honored). Nothing is executed.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile

# --- pinned psmux release (v3.3.6, GitHub-attested digests) -----------------

PSMUX_VERSION = "3.3.6"
_URL = "https://github.com/psmux/psmux/releases/download/v3.3.6/{filename}"
_ASSETS = {  # arch -> (filename, sha256)
    "x64":   ("psmux-v3.3.6-windows-x64.zip",   "a56a890ea0829567818b9a368f16dcbd39c087f27328573df17c10dd39618947"),
    "arm64": ("psmux-v3.3.6-windows-arm64.zip", "bf1e8bb9d624a2fe5cb09f6b5bb84e7f516cfd022efb677a2439f846a39f8f92"),
    "x86":   ("psmux-v3.3.6-windows-x86.zip",   "9b387fe03cbcc1ef671a5c3fbddaafe383089d82cea03c567980d8ed3de9ba82"),
}

# binaries we accept out of the archive (basename, case-insensitive); the first
# is the primary whose path we return.
_BINARIES = ("tmux.exe", "psmux.exe", "pmux.exe")


def find_multiplexer() -> str | None:
    """Return the path of the first tmux-compatible multiplexer on PATH, else None."""
    for name in ("tmux", "psmux", "pmux"):
        found = shutil.which(name)
        if found:
            return found
    return None


def multiplexer_hint() -> str:
    """A one-line, OS-aware install hint for a missing multiplexer."""
    if sys.platform == "win32":
        return ("no tmux-compatible multiplexer found — run "
                "`swarph install-multiplexer` (fetches psmux) or "
                "`winget install marlocarlo.psmux`")
    return ("no tmux found — install it via your package manager "
            "(e.g. `apt install tmux` / `brew install tmux`)")


def _detect_arch() -> str:
    """Map ``platform.machine()`` to an ``_ASSETS`` key; raise ValueError otherwise."""
    machine = platform.machine()
    m = machine.lower()
    if m in ("amd64", "x86_64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86", "i686", "i386"):
        return "x86"
    raise ValueError(f"unsupported architecture for psmux: {machine!r}")


def _default_opener(url: str) -> bytes:
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (pinned github.com URL)
        return resp.read()


def install_psmux(dest_dir, *, version=PSMUX_VERSION, opener=None) -> str:
    """Fetch + verify + extract the pinned psmux binary into ``dest_dir``.

    Order is load-bearing: download -> SHA-256 verify -> (reject OR extract). A
    checksum mismatch raises BEFORE anything is written, and only the basename of
    an archive member is ever honored (zip-slip guard). Returns the path to the
    extracted primary binary (``tmux.exe``). Nothing is executed.
    """
    opener = opener or _default_opener

    # 1. resolve the pinned asset for this arch
    arch = _detect_arch()
    filename, expected_sha = _ASSETS[arch]

    # 2. download
    data = opener(_URL.format(filename=filename))

    # 3. verify BEFORE touching the filesystem — a bad download stops here
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"psmux checksum mismatch for {filename}: "
            f"expected {expected_sha}, got {actual_sha} — refusing to extract")

    # 4. only now open the verified archive and extract the binaries
    os.makedirs(dest_dir, exist_ok=True)
    written: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            # zip-slip guard: never honor a path component; skip anything that
            # isn't a plain basename (no separators, no traversal).
            if not base or base != member or ".." in member \
                    or "/" in member or "\\" in member:
                continue
            if base.lower() not in _BINARIES:
                continue
            target = os.path.join(dest_dir, base)
            # atomic write: temp file in dest_dir, then os.replace
            fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".psmux-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(zf.read(member))
                os.replace(tmp, target)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            written[base.lower()] = target

    if not written:
        raise RuntimeError(
            f"no psmux binary ({'/'.join(_BINARIES)}) found in {filename}")

    # return the primary (tmux.exe) if present, else whatever we extracted
    for name in _BINARIES:
        if name in written:
            return written[name]
    return next(iter(written.values()))
