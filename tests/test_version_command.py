"""#615: swarph version — per-module versions + PEP 610 install origin.

The measured gap: a cell pipx-installed from merged main reported
'swarph-cli 0.49.1', identical to the release, while its dist-info already
recorded {"url": "file:///home/ubuntu/swarph-cli"}. The truth was on disk;
no verb read it.
"""

from __future__ import annotations

import json

import swarph_cli.commands.version as version


class _FakeDist:
    def __init__(self, ver, direct_url=None):
        self.version = ver
        self._du = direct_url

    def read_text(self, name):
        if name == "direct_url.json" and self._du is not None:
            return json.dumps(self._du)
        raise FileNotFoundError(name)


def _rows(monkeypatch, dists):
    monkeypatch.setattr(version.metadata, "distribution",
                        lambda name: dists[name])
    return version._collect()


def test_origin_dir_snapshot_is_named_not_hidden(monkeypatch):
    """The exact measured case: a working-tree install must not read as the
    released wheel."""
    rows = _rows(monkeypatch, {
        "swarph-cli": _FakeDist("0.49.1",
                                {"dir_info": {}, "url": "file:///home/ubuntu/swarph-cli"}),
        "swarph-mesh": _FakeDist("0.8.0"),
        "swarph-shared": _FakeDist("0.7.0"),
    })
    by = {r["name"]: r for r in rows}
    assert by["swarph-cli"]["origin"] == "dir:/home/ubuntu/swarph-cli"
    assert by["swarph-mesh"]["origin"] == "pypi"
    assert by["swarph-shared"]["origin"] == "pypi"


def test_origin_git_names_the_commit(monkeypatch):
    rows = _rows(monkeypatch, {
        "swarph-cli": _FakeDist("0.49.1", {"vcs_info": {"vcs": "git",
                                                        "commit_id": "1ec7703abcdef12"}}),
        "swarph-mesh": _FakeDist("0.8.0"),
        "swarph-shared": _FakeDist("0.7.0"),
    })
    assert {r["name"]: r for r in rows}["swarph-cli"]["origin"] == "git:1ec7703abcde"


def test_absent_module_is_reported_not_skipped(monkeypatch):
    def raise_missing(name):
        raise version.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "distribution", raise_missing)
    rows = version._collect()
    assert all(r["version"] is None and r["origin"] == "absent" for r in rows)


def test_check_flags_snapshot_and_stale(monkeypatch):
    _rows(monkeypatch, {
        "swarph-cli": _FakeDist("0.49.1", {"dir_info": {}, "url": "file:///x"}),
        "swarph-mesh": _FakeDist("0.7.0"),
        "swarph-shared": _FakeDist("0.7.0"),
    })
    monkeypatch.setattr(version, "_latest_published",
                        lambda name: {"swarph-cli": "0.49.1", "swarph-mesh": "0.8.0",
                                      "swarph-shared": "0.7.0"}[name])
    rc = version.run_version(["--check"])
    assert rc == 1  # snapshot cli + stale mesh: verified, actionable


def test_check_all_current_exits_0(monkeypatch):
    _rows(monkeypatch, {n: _FakeDist(v) for n, v in
                        (("swarph-cli", "0.49.1"), ("swarph-mesh", "0.8.0"),
                         ("swarph-shared", "0.7.0"))})
    monkeypatch.setattr(version, "_latest_published",
                        lambda name: {"swarph-cli": "0.49.1", "swarph-mesh": "0.8.0",
                                      "swarph-shared": "0.7.0"}[name])
    assert version.run_version(["--check"]) == 0


def test_check_pypi_unreachable_is_7_never_0(monkeypatch):
    """#401's contract on this surface: couldn't-verify is not success."""
    _rows(monkeypatch, {n: _FakeDist(v) for n, v in
                        (("swarph-cli", "0.49.1"), ("swarph-mesh", "0.8.0"),
                         ("swarph-shared", "0.7.0"))})
    monkeypatch.setattr(version, "_latest_published", lambda name: None)
    assert version.run_version(["--check"]) == 7


def test_plain_version_is_offline_and_zero(monkeypatch, capsys):
    """No --check: no network, always rc 0 — a readout, not a verdict."""
    _rows(monkeypatch, {n: _FakeDist(v) for n, v in
                        (("swarph-cli", "0.49.1"), ("swarph-mesh", "0.8.0"),
                         ("swarph-shared", "0.7.0"))})
    monkeypatch.setattr(version, "_latest_published",
                        lambda name: (_ for _ in ()).throw(AssertionError("offline path called")))
    assert version.run_version([]) == 0
    out = capsys.readouterr().out
    assert "swarph-cli" in out and "0.49.1" in out and "pypi" in out
