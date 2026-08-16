from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = ROOT / "src" / "swarph_cli" / "systemd"


def test_reply_drain_service_is_a_bounded_oneshot():
    unit = (SYSTEMD / "swarph-peer-reply-drain@.service").read_text()
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=" in unit
    assert "swarph peer-reply-drain" in unit
    assert "--spool-dir ${SWARPH_PEER_SPOOL_DIR}" in unit
    assert "--outbox-dir ${SWARPH_PEER_REPLY_OUTBOX_DIR}" in unit
    assert "send-keys" not in unit and "tmux" not in unit


def test_reply_drain_timer_targets_only_its_service():
    timer = (SYSTEMD / "swarph-peer-reply-drain@.timer").read_text()
    assert "Unit=swarph-peer-reply-drain@%i.service" in timer


def test_environment_file_requires_rendered_absolute_paths_not_specifiers():
    config = (SYSTEMD / "peer-reply-drain.default").read_text()
    values = [line for line in config.splitlines() if line.startswith("SWARPH_")]
    assert all("%h" not in line and "%i" not in line for line in values)
    assert "<ABSOLUTE_PATH_TO_PEER_SERVICE_SPOOL>" in config
    assert "<ABSOLUTE_PATH_TO_PEER_REPLY_OUTBOX>" in config
    assert "<ABSOLUTE_PATH_TO_PEER_TOKEN_FILE>" in config
