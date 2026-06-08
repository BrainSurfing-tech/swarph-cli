"""``swarph protocol-handler`` — register ``swarph://`` as an OS URL scheme.

Clicking a magnet-style ``swarph://...`` link on metaedge.surf should
launch ``swarph add <uri>`` the same way a ``magnet:`` link launches a
torrent client. This module performs the OS-level registration so the
browser knows which command to hand the clicked URL to.

The dispatcher (``swarph add``) already exists; this command only wires
the scheme to it.

Linux (primary target)
----------------------
Writes a ``.desktop`` file under ``~/.local/share/applications`` and
registers it for the ``x-scheme-handler/swarph`` MIME type via
``xdg-mime default`` + ``update-desktop-database``. The ``.desktop``
entry uses ``Terminal=true`` because ``swarph add`` shows the artifact
and prompts for confirmation on non-builtin installs — it MUST run in a
terminal where the user sees that prompt/output. ``%u`` passes the
clicked URL through as the argument.

macOS / Windows
---------------
There is no single portable CLI to register a scheme handler the way
``xdg-mime`` does, so those platforms print concise documented manual
steps (and the fallback of just copying the ``swarph add <uri>`` command
straight from metaedge) and return success — a non-Linux host is not an
error, just unsupported for auto-registration.

Atomic-write discipline mirrors ``hooks.py`` / ``watchdog.py``: write a
temp file in the SAME directory then ``os.replace`` onto the target so a
crash mid-write never leaves a truncated ``.desktop`` file.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


_DESKTOP_FILENAME = "swarph-url-handler.desktop"
_SCHEME = "x-scheme-handler/swarph"
_DEFAULT_APPLICATIONS_DIR = "~/.local/share/applications"


# --------------------------------------------------------------------------- #
# Pure desktop-entry builder
# --------------------------------------------------------------------------- #


def resolve_swarph_bin() -> str:
    """The command the handler invokes: ``which swarph`` else this argv[0].

    Prefers an installed ``swarph`` on PATH (stable across venv layouts).
    Falls back to the absolute path of the currently running entry-point so
    a not-yet-on-PATH dev checkout still produces a runnable handler.
    """
    found = shutil.which("swarph")
    if found:
        return found
    return os.path.abspath(sys.argv[0])


def _desktop_entry(swarph_bin: str) -> str:
    """Return a valid freedesktop ``.desktop`` entry for the scheme handler.

    ``Terminal=true`` is REQUIRED — ``swarph add`` displays the artifact and
    prompts for confirmation on non-builtin installs, so it must run in a
    terminal where the user can see the prompt/output. ``%u`` passes the
    clicked ``swarph://...`` URL through as the single argument.
    """
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=swarph URL handler\n"
        f"Exec={swarph_bin} add %u\n"
        "NoDisplay=true\n"
        "Terminal=true\n"
        f"MimeType={_SCHEME};\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (temp in same dir + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _resolve_applications_dir(applications_dir) -> Path:
    if applications_dir is None:
        return Path(_DEFAULT_APPLICATIONS_DIR).expanduser()
    return Path(applications_dir)


# --------------------------------------------------------------------------- #
# Non-Linux manual guidance
# --------------------------------------------------------------------------- #


def _print_manual_steps(platform_system: str, swarph_bin: str, out) -> None:
    """Print documented manual scheme-registration steps for non-Linux hosts."""
    out(
        f"swarph protocol-handler: automatic registration is Linux-only; "
        f"{platform_system} needs a manual step."
    )
    if platform_system == "Darwin":
        out("macOS:")
        out(
            "  - Build a tiny .app whose Info.plist declares the 'swarph' URL "
            "scheme (CFBundleURLTypes), or call"
        )
        out(
            "    LSSetDefaultHandlerForURLScheme(CFSTR(\"swarph\"), <your "
            "bundle id>) once from a small helper, so clicking a swarph:// link"
        )
        out(f"    runs:  {swarph_bin} add <uri>")
    elif platform_system == "Windows":
        out("Windows:")
        out("  - Register the scheme under HKCR\\swarph, e.g.:")
        out('      reg add "HKCR\\swarph" /ve /d "URL:swarph" /f')
        out('      reg add "HKCR\\swarph" /v "URL Protocol" /d "" /f')
        out(
            '      reg add "HKCR\\swarph\\shell\\open\\command" /ve /d '
            f'"\\"{swarph_bin}\\" add \\"%%1\\"" /f'
        )
    else:
        out(f"  - Register the 'swarph' URL scheme to run:  {swarph_bin} add <uri>")
    out(
        "Or just copy the `swarph add <uri>` command straight from metaedge "
        "and run it in a terminal."
    )


# --------------------------------------------------------------------------- #
# install / status / remove
# --------------------------------------------------------------------------- #


def install_protocol_handler(
    *,
    dry_run: bool = False,
    applications_dir=None,
    swarph_bin=None,
    platform_system=None,
    register: bool = True,
    out=print,
) -> int:
    """Register ``swarph://`` so clicking a link runs ``swarph add <uri>``.

    On non-Linux hosts this prints manual steps and returns 0 (graceful — an
    unsupported platform is not an error). On Linux it writes the ``.desktop``
    file and (when ``register``) best-effort runs ``xdg-mime`` +
    ``update-desktop-database``; missing tools are noted but never fail the
    install (the file is written either way). ``dry_run`` previews the entry +
    the commands and writes NOTHING.
    """
    plat = platform_system or platform.system()
    bin_ = swarph_bin or resolve_swarph_bin()

    if plat != "Linux":
        _print_manual_steps(plat, bin_, out)
        return 0

    apps_dir = _resolve_applications_dir(applications_dir)
    target = apps_dir / _DESKTOP_FILENAME
    content = _desktop_entry(bin_)

    if dry_run:
        out("# would write " + str(target) + ":")
        out(content)
        out(
            f"# would run: xdg-mime default {_DESKTOP_FILENAME} {_SCHEME} ; "
            f"update-desktop-database {apps_dir}"
        )
        return 0

    _atomic_write(target, content)
    out(f"wrote {target}")

    if register:
        _register_scheme(apps_dir, out)

    out(f"registered swarph:// → {bin_} add %u")
    out("test it: xdg-open 'swarph://hook/swarph-builtin/cell-resilience'")
    return 0


def _register_scheme(apps_dir: Path, out) -> None:
    """Best-effort ``xdg-mime default`` + ``update-desktop-database``.

    Swallows missing-tool (``FileNotFoundError``) and non-zero-exit
    (``CalledProcessError``) so a host without xdg utilities still gets the
    ``.desktop`` file written — the install never fails on registration.
    """
    try:
        subprocess.run(
            ["xdg-mime", "default", _DESKTOP_FILENAME, _SCHEME],
            check=True,
        )
    except FileNotFoundError:
        out(
            "note: xdg-mime not found — .desktop written but scheme not "
            "registered; install xdg-utils or run `xdg-mime default "
            f"{_DESKTOP_FILENAME} {_SCHEME}` manually."
        )
        return
    except subprocess.CalledProcessError as exc:
        out(f"note: xdg-mime default failed ({exc}); .desktop written anyway.")
        return

    try:
        subprocess.run(
            ["update-desktop-database", str(apps_dir)],
            check=True,
        )
    except FileNotFoundError:
        out(
            "note: update-desktop-database not found — registration may need a "
            "re-login to take effect."
        )
    except subprocess.CalledProcessError as exc:
        out(f"note: update-desktop-database failed ({exc}); continuing.")


def status_protocol_handler(
    *,
    applications_dir=None,
    platform_system=None,
    out=print,
) -> int:
    """Report whether the scheme handler is installed for this user."""
    plat = platform_system or platform.system()
    apps_dir = _resolve_applications_dir(applications_dir)
    target = apps_dir / _DESKTOP_FILENAME

    if target.exists():
        out(f"swarph:// handler [installed]  {target}")
    else:
        out(f"swarph:// handler [not installed]  (would live at {target})")

    if plat == "Linux":
        try:
            res = subprocess.run(
                ["xdg-mime", "query", "default", _SCHEME],
                check=True,
                capture_output=True,
                text=True,
            )
            current = res.stdout.strip()
            out(f"xdg-mime default for {_SCHEME}: {current or '(none)'}")
        except (FileNotFoundError, subprocess.CalledProcessError):
            out(f"xdg-mime query for {_SCHEME}: unavailable")

    return 0


def remove_protocol_handler(
    *,
    applications_dir=None,
    platform_system=None,
    register: bool = True,
    out=print,
) -> int:
    """Delete the ``.desktop`` handler (idempotent) + best-effort unregister."""
    apps_dir = _resolve_applications_dir(applications_dir)
    target = apps_dir / _DESKTOP_FILENAME

    if target.exists():
        try:
            target.unlink()
            out(f"removed {target}")
        except OSError as exc:
            out(f"note: could not remove {target} ({exc})")
    else:
        out(f"swarph:// handler not installed (nothing at {target})")

    if register:
        # Best-effort refresh of the desktop database so the dangling
        # association is dropped. Missing tools are silently fine.
        try:
            subprocess.run(
                ["update-desktop-database", str(apps_dir)],
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    return 0


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def run_protocol_handler(
    argv: list[str] | None = None,
    *,
    applications_dir=None,
    swarph_bin=None,
    platform_system=None,
    register: bool = True,
) -> int:
    """``swarph protocol-handler`` dispatch: ``install`` / ``status`` / ``remove``.

    The injectable seams (``applications_dir`` / ``swarph_bin`` /
    ``platform_system`` / ``register``) default to ``None``/real values so the
    command works out of the box, but tests can point it at tmp paths and a
    fake platform without monkeypatching module globals.
    """
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph protocol-handler"

    parser = argparse.ArgumentParser(
        prog="swarph protocol-handler",
        description=(
            "Register swarph:// as an OS URL-scheme handler so clicking a "
            "magnet link on metaedge.surf launches `swarph add <uri>`."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    install = sub.add_parser(
        "install", help="register the swarph:// scheme for this user"
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="print the .desktop entry + the commands that would run; "
        "write nothing",
    )

    sub.add_parser("status", help="report whether the handler is installed")
    sub.add_parser("remove", help="unregister the swarph:// scheme handler")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.action == "install":
        return install_protocol_handler(
            dry_run=args.dry_run,
            applications_dir=applications_dir,
            swarph_bin=swarph_bin,
            platform_system=platform_system,
            register=register,
        )

    if args.action == "status":
        return status_protocol_handler(
            applications_dir=applications_dir,
            platform_system=platform_system,
        )

    if args.action == "remove":
        return remove_protocol_handler(
            applications_dir=applications_dir,
            platform_system=platform_system,
            register=register,
        )

    parser.error(f"unknown action: {args.action}")
    return 2
