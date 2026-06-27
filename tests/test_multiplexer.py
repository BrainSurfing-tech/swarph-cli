"""Tests for the multiplexer helper — detect tmux/psmux + the checksum-verified
psmux fetcher.

The load-bearing security property: ``install_psmux`` MUST verify the SHA-256 of
the downloaded bytes BEFORE it writes or extracts anything, and it MUST never
honor a path component inside the zip (zip-slip). A bad download or a malicious
archive name therefore never reaches the filesystem outside ``dest_dir``.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile

import pytest

from swarph_cli import multiplexer as mx


# --- find_multiplexer -------------------------------------------------------

def test_find_multiplexer_returns_path_when_tmux_present(monkeypatch):
    monkeypatch.setattr(mx.shutil, "which",
                        lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    assert mx.find_multiplexer() == "/usr/bin/tmux"


def test_find_multiplexer_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(mx.shutil, "which", lambda name: None)
    assert mx.find_multiplexer() is None


def test_find_multiplexer_falls_back_to_psmux(monkeypatch):
    monkeypatch.setattr(mx.shutil, "which",
                        lambda name: "/c/psmux.exe" if name == "psmux" else None)
    assert mx.find_multiplexer() == "/c/psmux.exe"


# --- multiplexer_hint -------------------------------------------------------

def test_hint_windows_mentions_install_verb(monkeypatch):
    monkeypatch.setattr(mx.sys, "platform", "win32")
    hint = mx.multiplexer_hint()
    assert "install-multiplexer" in hint and "psmux" in hint


def test_hint_posix_mentions_package_manager(monkeypatch):
    monkeypatch.setattr(mx.sys, "platform", "linux")
    hint = mx.multiplexer_hint()
    assert "tmux" in hint and ("apt" in hint or "brew" in hint)


# --- _detect_arch -----------------------------------------------------------

@pytest.mark.parametrize("machine,expected", [
    ("AMD64", "x64"), ("x86_64", "x64"),
    ("ARM64", "arm64"), ("aarch64", "arm64"),
    ("x86", "x86"), ("i686", "x86"), ("i386", "x86"),
])
def test_detect_arch_maps_known_machines(monkeypatch, machine, expected):
    monkeypatch.setattr(mx.platform, "machine", lambda: machine)
    assert mx._detect_arch() == expected


def test_detect_arch_raises_on_unknown(monkeypatch):
    monkeypatch.setattr(mx.platform, "machine", lambda: "sparc")
    with pytest.raises(ValueError):
        mx._detect_arch()


# --- install_psmux helpers --------------------------------------------------

def _zip_bytes(members):
    """Build an in-memory zip: members is a list of (name, data)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _pin_arch(monkeypatch, filename, data):
    """Force _detect_arch -> 'x64' and pin _ASSETS so the real sha matches."""
    monkeypatch.setattr(mx, "_detect_arch", lambda: "x64")
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(mx, "_ASSETS", {"x64": (filename, sha)})
    return sha


# --- install_psmux: success -------------------------------------------------

def test_install_psmux_success_writes_binary(monkeypatch, tmp_path):
    data = _zip_bytes([("tmux.exe", b"FAKE")])
    _pin_arch(monkeypatch, "psmux.zip", data)

    out = mx.install_psmux(str(tmp_path), opener=lambda url: data)

    assert os.path.basename(out).lower() == "tmux.exe"
    assert os.path.isfile(out)
    assert (tmp_path / "tmux.exe").read_bytes() == b"FAKE"


# --- install_psmux: checksum mismatch (the load-bearing security test) ------

def test_install_psmux_checksum_mismatch_writes_nothing(monkeypatch, tmp_path):
    good = _zip_bytes([("tmux.exe", b"FAKE")])
    _pin_arch(monkeypatch, "psmux.zip", good)
    # opener returns DIFFERENT bytes whose hash won't match the pinned sha
    tampered = _zip_bytes([("tmux.exe", b"EVIL-PAYLOAD")])

    with pytest.raises(RuntimeError):
        mx.install_psmux(str(tmp_path), opener=lambda url: tampered)

    # nothing extracted/written: a bad download never reaches the filesystem
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / "tmux.exe").exists()


# --- install_psmux: zip-slip ------------------------------------------------

def test_install_psmux_zip_slip_stays_in_dest(monkeypatch, tmp_path):
    dest = tmp_path / "bin"
    dest.mkdir()
    # malicious member tries to escape dest_dir; hash matches so we test extract
    data = _zip_bytes([("../evil.exe", b"FAKE"), ("tmux.exe", b"GOOD")])
    _pin_arch(monkeypatch, "psmux.zip", data)

    out = mx.install_psmux(str(dest), opener=lambda url: data)

    # the escaped path was never honored
    assert not (tmp_path / "evil.exe").exists()
    assert not (dest.parent / "evil.exe").exists()
    # the legitimate binary still lands inside dest
    assert os.path.isfile(out)
    assert (dest / "tmux.exe").exists()


def test_install_psmux_no_binary_in_zip_raises(monkeypatch, tmp_path):
    data = _zip_bytes([("README.txt", b"hi")])
    _pin_arch(monkeypatch, "psmux.zip", data)
    with pytest.raises(RuntimeError):
        mx.install_psmux(str(tmp_path), opener=lambda url: data)
