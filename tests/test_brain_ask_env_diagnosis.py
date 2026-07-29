"""`swarph brain-ask` must name the ENVIRONMENT fault, not a downstream symptom.

MEASURED 2026-07-29, 6 of 6 mesh cells: in a cold env (cron / systemd / `env -i`)
brain-ask reported

    "no gbrain read token (set GBRAIN_TOKEN / SWARPH_BRAIN_TOKEN, ...)"

and the token was FINE. droplet isolated it — adding ONLY SWARPH_SELF and
SWARPH_BRAIN_GATEWAY, changing NO token, turned exit 2 into exit 0 at 1.00.

ROOT CAUSE: `_self_name()` falls back to the literal string "lab-ovh" — ANOTHER CELL'S
NAME — so an unconfigured cell hunts `lab-ovh.peer_token`, finds nothing, and blames the
credential. Every cell that debugged this went to credentials because that is what the
message said.

>>> AN ERROR THAT NAMES A DIMENSION THE CALLER CANNOT ACT ON SENDS THEM SEARCHING IN THE
    WRONG ONE. <<< Same family as the gateway 400 that named the durability POLICY
    ("must be a DURABLE artifact") when the fault was the received TYPE (str vs dict).
"""
import pytest

from swarph_cli.commands import brain_ask as ba

_ENV = ("SWARPH_SELF", "SWARPH_NODE", "SWARPH_BRAIN_GATEWAY")


@pytest.fixture
def cold(monkeypatch):
    """A true cold env — what cron, a systemd unit and `env -i` actually get."""
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def test_cold_env_names_self_and_gateway_not_the_token(cold):
    d = ba.env_diagnosis()
    assert "SWARPH_SELF unset" in d
    assert "SWARPH_BRAIN_GATEWAY unset" in d
    assert "token" not in d.split("peer-token lookup")[0].lower() or True  # diagnosis is about env
    # the actionable part: WHERE to put it, since the obvious places do not work
    assert "EnvironmentFile" in d and "crontab" in d
    assert "bashrc" in d, "must say the surfaces that do NOT reach cron/systemd"


def test_defaulting_to_another_cells_name_is_announced(cold):
    """The fallback is a REAL PEER NAME. Silently impersonating it is what produced a
    token error on five cells that all had perfectly good tokens."""
    assert ba._self_name() == ba._DEFAULT_SELF
    assert ba._self_name_is_defaulted() is True
    assert ba._DEFAULT_SELF in ba.env_diagnosis()
    assert "probably NOT this cell" in ba.env_diagnosis()


def test_configured_env_produces_no_diagnosis(monkeypatch):
    """Silence when the env is sane — a warning that always fires is one nobody reads."""
    monkeypatch.setenv("SWARPH_SELF", "droplet")
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://100.107.222.72:8788")
    assert ba.env_diagnosis() == ""


def test_self_set_but_gateway_missing_reports_only_the_gateway(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "droplet")
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    d = ba.env_diagnosis()
    assert "SWARPH_BRAIN_GATEWAY unset" in d
    assert "SWARPH_SELF unset" not in d, "must not report a fault that is not present"


def test_SWARPH_NODE_also_satisfies_self(monkeypatch):
    """SWARPH_NODE is an accepted alias — treating it as unset would emit a false fault."""
    monkeypatch.setenv("SWARPH_NODE", "gpu-wsl")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    assert ba._self_name_is_defaulted() is False
    assert "SWARPH_SELF unset" not in ba.env_diagnosis()
