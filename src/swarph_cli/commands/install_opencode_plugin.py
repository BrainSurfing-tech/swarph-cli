"""``swarph install-opencode-plugin`` — the OpenCode plugin that wires swarph hooks.

Part 2 of the opencode membrane. opencode has NO command-hook surface — its only
hook surface is a JavaScript plugin loaded from the config dir's ``plugins/``
tree (auto-loaded, no ``opencode.json`` ``plugin`` entry required; measured:
``~/.config/opencode/plugin/opensling.js`` loads against a config containing only
``$schema``). This verb renders the package payload (baking the pinned Python
interpreter, the same reason ``install_codex_hooks`` pins it: PATH presence is
not a liveness proof) and writes it as ``swarph-opencode.js``:

* ``--scope user``    → ``~/.config/opencode/plugins/swarph-opencode.js`` —
  the OPERATOR's bare ``opencode`` sessions (outside a swarph cell).
* ``--scope project`` → ``<cwd>/.opencode/plugins/swarph-opencode.js`` — a
  per-project wiring.

The swarph CELL does NOT read either of those locations: its membrane relocates
``XDG_CONFIG_HOME`` to ``<cwd>/.opencode-cell/config``, so its plugin lives at
``<cwd>/.opencode-cell/config/opencode/plugins/swarph-opencode.js`` and is
written by the membrane's ``_opencode_env`` via ``ensure_cell_plugin`` — a
spawned cell is wired without a separate onboarding step.

Identity (SWARPH_SELF / SWARPH_MEMORY_DIR) is read by the plugin from env AT
RUNTIME, not baked — the membrane already stamps SWARPH_SELF into the cell env,
and a bare-session install inherits the operator's env at hook time.

Idempotent, ``--uninstall``, ``--dry-run``, write-landed assert — the same
operator contract as every other hook installer (#527 task 3).
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from swarph_cli.cell import _atomic_write_text
from swarph_cli.commands.hook_interpreter import (
    hook_interpreter,
    interpreter_can_import,
    refuse_unless_importable,
)

_PAYLOAD = "swarph_cli.payloads.opencode"
_PLUGIN_NAME = "swarph-opencode.js"


def _payload_text() -> str:
    """Read the plugin FROM THE PACKAGE — never a source-tree path, so tests
    exercise the same importlib.resources lookup an installed wheel depends on
    (the 0.39.3 undeclared-package-data lesson)."""
    return resources.files(_PAYLOAD).joinpath("plugin.js").read_text(encoding="utf-8")


def _render_plugin(interpreter: str) -> str:
    r"""Render the plugin with @PYTHON@ replaced by ``interpreter`` AS A JS STRING.

    >>> shlex.quote is WRONG here, and it was the shipped defect (#423 review).
    The placeholder sits on the RHS of ``const PY = @PYTHON@;`` — a JavaScript
    string literal position, NOT shell text. ``shlex.quote`` wraps in single
    quotes and leaves backslashes raw, so on Windows
    ``C:\Program Files\Python\python.exe`` became
    ``const PY = "'C:\Program Files\Python\python.exe'";`` — invalid escapes, and
    the single quotes land INSIDE the double quotes, so the value is a wrong path
    even where it parses. json.dumps produces a valid JS string literal (double
    quoted, backslashes escaped as ``\\``), which a JS parser accepts and decodes
    back to the exact path. The template leaves the quotes to the substitution
    (``const PY = @PYTHON@;``) so they are never doubled. <<<
    """
    return _payload_text().replace("@PYTHON@", json.dumps(interpreter))


def _render() -> str:
    """The rendered plugin, with @PYTHON@ pinned to THIS interpreter."""
    return _render_plugin(hook_interpreter())


def _target(scope: str) -> Path:
    if scope == "project":
        return Path.cwd() / ".opencode" / "plugins" / _PLUGIN_NAME
    return Path.home() / ".config" / "opencode" / "plugins" / _PLUGIN_NAME


def cell_plugin_path(cwd: Path) -> Path:
    """Where a cell's plugin lives inside its isolated config dir.

    Mirrors ``_OPENCODE_CELL_SUBDIR`` + the XDG_CONFIG_HOME layout the membrane
    sets in ``_opencode_env`` — kept here (not imported from spawn) so spawn can
    call back into it without a cycle.
    """
    return cwd / ".opencode-cell" / "config" / "opencode" / "plugins" / _PLUGIN_NAME


def ensure_cell_plugin(cwd: Path) -> None:
    """Write the cell's plugin if absent or changed. Idempotent + best-effort:
    a failure must not block the spawn (worst case the cell runs unhooked)."""
    try:
        rendered = _render()
        if not interpreter_can_import(hook_interpreter()):
            return
        target = cell_plugin_path(cwd)
        if target.exists() and target.read_text(encoding="utf-8") == rendered:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, rendered)
    except Exception:
        pass


def _read_existing(target: Path) -> str:
    return target.read_text(encoding="utf-8") if target.exists() else ""


def run_install_opencode_plugin(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[2:]

    p = argparse.ArgumentParser(prog="swarph install-opencode-plugin")
    p.add_argument("--scope", choices=("user", "project"), default="user")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    target = _target(args.scope)

    if args.dry_run:
        print(f"# swarph install-opencode-plugin --dry-run", file=sys.stderr)
        print(f"#   scope:  {args.scope}", file=sys.stderr)
        print(f"#   target: {target}", file=sys.stderr)
        print(f"#   action: {'uninstall' if args.uninstall else 'install'}", file=sys.stderr)
        if not args.uninstall:
            print(_render())
        return 0

    if args.uninstall:
        existed = target.exists()
        if existed:
            target.unlink()
        print(
            f"swarph install-opencode-plugin: uninstalled {_PLUGIN_NAME} from "
            f"{target} ({'removed' if existed else 'was absent'}).",
            file=sys.stderr,
        )
        return 0

    rendered = _render()
    if _read_existing(target) == rendered:
        print(
            f"swarph install-opencode-plugin: no change needed at {target} "
            "(plugin already in desired state).",
            file=sys.stderr,
        )
        return 0

    refuse_unless_importable(hook_interpreter())
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, rendered)
    # The #527 task-3 assert: "installed" is a claim about the filesystem.
    if target.read_text(encoding="utf-8") != rendered:
        print(
            f"swarph install-opencode-plugin: WRITE DID NOT LAND — {target} "
            "re-reads as different content than was written. The install is NOT "
            "in effect.",
            file=sys.stderr,
        )
        return 2
    print(
        f"swarph install-opencode-plugin: installed {_PLUGIN_NAME} at {target}.",
        file=sys.stderr,
    )
    print(
        "NOTE: opencode loads the plugin table at session start. Already-open "
        "sessions keep the pre-edit table — open a FRESH session before testing.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_install_opencode_plugin(sys.argv[1:]))
