"""The interpreter a hook installer bakes into the hook it writes.

A venv's ``bin/python`` is a symlink. Python finds that environment's
site-packages only when launched through the symlink (``pyvenv.cfg`` sits
next to it). ``Path.resolve()`` and ``realpath`` follow the link to the base
interpreter, which does not have ``swarph_cli``, and every later fire dies
with ``No module named swarph_cli``. Bake the unresolved absolute path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def hook_interpreter() -> str:
    """Unresolved absolute path of the interpreter running the installer."""
    return str(Path(sys.executable).absolute())


def interpreter_can_import(interpreter: str) -> bool:
    proc = subprocess.run(
        [interpreter, "-c", "import swarph_cli"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return proc.returncode == 0


def refuse_unless_importable(interpreter: str) -> None:
    """Refuse before any hook is written. The message names the interpreter."""
    if interpreter_can_import(interpreter):
        return
    raise SystemExit(
        f"swarph: refusing to install a hook whose interpreter cannot import "
        f"swarph_cli: {interpreter}"
    )
