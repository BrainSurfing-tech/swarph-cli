"""#648: `swarph monitor install-unit` — the packaged Linux supervisor.

The Linux monitor unit sat at deploy/monitor/, OUTSIDE the package, so the
wheel never carried it and every box hand-rolled its own — four unit shapes
on one box, and reader-side compensation (_unit_identity) to work out which
unit belongs to which cell. #644 gave Windows install-task; this is the
Linux half.

The verb reads the template FROM THE INSTALLED PACKAGE via
importlib.resources, substitutes the install-time placeholders (<USER>,
<HOME>, <GATEWAY>, <SWARPH_BIN> — systemd has no specifier for "the user
this box's cells run as"), leaves %i for systemd, and writes the
@-instance form #130 prescribes. The accept check's clean-room legs
(built wheel, fresh venv; then the same with the package-data entry
removed) are run by hand — the source tree has the file either way, which
is the exact 0.39.3 / v0.45 defect pyproject's comments record. These
tests guard everything the tree CAN guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import swarph_cli.commands.monitor as monitor


def test_renders_placeholders_and_keeps_the_instance_specifier(capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://100.64.189.91:8788",
         "--user", "celluser", "--home", "/home/celluser"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[Unit]" in out and "[Service]" in out
    # every install-time placeholder substituted...
    import re
    assert not re.findall(r"<[A-Z_]+>", out), "unsubstituted placeholder survived"
    # ...but %i is systemd's, resolved per instance — the @-form's whole point
    assert "SWARPH_SELF=%i" in out
    assert "--as %i" in out
    assert "Environment=MESH_GATEWAY_URL=http://100.64.189.91:8788" in out
    assert "User=celluser" in out
    assert "ExecStart=/home/celluser/.local/bin/swarph monitor start" in out


def test_gateway_is_required_not_defaulted(capsys):
    """#578 removed the code default; a unit without a gateway refuses on
    every poll, so the verb refuses at RENDER time, where it is free."""
    rc = monitor.run_monitor(["install-unit", "--gateway", " "])
    assert rc == 2
    assert "MESH_GATEWAY_URL" in capsys.readouterr().err


def test_non_absolute_gateway_is_refused_by_name(capsys):
    rc = monitor.run_monitor(["install-unit", "--gateway", "lab-ovh-1:8788"])
    assert rc == 2
    assert "absolute" in capsys.readouterr().err


def test_missing_resource_is_a_named_error_not_a_traceback(monkeypatch, capsys):
    """The can-fail arm: a wheel built without the package-data entry must
    produce THIS message — a diagnosis of the packaging, not a crash in the
    verb. Patched at the resources layer so the conversion in
    _read_unit_template is what gets tested."""
    from importlib import resources
    monkeypatch.setattr(resources, "files",
                        lambda pkg: Path("/nonexistent-pkg"))
    rc = monitor.run_monitor(["install-unit", "--gateway", "http://g:1"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "swarph-monitor@.service" in err and "mis-built" in err
    assert "Traceback" not in err


def test_default_prints_and_writes_nothing(tmp_path, capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://g:1", "--dir", str(tmp_path)])
    assert rc == 0
    assert "[Unit]" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == [], "print mode must not write"


def test_write_lands_the_at_instance_form(tmp_path, capsys):
    """#130's guard on this verb: the file on disk is the TEMPLATE name —
    swarph-monitor@.service — never the bare or hyphen-suffixed form that
    diverged across the fleet. --write is Linux-only; on Windows the verb
    must name that and write nothing (reviewers-pixel on #347)."""
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://g:1", "--write",
         "--dir", str(tmp_path), "--as", "crespo3"])
    if os.name == "nt":
        assert rc == 2
        assert "Linux-only" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []
        return
    assert rc == 0
    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["swarph-monitor@.service"], names


def test_write_refuses_an_unwritable_dir(capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://g:1", "--write",
         "--dir", "/proc/1/no-such-dir"])
    err = capsys.readouterr().err
    assert rc == 2
    if os.name == "nt":
        # Linux-only fires before the writable check — the named refuse.
        assert "Linux-only" in err
    else:
        assert "not writable" in err


def test_unsubstituted_placeholder_is_loud(monkeypatch):
    monkeypatch.setattr(monitor, "_read_unit_template",
                        lambda: "ExecStart=<SWARPH_BIN> x\nEnvironment=<NOPE>\n")
    with pytest.raises(RuntimeError, match="NOPE"):
        monitor._render_unit(monitor._read_unit_template(),
                             gateway="http://g:1", user="u", home="/h",
                             swarph_bin="/b")


def test_print_path_names_the_packaged_template(capsys):
    rc = monitor.run_monitor(["install-unit", "--print-path"])
    out = capsys.readouterr().out
    assert rc == 0
    # Traversable on Windows prints backslashes; the packaged name is posix.
    assert out.strip().replace("\\", "/").endswith(
        "systemd/swarph-monitor@.service")
