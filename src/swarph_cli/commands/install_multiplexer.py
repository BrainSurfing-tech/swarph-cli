"""``swarph install-multiplexer`` — fetch the checksum-verified psmux binary.

swarph's session machinery needs a ``tmux``-compatible multiplexer on PATH. On
Linux/macOS that's native ``tmux`` (install via apt/brew). On Windows there's no
pip/npm package, so this verb fetches the pinned, SHA-256-verified **psmux**
release zip and drops its ``tmux.exe`` into ``--dir`` (default
``~/.swarph/bin``). The download is verified before anything is written.

Usage:
  swarph install-multiplexer [--dir DIR] [--force] [--version 3.3.6]
"""

from __future__ import annotations

import argparse
import os
import sys

from swarph_cli.multiplexer import (
    PSMUX_VERSION,
    find_multiplexer,
    install_psmux,
)


def run_install_multiplexer(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph install-multiplexer",
        description="Fetch the checksum-verified psmux (tmux-for-Windows) binary.")
    p.add_argument("--dir", default=os.path.expanduser("~/.swarph/bin"),
                   help="install directory (default ~/.swarph/bin)")
    p.add_argument("--force", action="store_true",
                   help="reinstall even if a multiplexer is already on PATH")
    p.add_argument("--version", default=PSMUX_VERSION,
                   help=f"psmux version to fetch (default {PSMUX_VERSION})")
    args = p.parse_args(argv)

    if sys.platform != "win32":
        print("swarph install-multiplexer fetches the Windows psmux binary. On "
              "Linux/macOS, tmux is the native multiplexer — install it via your "
              "package manager (`apt install tmux` / `brew install tmux`).",
              file=sys.stderr)
        return 0

    if not args.force:
        present = find_multiplexer()
        if present:
            print(f"multiplexer already present at {present}")
            return 0

    try:
        path = install_psmux(args.dir, version=args.version)
    except (RuntimeError, ValueError, OSError) as err:
        print(f"swarph install-multiplexer: {err}", file=sys.stderr)
        return 1

    print(f"installed psmux {args.version} -> {path}; "
          f"add {args.dir} to your PATH")
    return 0
