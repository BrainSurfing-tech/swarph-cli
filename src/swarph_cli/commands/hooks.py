"""``swarph hooks`` — Claude Code hooks installer (T1: merge core only).

This module ships the PURE settings.json merge primitives that wire
Claude Code hooks into ``~/.claude/settings.json`` as installable
content. Hooks become installable artifacts (shell scripts wired into
event handlers) WITHOUT a swarph-cli version bump per hook — the
installer reads bundled hook content and merges it into the user's
settings, the same way ``watchdog --install-service`` installs systemd
units as bundled data.

Claude Code's settings.json hook shape::

    {
      "hooks": {
        "StopFailure": [
          { "matcher": "rate_limit",
            "hooks": [ { "type": "command",
                         "command": "~/.swarph/hooks/cell-resilience.sh" } ] }
        ],
        "PostToolUse": [ ... ]
      }
    }

``hooks`` maps an EVENT name → a list of entries; each entry has a
``matcher`` (string; ``""`` = match-all) and a ``hooks`` list of
``{type, command}`` actions.

T1 SCOPE: ``_load_settings`` / ``_save_settings`` / ``_merge_hook`` /
``_unmerge_hook`` only. No CLI command, no argparse — those land in a
later task. The merge functions are pure (mutate-and-return a dict) so
the eventual CLI layer is a thin read → merge → atomic-write wrapper.

Atomic-write discipline mirrors ``watchdog.py``: write a temp file in
the SAME directory, then ``os.replace`` onto the target (atomic rename
on POSIX) so a crash mid-write never leaves a truncated settings.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# HookBundle model (T2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookBinding:
    """One event/matcher wiring for a hook script.

    A single script may be bound to MULTIPLE (event, matcher) pairs — e.g.
    ``cell-resilience`` listens on both ``StopFailure``/``rate_limit`` and
    ``Stop``/``""``. ``matcher == ""`` means match-all.
    """

    event: str  # "StopFailure" | "Stop" | ...
    matcher: str  # "rate_limit" | "" (= all)


@dataclass(frozen=True)
class HookBundle:
    """An installable hook: one script + its event/matcher bindings.

    ``script_body`` is carried INLINE (no package-data plumbing in v1) so the
    installer can write it under ``~/.swarph/hooks/<script_name>`` and then
    ``_merge_hook`` each binding into settings.json pointing at that path.
    """

    name: str
    description: str
    publisher: str  # "swarph-builtin" for bundled
    trust: str  # "builtin" | "local"
    script_name: str  # filename under ~/.swarph/hooks/, e.g. "cell-resilience.sh"
    script_body: str  # full shell-script content (inline)
    bindings: tuple  # tuple[HookBinding, ...]


# The bundled cell-resilience script. POSIX sh. Observational only: it
# records WHY a session went idle (throttle vs normal completion) into the
# watchdog's state home so a wake-on-throttle loop can read it. It MUST NOT
# fail the session — every path exits 0, and jq is optional (a printf/sed
# fallback handles the flat hook-stdin JSON when jq is absent).
_CELL_RESILIENCE_SH = r"""#!/bin/sh
# cell-resilience.sh — swarph bundled Claude Code hook (StopFailure/Stop).
#
# Reads the hook stdin JSON and records WHY the session just went idle into
#   $XDG_STATE_HOME/swarph/idle_since.json   (default ~/.local/state/swarph/)
# as {"session","reason","hook_event","ts"} where reason is "throttle" when
# the event is StopFailure (or error_type == rate_limit) else "normal".
# Observational: never blocks, always exits 0. jq used if present, else a
# printf/sed/grep fallback parses the flat hook-stdin JSON shape.

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/swarph"
mkdir -p "$STATE_DIR" 2>/dev/null || true

PAYLOAD=$(cat 2>/dev/null)
TS=$(date +%s 2>/dev/null || echo 0)

SESSION=""
EVENT=""
ERROR_TYPE=""

if command -v jq >/dev/null 2>&1; then
    SESSION=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null)
    EVENT=$(printf '%s' "$PAYLOAD" | jq -r '.hook_event_name // ""' 2>/dev/null)
    ERROR_TYPE=$(printf '%s' "$PAYLOAD" | jq -r '.error_type // ""' 2>/dev/null)
else
    # Degraded fallback: extract "key":"value" from flat JSON via sed.
    _extract() {
        printf '%s' "$PAYLOAD" \
            | tr ',' '\n' \
            | grep "\"$1\"" \
            | sed -e 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"//' -e 's/".*//' \
            | head -n 1
    }
    SESSION=$(_extract session_id)
    EVENT=$(_extract hook_event_name)
    ERROR_TYPE=$(_extract error_type)
fi

REASON="normal"
if [ "$EVENT" = "StopFailure" ] || [ "$ERROR_TYPE" = "rate_limit" ]; then
    REASON="throttle"
fi

printf '{"session":"%s","reason":"%s","hook_event":"%s","ts":%s}\n' \
    "$SESSION" "$REASON" "$EVENT" "$TS" \
    > "$STATE_DIR/idle_since.json" 2>/dev/null || true

exit 0
"""


BUILTIN_HOOKS: dict = {
    "cell-resilience": HookBundle(
        name="cell-resilience",
        description=(
            "Records why a Claude Code session went idle (throttle vs normal) "
            "into $XDG_STATE_HOME/swarph/idle_since.json for the watchdog's "
            "wake-on-throttle loop."
        ),
        publisher="swarph-builtin",
        trust="builtin",
        script_name="cell-resilience.sh",
        script_body=_CELL_RESILIENCE_SH,
        bindings=(
            HookBinding("StopFailure", "rate_limit"),
            HookBinding("Stop", ""),
        ),
    ),
}


# The curated set a fresh cell should start with — the recommended bundled
# hooks ``swarph hooks init`` installs in one shot. Today just cell-resilience
# (the push-side throttle detector); add future builtins here, not at call sites.
RECOMMENDED_HOOKS: tuple = ("cell-resilience",)


def resolve_builtin(name: str) -> HookBundle:
    """Return the bundled :class:`HookBundle` named ``name``.

    Raises ``ValueError`` (with the list of available builtin names) for an
    unknown name so the caller surfaces what IS installable. Local-path and
    published resolution are out of scope here (T3/T4).
    """
    try:
        return BUILTIN_HOOKS[name]
    except KeyError:
        available = ", ".join(sorted(BUILTIN_HOOKS))
        raise ValueError(
            f"unknown builtin hook {name!r}; available builtin hooks: {available}"
        ) from None


def _load_settings(path) -> dict:
    """Load settings.json, returning ``{}`` for a not-yet-existing file.

    * Missing file → ``{}`` — a settings.json that doesn't exist yet is a
      fine empty starting point for the installer.
    * Present but CORRUPT JSON → raise ``ValueError`` (with the path in the
      message). We MUST NOT silently return ``{}`` here: a later
      ``_save_settings`` would then overwrite the user's real-but-unparseable
      settings with our merged-onto-empty result, silently destroying their
      config. Surfacing the error protects them — the caller stops and the
      user fixes (or we back up) the broken file first.
    * Present but VALID-yet-NON-OBJECT JSON (``[]``, ``null``, ``5``, ``"x"`` —
      e.g. a truncated/fragment file) → raise ``ValueError`` too. Same
      fail-closed reasoning: a non-dict can't be merged onto (it would crash
      ``_merge_hook``'s ``setdefault``), and silently treating it as ``{}``
      would overwrite the user's real file. The contract is "never silently
      overwrite a user's real settings file."
    * Valid OBJECT → the parsed dict.
    """
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"settings.json at {p} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"settings.json at {p} is not a JSON object (got "
            f"{type(data).__name__}); refusing to merge onto a non-object "
            f"settings file"
        )
    return data


def _save_settings(path, obj) -> None:
    """Atomically write ``obj`` as JSON to ``path``.

    Creates parent dirs as needed. Writes to a NamedTemporaryFile in the
    same directory (so ``os.replace`` is a same-filesystem atomic rename),
    json.dump with ``indent=2``, flushes + fsyncs, then replaces the target.
    Mirrors watchdog.py's atomic-write discipline: a crash mid-write leaves
    either the old complete file or the new complete file, never a truncated
    one.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        # Clean up the temp file on any failure so we don't leak it.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _merge_hook(settings: dict, event: str, matcher: str, command: str) -> dict:
    """Merge one ``{event, matcher, command}`` hook into ``settings``.

    Mutate-and-return. Idempotent: dedups on ``command`` within the
    matching-matcher entry so re-running the installer never stacks
    duplicate actions. Preserves every other top-level key, every other
    event, and every other matcher-entry untouched.
    """
    hooks = settings.setdefault("hooks", {})
    event_list = hooks.setdefault(event, [])

    action = {"type": "command", "command": command}

    for entry in event_list:
        if entry.get("matcher", "") == matcher:
            actions = entry.setdefault("hooks", [])
            # Dedup on command — idempotent re-install.
            if not any(a.get("command") == command for a in actions):
                actions.append(action)
            return settings

    # No entry for this matcher yet — append a fresh one.
    event_list.append({"matcher": matcher, "hooks": [action]})
    return settings


def _unmerge_hook(settings: dict, event: str, matcher: str, command: str) -> dict:
    """Reverse of ``_merge_hook``. Mutate-and-return, idempotent.

    No-op (never raises) when the hooks key, the event, the matcher, or the
    command is absent. Removes the matching action; prunes the entry when its
    ``hooks`` list becomes empty; prunes the event key when its list becomes
    empty. Preserves all siblings (other events, matchers, commands, and
    top-level keys).
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    event_list = hooks.get(event)
    if not isinstance(event_list, list):
        return settings

    for entry in event_list:
        if entry.get("matcher", "") == matcher:
            actions = entry.get("hooks", [])
            entry["hooks"] = [
                a for a in actions if a.get("command") != command
            ]
            if not entry["hooks"]:
                event_list.remove(entry)
            break

    if not event_list:
        del hooks[event]

    return settings


# --------------------------------------------------------------------------- #
# Local-bundle resolver (T3)
# --------------------------------------------------------------------------- #


_DEFAULT_HOOKS_HOME = "~/.swarph/hooks"
_DEFAULT_SETTINGS_PATH = "~/.claude/settings.json"


def resolve_local(path) -> HookBundle:
    """Resolve a LOCAL hook bundle directory into a :class:`HookBundle`.

    ``path`` is a directory containing:
      * ``hook.json`` — manifest ``{"name","description","script_name",
        "bindings":[{"event","matcher"},...]}``
      * the script file named by ``script_name``.

    The returned bundle is tagged ``trust="local"`` and ``publisher=
    f"local:{path}"`` so the installer knows to REQUIRE confirmation before
    writing (vs. builtins which are trusted). ``script_body`` is read inline
    from the script file, mirroring the builtin bundles.

    Raises ``FileNotFoundError`` with a clear message when ``hook.json`` or
    the named script file is missing.
    """
    d = Path(path).expanduser()
    manifest_path = d / "hook.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"local hook bundle missing manifest: {manifest_path} not found"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook.json at {manifest_path} is not valid JSON: {exc}") from exc

    script_name = manifest["script_name"]
    script_path = d / script_name
    if not script_path.is_file():
        raise FileNotFoundError(
            f"local hook bundle missing script: {script_path} "
            f"(referenced by hook.json script_name={script_name!r})"
        )
    script_body = script_path.read_text(encoding="utf-8")

    bindings = tuple(
        HookBinding(event=b["event"], matcher=b.get("matcher", ""))
        for b in manifest.get("bindings", [])
    )

    return HookBundle(
        name=manifest["name"],
        description=manifest.get("description", ""),
        publisher=f"local:{d}",
        trust="local",
        script_name=script_name,
        script_body=script_body,
        bindings=bindings,
    )


# --------------------------------------------------------------------------- #
# Install (T3)
# --------------------------------------------------------------------------- #


def install_hook(
    bundle: HookBundle,
    *,
    settings_path=_DEFAULT_SETTINGS_PATH,
    hooks_home=_DEFAULT_HOOKS_HOME,
    assume_yes: bool = False,
    out=print,
) -> int:
    """Install ``bundle``: write its script + merge every binding into settings.

    Steps (with a show-before-write preview):

      1. Resolve the absolute installed-script path
         (``hooks_home/script_name`` expanded + absolute) — this is the
         command string written into settings.
      2. ``out(...)`` a preview: the script body (head) and a human-readable
         per-binding summary of what will be added (event / matcher / command).
      3. For ``trust != "builtin"`` (local bundles) REQUIRE confirmation. When
         ``assume_yes`` is False, prompt via ``input()`` and ABORT — writing
         NOTHING (no script, no settings) and returning non-zero — on anything
         but an affirmative ("y"/"yes"). Builtins proceed without a prompt.
      4. ``_load_settings`` → ``_merge_hook`` each binding into an in-memory
         merged dict. This happens BEFORE any disk write so a load/merge
         failure (corrupt or non-object settings.json, permission error)
         aborts with NOTHING written — all-or-nothing, no orphaned script.
      5. Write the script to ``hooks_home`` (mkdir -p, chmod 0755), then
         ``_save_settings`` the merged dict. If the save fails after the
         script write, best-effort remove the just-written script so a
         save-failure also leaves no orphan.
      6. ``out`` the activation caveat (open ``/hooks`` once or restart —
         Claude Code can't hot-load a hook into a running session).

    Returns 0 on success, non-zero on abort. A load/merge failure (e.g. a
    non-object settings.json) propagates as ``ValueError`` — the caller (CLI
    layer) surfaces it; nothing is written.
    """
    hooks_home_p = Path(hooks_home).expanduser()
    script_dst = (hooks_home_p / bundle.script_name).resolve()
    command = str(script_dst)

    # ---- show-before-write preview ----
    out(f"hook: {bundle.name}  (trust={bundle.trust}, publisher={bundle.publisher})")
    if bundle.description:
        out(f"  {bundle.description}")
    out(f"script → {script_dst}")
    body_lines = bundle.script_body.splitlines()
    head = body_lines[:12]
    out("  --- script (head) ---")
    for line in head:
        out(f"  | {line}")
    if len(body_lines) > len(head):
        out(f"  | ... ({len(body_lines) - len(head)} more lines)")
    out("  will add these bindings to settings:")
    for b in bundle.bindings:
        matcher_disp = b.matcher if b.matcher else "(all)"
        out(f"    - event={b.event}  matcher={matcher_disp}  command={command}")

    # ---- confirmation gate for non-builtin (local) bundles ----
    if bundle.trust != "builtin" and not assume_yes:
        try:
            answer = input(f"install local hook '{bundle.name}'? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            out("aborted — nothing written.")
            return 1

    # ---- load + merge FIRST (all-or-nothing) ----
    # Compute the final merged settings in memory BEFORE touching disk. If
    # _load_settings raises (corrupt or non-object settings.json, perm error),
    # we abort here having written NOTHING — no orphaned script.
    settings = _load_settings(settings_path)
    for b in bundle.bindings:
        settings = _merge_hook(settings, b.event, b.matcher, command)

    # ---- write the script, then atomic-save settings ----
    hooks_home_p.mkdir(parents=True, exist_ok=True)
    script_dst.write_text(bundle.script_body, encoding="utf-8")
    os.chmod(script_dst, 0o755)
    try:
        _save_settings(settings_path, settings)
    except BaseException:
        # Save failed AFTER the script write — best-effort remove the orphan
        # so a save-failure also leaves nothing behind.
        try:
            script_dst.unlink()
        except OSError:
            pass
        raise

    out("installed — open /hooks once or restart to activate")
    return 0


# --------------------------------------------------------------------------- #
# Recommended-set installer (T5): swarph hooks init
# --------------------------------------------------------------------------- #


def init_hooks(
    *,
    settings_path=_DEFAULT_SETTINGS_PATH,
    hooks_home=_DEFAULT_HOOKS_HOME,
    assume_yes: bool = False,
    out=print,
) -> int:
    """Install the recommended bundled set (:data:`RECOMMENDED_HOOKS`).

    For each recommended builtin name, ``install_hook(resolve_builtin(name),
    ...)``. Idempotent — re-running installs nothing new because ``_merge_hook``
    dedups on command, so a second ``init`` leaves each binding's ``hooks[]``
    untouched. Builtins are trusted, so no per-hook prompt fires regardless of
    ``assume_yes`` (it's threaded through anyway for symmetry / future local
    recommendations).

    Returns 0 when every install succeeded, non-zero if any returned non-zero
    (the worst rc is propagated). Emits one summary line at the end.
    """
    rc = 0
    for name in RECOMMENDED_HOOKS:
        bundle = resolve_builtin(name)
        one = install_hook(
            bundle,
            settings_path=settings_path,
            hooks_home=hooks_home,
            assume_yes=assume_yes,
            out=out,
        )
        if one != 0 and rc == 0:
            rc = one

    names = ", ".join(RECOMMENDED_HOOKS)
    status = "all installed" if rc == 0 else "completed with errors"
    out(
        f"hooks init: recommended set [{names}] {status} "
        f"— open /hooks once or restart to activate"
    )
    return rc


# --------------------------------------------------------------------------- #
# Uninstall + list (T4)
# --------------------------------------------------------------------------- #


def _installed_command(bundle: HookBundle, hooks_home) -> str:
    """The absolute installed-script path written into settings for ``bundle``.

    SAME construction ``install_hook`` uses, so unmerge/list match what install
    merged: ``(hooks_home/script_name).expanduser().resolve()`` as a string.
    """
    return str((Path(hooks_home).expanduser() / bundle.script_name).resolve())


def uninstall_hook(
    bundle: HookBundle,
    *,
    settings_path=_DEFAULT_SETTINGS_PATH,
    hooks_home=_DEFAULT_HOOKS_HOME,
    remove_script: bool = True,
    out=print,
) -> int:
    """Inverse of :func:`install_hook` — strip every binding + drop the script.

    For each binding, ``_unmerge_hook`` the resolved installed-script command
    (matching what install merged) out of settings, then atomically save. When
    ``remove_script`` and the script exists under ``hooks_home``, delete it
    (best-effort; errors swallowed). Idempotent: removing a not-installed hook
    is a no-op — never raises, returns 0.
    """
    command = _installed_command(bundle, hooks_home)

    settings = _load_settings(settings_path)
    for b in bundle.bindings:
        settings = _unmerge_hook(settings, b.event, b.matcher, command)
    _save_settings(settings_path, settings)

    if remove_script:
        script = Path(hooks_home).expanduser() / bundle.script_name
        if script.exists():
            try:
                script.unlink()
            except OSError:
                pass

    out(f"removed {bundle.name} — open /hooks once or restart to deactivate")
    return 0


def _is_installed(settings: dict, command: str, bundle: HookBundle) -> bool:
    """True when ALL of ``bundle``'s bindings' commands are present in settings."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for b in bundle.bindings:
        event_list = hooks.get(b.event)
        if not isinstance(event_list, list):
            return False
        found = False
        for entry in event_list:
            if entry.get("matcher", "") == b.matcher:
                if any(
                    a.get("command") == command for a in entry.get("hooks", [])
                ):
                    found = True
                break
        if not found:
            return False
    return True


def list_hooks(
    *,
    settings_path=_DEFAULT_SETTINGS_PATH,
    hooks_home=_DEFAULT_HOOKS_HOME,
    out=print,
) -> int:
    """List builtin hooks with install status. One greppable line per builtin::

        name  [installed|available]  trust=builtin  — description
    """
    settings = _load_settings(settings_path)
    for name in sorted(BUILTIN_HOOKS):
        bundle = BUILTIN_HOOKS[name]
        command = _installed_command(bundle, hooks_home)
        status = "installed" if _is_installed(settings, command, bundle) else "available"
        out(f"{name}  [{status}]  trust=builtin  — {bundle.description}")
    return 0


# --------------------------------------------------------------------------- #
# CLI surface (T3): swarph hooks add <name|path> [--yes]
# --------------------------------------------------------------------------- #


# The commons sigil. A ``target`` starting with this is a PUBLISHED reference
# (commons / another cell). v1 supports builtin + local ONLY — a published
# reference FAILS CLOSED (never installs an unreviewed peer's code).
_PUBLISHED_SIGIL = "@"


def _resolve_add_target(target: str) -> HookBundle:
    """Resolve an ``add`` target to a bundle, failing closed on published refs.

    A ``target`` starting with ``@`` is a published/commons reference. v1's
    trust gate REFUSES it outright (the real security evaluation is v2, scope
    §3.1) — raising ``ValueError`` BEFORE any builtin/local resolution so the
    add path mutates nothing. Otherwise: builtin name first, else local dir.
    """
    if target.startswith(_PUBLISHED_SIGIL):
        available = ", ".join(sorted(BUILTIN_HOOKS))
        raise ValueError(
            f"published hook {target!r} is not yet trusted — install a builtin "
            f"(available: {available}) or a local bundle dir you've reviewed; "
            f"signed-publisher + security-gate support is coming (scope §3.1)"
        )
    return _resolve_bundle(target)


def _resolve_bundle(target: str) -> HookBundle:
    """Resolve ``target`` to a bundle: builtin name first, else local dir.

    Raises ``ValueError`` (with the builtin list) when ``target`` is neither a
    known builtin nor an existing local bundle directory.
    """
    if target in BUILTIN_HOOKS:
        return resolve_builtin(target)
    if Path(target).expanduser().is_dir():
        return resolve_local(target)
    available = ", ".join(sorted(BUILTIN_HOOKS))
    raise ValueError(
        f"unknown hook {target!r} — not a builtin (available: {available}) "
        f"and not a local bundle dir"
    )


def run_hooks(
    argv: list[str] | None = None,
    *,
    settings_path=_DEFAULT_SETTINGS_PATH,
    hooks_home=_DEFAULT_HOOKS_HOME,
) -> int:
    """``swarph hooks`` dispatch: ``init`` / ``add`` / ``list`` / ``remove``.

    ``settings_path``/``hooks_home`` default to the real ``~`` locations but are
    overridable — the test seam for pointing the whole command at tmp paths
    without monkeypatching the default-path constants.
    """
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph hooks"

    parser = argparse.ArgumentParser(
        prog="swarph hooks",
        description="Manage Claude Code hooks (builtin or local bundle).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    init = sub.add_parser(
        "init", help="install the recommended bundled set (cell-resilience)"
    )
    init.add_argument(
        "--yes",
        action="store_true",
        help="skip confirmation prompts (no-op for trusted builtins, "
        "future-proofs against recommended local bundles)",
    )

    add = sub.add_parser("add", help="install a builtin or local hook bundle")
    add.add_argument(
        "target",
        help="builtin hook name (e.g. cell-resilience) or a local bundle dir "
        "containing hook.json + its script",
    )
    add.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for local (untrusted) bundles",
    )

    sub.add_parser("list", help="list builtin hooks and their install status")

    remove = sub.add_parser("remove", help="uninstall a builtin hook by name")
    remove.add_argument(
        "name",
        help="builtin hook name to uninstall (e.g. cell-resilience)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.action == "init":
        try:
            return init_hooks(
                settings_path=settings_path,
                hooks_home=hooks_home,
                assume_yes=args.yes,
            )
        except (ValueError, OSError) as exc:
            print(f"swarph hooks: {exc}", file=sys.stderr)
            return 2

    if args.action == "add":
        try:
            bundle = _resolve_add_target(args.target)
        except (ValueError, FileNotFoundError) as exc:
            print(f"swarph hooks: {exc}", file=sys.stderr)
            return 2
        try:
            return install_hook(
                bundle,
                settings_path=settings_path,
                hooks_home=hooks_home,
                assume_yes=args.yes,
            )
        except (ValueError, OSError) as exc:
            print(f"swarph hooks: {exc}", file=sys.stderr)
            return 2

    if args.action == "list":
        return list_hooks(settings_path=settings_path, hooks_home=hooks_home)

    if args.action == "remove":
        try:
            bundle = resolve_builtin(args.name)
        except ValueError as exc:
            print(
                f"swarph hooks: {exc}; remove targets builtins by name "
                f"(local-remove-by-path is a future nicety)",
                file=sys.stderr,
            )
            return 2
        return uninstall_hook(
            bundle, settings_path=settings_path, hooks_home=hooks_home
        )

    parser.error(f"unknown action: {args.action}")
    return 2
