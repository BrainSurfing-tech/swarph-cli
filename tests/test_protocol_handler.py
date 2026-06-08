"""Tests for ``swarph protocol-handler`` (T6).

Registers ``swarph://`` as an OS URL-scheme handler so clicking a magnet
link on metaedge.surf launches ``swarph add <uri>`` (the magnet:→torrent
client UX). Linux is the primary target (.desktop + xdg-mime); other
platforms print documented manual steps.

All tests pass ``register=False`` so no real ``xdg-mime`` /
``update-desktop-database`` subprocess ever runs — the install/remove
paths stay hermetic and only touch the tmp ``applications_dir``.
"""

from __future__ import annotations

from swarph_cli import main as main_mod
from swarph_cli.commands import protocol_handler as ph


class _Capture:
    """Tiny ``out=`` sink collecting lines for assertions."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, *args) -> None:
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
# Pure desktop-entry builder
# --------------------------------------------------------------------------- #


def test_desktop_entry_contents():
    entry = ph._desktop_entry("/usr/bin/swarph")
    assert "Type=Application" in entry
    assert "Exec=/usr/bin/swarph add %u" in entry
    assert "MimeType=x-scheme-handler/swarph;" in entry
    assert "Terminal=true" in entry


# --------------------------------------------------------------------------- #
# install dry-run — writes nothing
# --------------------------------------------------------------------------- #


def test_install_dry_run_writes_nothing(tmp_path):
    cap = _Capture()
    rc = ph.install_protocol_handler(
        dry_run=True,
        applications_dir=tmp_path,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
        out=cap,
    )
    assert rc == 0
    # No .desktop file written.
    assert not (tmp_path / ph._DESKTOP_FILENAME).exists()
    assert list(tmp_path.iterdir()) == []
    # The preview includes the Exec line.
    assert "Exec=/usr/bin/swarph add %u" in cap.text


# --------------------------------------------------------------------------- #
# install real (hermetic) — writes the .desktop
# --------------------------------------------------------------------------- #


def test_install_real_writes_desktop(tmp_path):
    cap = _Capture()
    rc = ph.install_protocol_handler(
        applications_dir=tmp_path,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
        out=cap,
    )
    assert rc == 0
    desktop = tmp_path / ph._DESKTOP_FILENAME
    assert desktop.exists()
    content = desktop.read_text(encoding="utf-8")
    assert "Exec=/usr/bin/swarph add %u" in content
    assert "Terminal=true" in content
    assert "MimeType=x-scheme-handler/swarph;" in content
    # Some success line emitted.
    assert "swarph://" in cap.text or "installed" in cap.text.lower()


def test_install_creates_parent_dirs(tmp_path):
    nested = tmp_path / "deep" / "applications"
    rc = ph.install_protocol_handler(
        applications_dir=nested,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
        out=_Capture(),
    )
    assert rc == 0
    assert (nested / ph._DESKTOP_FILENAME).exists()


# --------------------------------------------------------------------------- #
# non-Linux graceful
# --------------------------------------------------------------------------- #


def test_install_non_linux_graceful(tmp_path):
    cap = _Capture()
    rc = ph.install_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Darwin",
        register=False,
        out=cap,
    )
    assert rc == 0
    # Nothing written.
    assert list(tmp_path.iterdir()) == []
    # Mentions manual guidance / the swarph add command.
    assert "swarph add" in cap.text


def test_install_windows_graceful(tmp_path):
    cap = _Capture()
    rc = ph.install_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Windows",
        register=False,
        out=cap,
    )
    assert rc == 0
    assert list(tmp_path.iterdir()) == []
    assert "swarph add" in cap.text


# --------------------------------------------------------------------------- #
# remove idempotent
# --------------------------------------------------------------------------- #


def test_remove_idempotent(tmp_path):
    # First install for real.
    ph.install_protocol_handler(
        applications_dir=tmp_path,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
        out=_Capture(),
    )
    assert (tmp_path / ph._DESKTOP_FILENAME).exists()

    rc1 = ph.remove_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Linux",
        register=False,
        out=_Capture(),
    )
    assert rc1 == 0
    assert not (tmp_path / ph._DESKTOP_FILENAME).exists()

    # Calling again — still 0, no raise.
    rc2 = ph.remove_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Linux",
        register=False,
        out=_Capture(),
    )
    assert rc2 == 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_before_and_after_install(tmp_path):
    cap_before = _Capture()
    rc = ph.status_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Linux",
        out=cap_before,
    )
    assert rc == 0
    # Not installed yet — report says so.
    assert "not" in cap_before.text.lower() or "available" in cap_before.text.lower()

    ph.install_protocol_handler(
        applications_dir=tmp_path,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
        out=_Capture(),
    )

    cap_after = _Capture()
    rc = ph.status_protocol_handler(
        applications_dir=tmp_path,
        platform_system="Linux",
        out=cap_after,
    )
    assert rc == 0
    assert "installed" in cap_after.text.lower()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_install_dry_run(tmp_path):
    rc = ph.run_protocol_handler(
        ["install", "--dry-run"],
        applications_dir=tmp_path,
        swarph_bin="/usr/bin/swarph",
        platform_system="Linux",
        register=False,
    )
    assert rc == 0
    assert not (tmp_path / ph._DESKTOP_FILENAME).exists()


def test_cli_status_and_remove(tmp_path):
    # install (real), status, remove via the CLI dispatcher.
    assert (
        ph.run_protocol_handler(
            ["install"],
            applications_dir=tmp_path,
            swarph_bin="/usr/bin/swarph",
            platform_system="Linux",
            register=False,
        )
        == 0
    )
    assert (tmp_path / ph._DESKTOP_FILENAME).exists()
    assert (
        ph.run_protocol_handler(
            ["status"],
            applications_dir=tmp_path,
            platform_system="Linux",
            register=False,
        )
        == 0
    )
    assert (
        ph.run_protocol_handler(
            ["remove"],
            applications_dir=tmp_path,
            platform_system="Linux",
            register=False,
        )
        == 0
    )
    assert not (tmp_path / ph._DESKTOP_FILENAME).exists()


def test_verb_registered_in_main():
    assert "protocol-handler" in main_mod._VERB_HANDLERS
    assert (
        main_mod._VERB_HANDLERS["protocol-handler"]
        == "swarph_cli.commands.protocol_handler.run_protocol_handler"
    )


def test_resolve_swarph_bin_returns_str():
    # Should always return a non-empty string (which or argv[0]).
    result = ph.resolve_swarph_bin()
    assert isinstance(result, str)
    assert result
