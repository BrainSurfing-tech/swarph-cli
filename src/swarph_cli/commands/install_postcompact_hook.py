"""``swarph install-postcompact-hook`` — board card #566.

Ships the card #549 post-compact wiring as a repo payload instead of a
box-local artifact: until this verb existed the three hook scripts lived only
in ``~/.cursor/hooks/`` on one box and had to be DM'd by hand to cursor-win
(msgs 26516-26518).

Per-harness wiring (the envelope translation is the harness difference):

* ``cursor`` — ``preCompact`` sets a per-conversation flag (cursor's
  preCompact is observational: it cannot inject context); the first
  ``postToolUse`` after the compaction consumes the flag and injects the
  7-day timeline recall as ``additional_context``. A second ``postToolUse``
  entry (matcher ``Write|Edit|StrReplace``) runs emit-on-write, with the
  cursor→claude shape adapter (``tool_input.path`` → ``tool_input.file_path``).
* ``claude`` — ``SessionStart`` fires with ``source=="compact"`` on the first
  post-compact turn, so no flag handshake is needed; the recall script
  rewrites the verb's ``hookEventName`` to ``SessionStart``. ``PostToolUse``
  (matcher ``Write|Edit|MultiEdit|NotebookEdit``) runs emit-on-write; the
  claude envelope is already the shape ``memory-emit-hook`` reads.

Identity and memory dir are baked at INSTALL time (the hook fires in the
harness's environment, which carries neither ``SWARPH_SELF`` nor
``SWARPH_MEMORY_DIR``) — but an explicit ``--cell`` into a BOX-GLOBAL config
is refused, same shape as card #527: a baked name in a file every session on
the box reads is a fleet-wide misconfiguration with a single-cell author.
Derivation at install (``$SWARPH_SELF`` → ``$SWARPH_CELL``) is the box-global
path; explicit ``--cell`` requires ``--scope project``.

On Windows (``os.name == 'nt'``) the harness spawns hooks via cmd and the
shebang never runs — every script gets a ``.cmd`` shim and the registration
points at the shim (cursor-win's measured finding, #549 obligation #24).

Idempotent, ``--uninstall``, ``--dry-run``, write-landed assert — the same
operator contract as ``swarph install-wake-hook`` (#527 task 3).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from swarph_cli.cell import _atomic_write_text
from swarph_cli.commands.install_wake_hook import _detect_harness

_PAYLOAD = "swarph_cli.payloads.postcompact"
# Every installed script path carries this component; it is the owned-entry
# marker for install idempotency and uninstall.
_DIR_NAME = "swarph-postcompact"

_CURSOR_SCRIPTS = ("precompact-flag.sh", "posttooluse-recall.sh",
                   "posttooluse-emit.sh")
_CLAUDE_SCRIPTS = ("sessionstart-recall.sh", "posttooluse-emit-claude.sh")


def _payload_text(name: str) -> str:
    """Read a payload script FROM THE PACKAGE — never from a source-tree
    path, so tests exercise the same importlib.resources lookup an installed
    wheel depends on (the 0.39.3 lesson: undeclared package data passes every
    test in the tree and fails on every installed wheel)."""
    return resources.files(_PAYLOAD).joinpath(name).read_text(encoding="utf-8")


def _config_path(harness: str, scope: str) -> Path:
    base = Path.cwd() if scope == "project" else Path.home()
    if harness == "cursor":
        return base / ".cursor" / "hooks.json"
    if harness == "claude":
        return base / ".claude" / "settings.json"
    raise ValueError(f"install-postcompact-hook: unknown harness {harness!r}")


def _scripts_dir(harness: str, scope: str) -> Path:
    base = Path.cwd() if scope == "project" else Path.home()
    sub = ".cursor" if harness == "cursor" else ".claude"
    return base / sub / "hooks" / _DIR_NAME


def _cursor_cell_memory_dir() -> Optional[Path]:
    """The cursor-cell memory convention: ~/.cursor-cell/projects/<cwd-slug>/
    memories (cwd /home/ubuntu -> home-ubuntu). Returned only when it exists —
    baking a path nobody reads manufactures the armed-looking-but-deaf cell."""
    slug = "-".join(p for p in Path.cwd().parts if p not in (os.sep, ""))
    candidate = Path.home() / ".cursor-cell" / "projects" / slug / "memories"
    return candidate if candidate.is_dir() else None


def _env_prefix(cell: Optional[str], memory_dir: Optional[Path]) -> str:
    parts = []
    if cell:
        # SWARPH_SELF, not SWARPH_CELL: psmux leaks CELL from the spawning
        # environment (#538); a baked SELF is the stronger identity claim and
        # outranks the leak under the #296 precedence fix.
        parts.append(f"SWARPH_SELF={shlex.quote(cell)}")
    if memory_dir:
        parts.append(f"SWARPH_MEMORY_DIR={shlex.quote(str(memory_dir))}")
    return (" ".join(parts) + " ") if parts else ""


def _render(name: str, *, env_prefix: str) -> str:
    return (_payload_text(name)
            .replace("@PYTHON@", shlex.quote(str(Path(sys.executable).resolve())))
            .replace("@ENV_PREFIX@", env_prefix))


def _registration(harness: str, scripts: Path, windows: bool) -> dict[str, Any]:
    """The config fragment for the harness, pointing at the installed scripts
    (or their .cmd shims on Windows)."""
    def cmd(script: str) -> str:
        p = scripts / (script.replace(".sh", ".cmd") if windows else script)
        return str(p)

    if harness == "cursor":
        return {
            "preCompact": [{"command": cmd("precompact-flag.sh"), "timeout": 5}],
            "postToolUse": [
                {"command": cmd("posttooluse-recall.sh"), "timeout": 15},
                {"command": cmd("posttooluse-emit.sh"),
                 "matcher": "Write|Edit|StrReplace", "timeout": 10},
            ],
        }
    return {
        "SessionStart": [{
            "hooks": [{"type": "command", "timeout": 15,
                       "command": cmd("sessionstart-recall.sh")}],
        }],
        "PostToolUse": [{
            "matcher": "Write|Edit|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "timeout": 10,
                       "command": cmd("posttooluse-emit-claude.sh")}],
        }],
    }


def _event_keys(harness: str) -> tuple[str, ...]:
    return ("preCompact", "postToolUse") if harness == "cursor" \
        else ("SessionStart", "PostToolUse")


def _is_owned_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if _DIR_NAME in str(entry.get("command", "")):
        return True
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        return any(isinstance(h, dict) and _DIR_NAME in str(h.get("command", ""))
                   for h in hooks)
    return False


def _merge(config: dict[str, Any], harness: str,
           fragment: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Replace owned entries, keep everything else. Returns (config, changed)."""
    config = dict(config)
    changed = False
    if harness == "cursor":
        config.setdefault("version", 1)
    hooks_node = config.setdefault("hooks", {})
    if not isinstance(hooks_node, dict):
        raise SystemExit(
            "swarph install-postcompact-hook: config key 'hooks' is not an "
            "object — refusing to overwrite. Fix the file first.")
    for event, new_entries in fragment.items():
        existing = hooks_node.get(event, [])
        if not isinstance(existing, list):
            raise SystemExit(
                f"swarph install-postcompact-hook: config key {event!r} is not "
                "an array — refusing to overwrite. Fix the file first.")
        kept = [e for e in existing if not _is_owned_entry(e)]
        merged = kept + new_entries
        if merged != existing:
            changed = True
        hooks_node[event] = merged
    return config, changed


def _remove_owned(config: dict[str, Any], harness: str) -> tuple[dict[str, Any], bool]:
    config = dict(config)
    hooks_node = config.get("hooks")
    if not isinstance(hooks_node, dict):
        return config, False
    changed = False
    for event in _event_keys(harness):
        existing = hooks_node.get(event)
        if not isinstance(existing, list):
            continue
        kept = [e for e in existing if not _is_owned_entry(e)]
        if kept != existing:
            changed = True
            hooks_node[event] = kept
    return config, changed


_USAGE = """\
Usage:
  swarph install-postcompact-hook [--harness cursor|claude]
                                  [--scope user|project] [--cell CELL]
                                  [--memory-dir DIR] [--uninstall] [--dry-run]

Installs the card #549 post-compact wiring (recall + emit-on-write) from the
repo payload (card #566). cursor gets the preCompact flag + postToolUse
recall handshake; claude gets SessionStart source=="compact" recall. Both get
emit-on-write. --cell bakes SWARPH_SELF and is refused with --scope user
(card #527's shape): a baked name in a box-global file arms one cell's
identity for every session on the box. Omit --cell and the install derives
$SWARPH_SELF / $SWARPH_CELL from the environment it runs in.
"""


def run_install_postcompact_hook(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]

    p = argparse.ArgumentParser(prog="swarph install-postcompact-hook")
    p.add_argument("--harness", default=None,
                   help="cursor|claude (default: detect)")
    p.add_argument("--cell", default=None,
                   help="cell name baked as SWARPH_SELF (default: derive from "
                   "$SWARPH_SELF/$SWARPH_CELL at install time). Valid only "
                   "with --scope project — a baked name in a box-global file "
                   "is refused (card #527's shape)")
    p.add_argument("--memory-dir", default=None,
                   help="extra memory directory baked as SWARPH_MEMORY_DIR "
                   "(default for cursor: the cursor-cell memories/ dir for "
                   "the current project, when it exists)")
    p.add_argument("--scope", choices=("user", "project"), default="user")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    if not argv and not sys.stdin.isatty():
        print(_USAGE, file=sys.stderr)
        return 0

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    harness = (args.harness or "").strip().lower() or _detect_harness()
    if harness not in ("cursor", "claude"):
        print(
            "swarph install-postcompact-hook: LOUD REFUSAL — "
            + (f"unsupported harness {args.harness!r}. " if args.harness
               else "could not detect a supported harness from this "
                    "environment. ")
            + "Supported: cursor, claude. Nothing was written. A silent "
            "no-op here would manufacture an armed-looking-but-deaf cell — "
            "the failure this verb exists to eliminate.",
            file=sys.stderr,
        )
        return 2

    target = _config_path(harness, args.scope)
    scripts = _scripts_dir(harness, args.scope)

    # Card #527's pair-check, one verb over: an EXPLICIT cell name baked into
    # a BOX-GLOBAL config arms one cell's identity for every session on the
    # box. Compare paths, not flags — --scope project run from the home
    # directory lands on the same box-global file.
    if (
        args.cell
        and not args.uninstall
        and target == _config_path(harness, "user")
    ):
        print(
            "swarph install-postcompact-hook: LOUD REFUSAL — --cell with a "
            f"box-global target ({target}) bakes ONE cell's identity into a "
            "file every session on the box reads (card #527's defect class). "
            "Valid combinations: box-global install WITHOUT --cell (identity "
            "is derived from this environment), or --scope project WITH "
            "--cell from a directory that is NOT the box's home. Nothing was "
            "written.",
            file=sys.stderr,
        )
        return 2

    cell = args.cell or os.environ.get("SWARPH_SELF") \
        or os.environ.get("SWARPH_CELL") or None
    memory_dir: Optional[Path] = None
    if args.memory_dir:
        memory_dir = Path(os.path.expanduser(args.memory_dir))
    elif os.environ.get("SWARPH_MEMORY_DIR"):
        memory_dir = Path(os.path.expanduser(os.environ["SWARPH_MEMORY_DIR"]))
    elif harness == "cursor":
        memory_dir = _cursor_cell_memory_dir()

    if args.uninstall:
        before_txt = target.read_text(encoding="utf-8") if target.exists() else "{}"
        try:
            before = json.loads(before_txt)
        except json.JSONDecodeError as exc:
            print(f"swarph install-postcompact-hook: {target} is not valid "
                  f"JSON: {exc}. Refusing to rewrite — fix the file first.",
                  file=sys.stderr)
            return 2
        after, changed = _remove_owned(before, harness)
        if args.dry_run:
            print(f"# dry-run: uninstall at {target}, changed={changed}",
                  file=sys.stderr)
            return 0
        if changed:
            _atomic_write_text(target, json.dumps(after, indent=2,
                                                  sort_keys=True) + "\n")
        scripts_existed = scripts.exists()
        if scripts_existed:
            shutil.rmtree(scripts)
        print(f"swarph install-postcompact-hook: uninstalled from {target} "
              f"(registration {'removed' if changed else 'was absent'}; "
              f"scripts dir {'removed' if scripts_existed else 'was absent'}: "
              f"{scripts}).", file=sys.stderr)
        return 0

    env_prefix = _env_prefix(cell, memory_dir)
    windows = os.name == "nt"
    names = list(_CURSOR_SCRIPTS if harness == "cursor" else _CLAUDE_SCRIPTS)

    if args.dry_run:
        print("# swarph install-postcompact-hook --dry-run", file=sys.stderr)
        print(f"#   harness:    {harness}", file=sys.stderr)
        print(f"#   config:     {target}", file=sys.stderr)
        print(f"#   scripts:    {scripts}", file=sys.stderr)
        print(f"#   cell:       {cell or '(none baked)'}", file=sys.stderr)
        print(f"#   memory-dir: {memory_dir or '(none baked)'}", file=sys.stderr)
        print(f"#   windows:    {windows}", file=sys.stderr)
        return 0

    scripts.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = scripts / name
        dest.write_text(_render(name, env_prefix=env_prefix), encoding="utf-8")
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)
        if windows:
            shim = _payload_text("_shim.cmd").replace("@SCRIPT@", name)
            (scripts / name.replace(".sh", ".cmd")).write_text(
                shim, encoding="utf-8")

    before_txt = target.read_text(encoding="utf-8") if target.exists() else "{}"
    try:
        before = json.loads(before_txt)
    except json.JSONDecodeError as exc:
        print(f"swarph install-postcompact-hook: {target} is not valid JSON: "
              f"{exc}. Refusing to overwrite — fix the file first.",
              file=sys.stderr)
        return 2
    if not isinstance(before, dict):
        print(f"swarph install-postcompact-hook: {target} must contain a JSON "
              "object — refusing to overwrite.", file=sys.stderr)
        return 2

    after, changed = _merge(before, harness,
                            _registration(harness, scripts, windows))
    payload = json.dumps(after, indent=2, sort_keys=True) + "\n"
    if changed:
        _atomic_write_text(target, payload)
        # The #527 task-3 assert: "installed" is a claim about the filesystem.
        if target.read_text(encoding="utf-8") != payload:
            print(f"swarph install-postcompact-hook: WRITE DID NOT LAND — "
                  f"{target} re-reads as different content than was written. "
                  "The install is NOT in effect.", file=sys.stderr)
            return 2

    print(f"swarph install-postcompact-hook: installed at {target} "
          f"({'registration updated' if changed else 'already up to date'}; "
          f"scripts in {scripts}).", file=sys.stderr)
    cell_disp = cell or "(none — memory-emit-hook falls back to the hostname)"
    mem_disp = str(memory_dir) if memory_dir else \
        "(none — only the .claude auto-memory layout is recognized)"
    print(f"Identity baked: SWARPH_SELF={cell_disp}; "
          f"SWARPH_MEMORY_DIR={mem_disp}.", file=sys.stderr)
    # cursor-win measured on the Windows membrane (DM 27104): the hook table
    # loads at session start only — no hot reload, and a compaction keeps the
    # pre-edit table. An install reported as landed is still inert in every
    # ALREADY-OPEN session; say so or the operator tests in the wrong session
    # and files the wiring as broken.
    print("NOTE: hook tables load at session start. Already-open sessions "
          "keep the pre-edit table — open a FRESH chat before testing.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_install_postcompact_hook(sys.argv[1:]))
