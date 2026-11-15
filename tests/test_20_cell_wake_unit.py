"""Deployment contract only: never installs, starts or restarts the unit."""
from pathlib import Path

import pytest

UNIT = Path(__file__).parents[1] / "src/swarph_cli/systemd/swarph-cell-wake@.service"


def settings():
    values = {}
    for line in UNIT.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    return values


@pytest.mark.parametrize("key,value", (
    ("User", "cell-%i"), ("Group", "cell-%i"),
    ("NoNewPrivileges", "yes"), ("LimitCORE", "0"),
    ("ProtectSystem", "strict"), ("StateDirectory", "swarph-cell-wake/%i"),
    ("StateDirectoryMode", "0755"), ("UMask", "0077"),
    ("Restart", "always"), ("RestartPreventExitStatus", "77 78 79 80"),
    ("RestrictAddressFamilies", "AF_UNIX"),
    ("UnsetEnvironment", "TMUX_TMPDIR TMUX"),
))
def test_unit_preserves_cell_boundary_and_refusal_policy(key, value):
    assert settings()[key] == [value]


def test_unit_exposes_own_socket_and_install_without_credential_file():
    config = settings()
    assert not {"EnvironmentFile", "PrivateTmp", "ProtectHome"} & config.keys()
    assert config["Environment"] == [
        "PYTHONDONTWRITEBYTECODE=1", "SWARPH_CELL_WAKE_PANE=%i:0.0"]
    command = config["ExecStart"][0].split()
    assert command[:3] == ["/home/cell-%i/.local/bin/swarph", "monitor", "wake-from-inbox"]
    assert command[3:] == ["--cell", "%i", "--inbox", "/var/log/swarph-proxy/%i/monitor/inbox.log",
                           "--pane", "${SWARPH_CELL_WAKE_PANE}"]
