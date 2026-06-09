"""``swarph add`` — the ``lib`` artifact class (5th class).

The ``lib`` class is a THIN wrapper over pip: it discovers a builtin PyPI
package by name, runs ``pip install <package>`` (with a version pin when the
URI carries ``@<version>``), and registers the installed lib in
``~/.swarph/libs.json``. pip does the real install; the swarph's job is
discovery + the uniform ``swarph add`` verb + the registry record.

These tests pin: the grown :data:`ARTIFACT_CLASSES` enum, the lib-registry
persistence machinery (``_load_libs`` / ``_save_libs`` / ``_merge_lib``),
builtin resolution (:func:`resolve_builtin_lib`), and the full install path via
``run_add`` with an INJECTED fake ``pip_runner`` (no real shelling out):
builtin install / version-pin / pip-failure (code 6) / published-fails-closed
(code 2) / hash-mismatch (code 5).
"""

from __future__ import annotations

import json

import pytest

from swarph_cli.commands.add import (
    ARTIFACT_CLASSES,
    LibBundle,
    LibHandler,
    _load_libs,
    _merge_lib,
    parse_uri,
    resolve_builtin_lib,
    run_add,
    sha256_hex,
)


# --------------------------------------------------------------------------- #
# enum grew to 5 classes — lib is a valid parse class now
# --------------------------------------------------------------------------- #


def test_lib_in_artifact_classes() -> None:
    assert "lib" in ARTIFACT_CLASSES


def test_parse_lib_uri() -> None:
    ref = parse_uri("swarph://lib/swarph-builtin/phawkes")
    assert ref.klass == "lib"
    assert ref.publisher == "swarph-builtin"
    assert ref.name == "phawkes"
    assert ref.version is None
    assert ref.sha256 is None


# --------------------------------------------------------------------------- #
# lib-registry persistence machinery
# --------------------------------------------------------------------------- #


def test_merge_lib_idempotent_and_preserves_siblings() -> None:
    config = {"libs": {"existing": {"package": "existing"}}, "other": 1}
    meta = {"package": "phawkes", "version": None}
    out1 = _merge_lib(config, "phawkes", meta)
    assert out1["libs"]["phawkes"] == meta
    # sibling lib + sibling top-level key preserved
    assert out1["libs"]["existing"] == {"package": "existing"}
    assert out1["other"] == 1
    # idempotent: merging the same thing again is identical
    out2 = _merge_lib(out1, "phawkes", meta)
    assert out2 == out1


def test_merge_lib_creates_libs_key_when_absent() -> None:
    config: dict = {}
    out = _merge_lib(config, "x", {"package": "x"})
    assert out["libs"]["x"] == {"package": "x"}


def test_load_libs_missing_file_returns_empty(tmp_path) -> None:
    assert _load_libs(tmp_path / "nope.json") == {}


def test_load_libs_non_object_raises(tmp_path) -> None:
    p = tmp_path / "libs.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_libs(p)


# --------------------------------------------------------------------------- #
# builtin resolution
# --------------------------------------------------------------------------- #


def test_resolve_builtin_lib_phawkes() -> None:
    bundle = resolve_builtin_lib("phawkes")
    assert isinstance(bundle, LibBundle)
    assert bundle.trust == "builtin"
    assert bundle.publisher == "swarph-builtin"
    assert bundle.package == "phawkes"
    assert bundle.version is None


def test_resolve_builtin_lib_unknown_lists_the_five() -> None:
    with pytest.raises(ValueError) as exc:
        resolve_builtin_lib("nope")
    msg = str(exc.value)
    for name in ("phawkes", "fisherrao", "tailcor", "diebold-yilmaz", "hodgex"):
        assert name in msg


# --------------------------------------------------------------------------- #
# install path via run_add (fake pip_runner injected — no real shelling out)
# --------------------------------------------------------------------------- #


class FakePip:
    """Records pip args; returns a configurable returncode."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> int:
        self.calls.append(args)
        return self.rc


def test_builtin_install_pip_mocked(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=0)
    code = run_add(
        ["swarph://lib/swarph-builtin/phawkes", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 0
    assert pip.calls == [["install", "phawkes"]]
    libs = _load_libs(libs_path)
    assert libs["libs"]["phawkes"]["package"] == "phawkes"
    assert libs["libs"]["phawkes"]["publisher"] == "swarph-builtin"


def test_version_pin(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=0)
    code = run_add(
        ["swarph://lib/swarph-builtin/phawkes@0.1.0", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 0
    assert pip.calls == [["install", "phawkes==0.1.0"]]
    libs = _load_libs(libs_path)
    assert libs["libs"]["phawkes"]["version"] == "0.1.0"


def test_pip_failure_returns_6_and_writes_nothing(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=1)
    code = run_add(
        ["swarph://lib/swarph-builtin/phawkes", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 6
    assert pip.calls == [["install", "phawkes"]]
    # nothing registered
    assert not libs_path.exists()


def test_published_fails_closed(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=0)
    code = run_add(
        ["swarph://lib/lab-ovh/x", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 2
    # pip NOT called, nothing written
    assert pip.calls == []
    assert not libs_path.exists()


def test_hash_mismatch_returns_5_and_pip_not_called(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=0)
    # canonical bytes hash of phawkes's spec, then mangle it
    bundle = resolve_builtin_lib("phawkes")
    handler = LibHandler(libs_path=libs_path, pip_runner=pip)
    good = sha256_hex(handler._canonical_bytes(bundle))
    wrong = ("f" if good[0] != "f" else "0") + good[1:]
    code = run_add(
        [f"swarph://lib/swarph-builtin/phawkes#{wrong}", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 5
    assert pip.calls == []
    assert not libs_path.exists()


def test_hash_match_proceeds(tmp_path) -> None:
    libs_path = tmp_path / "libs.json"
    pip = FakePip(rc=0)
    bundle = resolve_builtin_lib("phawkes")
    handler = LibHandler(libs_path=libs_path, pip_runner=pip)
    good = sha256_hex(handler._canonical_bytes(bundle))
    code = run_add(
        [f"swarph://lib/swarph-builtin/phawkes#{good}", "--yes"],
        libs_path=libs_path,
        pip_runner=pip,
    )
    assert code == 0
    assert pip.calls == [["install", "phawkes"]]
    libs = _load_libs(libs_path)
    assert libs["libs"]["phawkes"]["sha256"] == good
