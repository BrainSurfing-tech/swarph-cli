"""Card #20 deployment artifacts: isolated executables, uid and pull-only state."""
from __future__ import annotations

import configparser
from importlib.resources import files
import os
import plistlib
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


def _unit(name):
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.optionxform = str
    unit.read_string(files("swarph_cli").joinpath("systemd", name).read_text())
    return unit


def test_proxy_service_uses_the_isolated_install_and_private_store():
    service = _unit("swarph-proxy@.service")["Service"]
    assert service["User"] == "proxy-%i"
    assert service["Group"] == "proxy-%i"
    assert shlex.split(service["ExecStart"]) == [
        "/opt/swarph-proxy/bin/python", "-I", "-m",
        "swarph_cli.commands.proxy", "serve", "--cell", "%i",
    ]
    assert service["Environment"] == "HOME=/var/lib/swarph-proxy/%i"
    assert service["WorkingDirectory"] == "/var/lib/swarph-proxy/%i"
    assert service["LimitCORE"] == "0"
    assert service["UMask"] == "0077"
    assert "RuntimeDirectory" not in service


def test_socket_is_created_by_the_system_manager_in_a_non_cell_writable_directory():
    socket = _unit("swarph-proxy@.socket")["Socket"]
    assert socket["ListenStream"] == "/run/swarph-proxy/%i.sock"
    assert socket["SocketUser"] == "proxy-%i"
    assert socket["SocketGroup"] == "cell-%i"
    assert socket["SocketMode"] == "0660"
    assert socket["DirectoryMode"] == "0755"
    assert socket["RemoveOnStop"] == "yes"


def test_monitor_dropin_supersedes_both_legacy_wakes_and_keeps_state_traversable():
    name = "zzzz-proxy20.conf"
    assert all(name > legacy for legacy in ("20-tmux-wake-961.conf", "zz-pilot20.conf"))
    text = files("swarph_cli").joinpath(
        "systemd", "swarph-monitor@.service.d", name).read_text()
    service = _unit(f"swarph-monitor@.service.d/{name}")["Service"]
    assert service["User"] == "proxy-%i"
    assert service["Group"] == "cell-%i"
    assert service["Environment"] == "HOME=/var/lib/swarph-proxy/%i"
    assert "Environment=\n" in text
    assert "ExecStart=\n" in text
    assert shlex.split(service["ExecStart"]) == [
        "/opt/swarph-proxy/bin/python", "-I", "-m", "swarph_cli.proxy_monitor",
        "--cell", "%i", "--state-dir", "/var/log/swarph-proxy/%i/monitor",
    ]
    assert service["LimitCORE"] == "0"
    assert service["UMask"] == "0027"
    assert "MESH_GATEWAY_URL" in service["UnsetEnvironment"]
    assert "MESH_GATEWAY_TOKEN" in service["UnsetEnvironment"]
    assert "tmux" not in service["ExecStart"]


def test_launchd_proxy_and_monitor_have_separate_users_and_disable_core_dumps():
    for filename, module, group, umask in (
        ("swarph-proxy.plist", "swarph_cli.commands.proxy", "cell-__CELL__", 0o077),
        ("swarph-proxy-monitor.plist", "swarph_cli.proxy_monitor", "cell-__CELL__", 0o027),
    ):
        unit = plistlib.loads(files("swarph_cli").joinpath("launchd", filename).read_bytes())
        assert unit["UserName"] == "proxy-__CELL__"
        assert unit["GroupName"] == group
        assert unit["ProgramArguments"][:4] == [
            "/opt/swarph-proxy/bin/python", "-I", "-m", module,
        ]
        assert unit["EnvironmentVariables"] == {"HOME": "/private/var/lib/swarph-proxy/__CELL__"}
        assert unit["WorkingDirectory"] == "/private/var/lib/swarph-proxy/__CELL__"
        assert unit["SoftResourceLimits"]["Core"] == 0
        assert unit["HardResourceLimits"]["Core"] == 0
        assert unit["Umask"] == umask
        assert unit["KeepAlive"] is True
        if module.endswith("proxy_monitor"):
            assert unit["ProgramArguments"][4:] == [
                "--cell", "__CELL__", "--state-dir", "/private/var/log/swarph-proxy/__CELL__/monitor",
            ]
        else:
            assert unit["ProgramArguments"][4:] == ["serve", "--cell", "__CELL__"]


def test_built_wheel_contains_every_proxy_deployment_template(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(root / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copyfile(root / name, source / name)
    built = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
        cwd=source, capture_output=True, text=True, timeout=90,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    with zipfile.ZipFile(next((source / "dist").glob("*.whl"))) as wheel:
        assert {
            "swarph_cli/systemd/swarph-proxy@.service",
            "swarph_cli/systemd/swarph-proxy@.socket",
            "swarph_cli/systemd/swarph-monitor@.service.d/zzzz-proxy20.conf",
            "swarph_cli/launchd/swarph-proxy.plist",
            "swarph_cli/launchd/swarph-proxy-monitor.plist",
        } <= set(wheel.namelist())


@pytest.mark.skipif(os.name != "posix", reason="separate Unix uid and mode boundary")
def test_install_preflight_checks_real_executable_home_and_import_tree_modes(tmp_path):
    from swarph_cli.local_proxy import validate_install
    from swarph_cli.proxy_routes import ProxyError

    tmp_path = tmp_path.resolve()
    cell_uid = 65534 if os.geteuid() != 65534 else 65533
    install = tmp_path / "opt" / "swarph-proxy"
    python = install / "bin" / "python"
    imports = install / "lib" / "python" / "site-packages"
    module = imports / "swarph_cli" / "__init__.py"
    home = tmp_path / "var" / "lib" / "swarph-proxy" / "fixture-cell"
    for parent in (python.parent, module.parent, home):
        parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    python.chmod(0o755)
    module.write_text("", encoding="utf-8")
    module.chmod(0o644)
    home.chmod(0o700)
    assert home.stat().st_uid != cell_uid
    validate_install([python, home, imports], cell_uid)

    module.chmod(0o666)
    with pytest.raises(ProxyError):
        validate_install([python, home, imports], cell_uid)
    module.chmod(0o644)
    install.chmod(0o777)
    with pytest.raises(ProxyError):
        validate_install([python, home, imports], cell_uid)
    install.chmod(0o755)
    python.unlink()
    python.symlink_to(sys.executable)
    with pytest.raises(ProxyError):
        validate_install([python, home, imports], cell_uid)
