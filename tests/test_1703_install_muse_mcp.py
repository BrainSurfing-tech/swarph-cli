"""Card #1703: scripts/install-muse-mcp.sh against a fake ``muse`` and a scratch HOME."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-muse-mcp.sh"
GATEWAY = "http://100.64.189.91:8788"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash script")

# Fake muse: `mcp get <name> --json` and `mcp add-json <name> <json> --user`
# over $HOME/.config/muse/settings.json; an existing name is refused, as Muse does.
FAKE_MUSE = r'''import json, os, sys
path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.environ["HOME"], ".config"), "muse", "settings.json")
data = json.load(open(path)) if os.path.exists(path) else {}
servers = data.setdefault("mcpServers", {})
args = sys.argv[1:]
if args[:2] == ["mcp", "get"]:
    if args[2] not in servers:
        sys.exit(1)
    print(json.dumps(dict(servers[args[2]], env={k: "[REDACTED]" for k in servers[args[2]].get("env", {})})))
elif args[:2] == ["mcp", "add-json"]:
    if args[2] in servers:
        sys.exit("already exists")
    servers[args[2]] = json.loads(args[3])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w"))
else:
    sys.exit("fake muse: unexpected " + repr(args))
'''


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    muse = bindir / "muse"
    muse.write_text(f"#!{sys.executable}\n" + FAKE_MUSE)
    swarph = bindir / "swarph"
    swarph.write_text("#!/bin/sh\nexit 0\n")
    for tool in (muse, swarph):
        tool.chmod(0o755)
    environ = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    return environ, home / ".config/muse/settings.json", str(swarph)


def run(environ, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], env=environ, text=True,
                          capture_output=True, timeout=30)


def test_writes_one_literal_entry_with_mode_optional(env):
    environ, settings, swarph = env
    result = run(environ, "meta-muse", GATEWAY)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text()) == {"mcpServers": {"swarph": {
        "type": "stdio", "command": swarph, "args": ["mcp-server"],
        "env": {"SWARPH_SELF": "meta-muse", "MESH_GATEWAY_URL": GATEWAY},
        "mode": "optional"}}}


def test_rerun_is_a_no_op_and_other_values_are_refused(env):
    environ, settings, _ = env
    assert run(environ, "meta-muse", GATEWAY).returncode == 0
    before = settings.read_bytes()
    again = run(environ, "meta-muse", GATEWAY)
    assert again.returncode == 0 and "nothing to do" in again.stdout
    other = run(environ, "meta-muse", "http://127.0.0.1:9")
    assert other.returncode == 1 and "not replacing" in other.stderr
    other_cell = run(environ, "other-cell", GATEWAY)
    assert other_cell.returncode == 1
    assert settings.read_bytes() == before


@pytest.mark.parametrize("cell, gateway", [
    ("Meta;rm", GATEWAY), ("meta-muse-", GATEWAY), ("", GATEWAY),
    ("meta-muse", "ftp://x"), ("meta-muse", "http://a b"), ("meta-muse", ""),
    ("a" * 65, GATEWAY), ("\u00e1bc", GATEWAY),
    ("meta-muse", "http://user:pw@host:8788"), ("meta-muse", "http://host:8788/?token=t"),
    ("meta-muse", "http://host:8788/#frag"),
])
def test_invalid_input_refused_before_any_write(env, cell, gateway):
    environ, settings, _ = env
    # A UTF-8 locale must not widen [a-z] to accented letters.
    result = run({**environ, "LC_ALL": "en_US.UTF-8"}, cell, gateway)
    assert result.returncode == 1
    assert not settings.exists()


def test_gateway_defaults_to_mesh_gateway_url(env):
    environ, settings, _ = env
    result = run({**environ, "MESH_GATEWAY_URL": GATEWAY}, "meta-muse")
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["mcpServers"]["swarph"]["env"]["MESH_GATEWAY_URL"] == GATEWAY


def test_sixty_four_character_name_is_accepted(env):
    environ, settings, _ = env
    assert run(environ, "a" * 64, GATEWAY).returncode == 0


def _seed(settings, entry):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"mcpServers": {"swarph": entry}}))


@pytest.mark.parametrize("change", [
    {"mode": None}, {"mode": "required"}, {"command": "/other/swarph"},
    {"args": ["channel-serve"]}, {"enabled": False},
], ids=["no-mode", "mode-required", "other-command", "other-args", "disabled"])
def test_entry_differing_in_one_key_is_refused_unchanged(env, change):
    environ, settings, swarph = env
    entry = {"type": "stdio", "command": swarph, "args": ["mcp-server"],
             "env": {"SWARPH_SELF": "meta-muse", "MESH_GATEWAY_URL": GATEWAY}, "mode": "optional"}
    for key, value in change.items():
        if value is None:
            entry.pop(key)
        else:
            entry[key] = value
    _seed(settings, entry)
    before = settings.read_bytes()
    result = run(environ, "meta-muse", GATEWAY)
    assert result.returncode == 1 and "not replacing" in result.stderr
    assert settings.read_bytes() == before


def test_command_is_absolute_with_a_relative_path_entry(env, tmp_path):
    environ, settings, swarph = env
    bindir = Path(swarph).parent
    result = subprocess.run(["bash", str(SCRIPT), "meta-muse", GATEWAY], cwd=bindir.parent,
                            env={**environ, "PATH": f"{bindir.name}:/usr/bin:/bin"},
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["mcpServers"]["swarph"]["command"] == swarph


def test_follows_xdg_config_home_and_reruns_cleanly(env, tmp_path):
    environ, settings, _ = env
    xdg = tmp_path / "xdg"
    environ = {**environ, "XDG_CONFIG_HOME": str(xdg)}
    assert run(environ, "meta-muse", GATEWAY).returncode == 0
    written = xdg / "muse" / "settings.json"
    assert written.exists() and not settings.exists()
    again = run(environ, "meta-muse", GATEWAY)
    assert again.returncode == 0 and "nothing to do" in again.stdout, again.stderr
