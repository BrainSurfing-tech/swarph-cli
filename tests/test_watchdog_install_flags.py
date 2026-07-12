"""`--install-service` must be SAFE-BY-DEFAULT and PROPAGATE the operator's flags.

FOOTGUN (droplet, 2026-07-12, board #28): the generated ExecStart was built by a
template substitution that only added `--cell <cell>` and DROPPED every runtime flag
the operator passed (--no-respawn, --no-model-rung, --activity-marker, --threshold, ...).
So `--install-service --cell X --no-respawn` installed a service whose ExecStart was
`swarph watchdog --check --cell X` — respawn ENABLED, no marker — the exact 2026-06-11
false-fire shape. The 'safe staging' install produced the MOST AGGRESSIVE config.

Fix: (1) the generated ExecStart carries `--no-respawn` BY DEFAULT — arming the
destructive A2 respawn requires an explicit `--arm-respawn`; and (2) the operator's
passed runtime flags are propagated into ExecStart (what you install is what you asked).

The dry-run preview writes the unit contents to stderr, so we assert on the ExecStart line.
Run: venv/bin/python -m pytest tests/test_watchdog_install_flags.py -v
"""
import pytest

from swarph_cli.commands.watchdog import run_watchdog


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Pin TMPDIR + XDG_STATE_HOME under tmp_path; clear MESH_GATEWAY_TOKEN.
    (Mirrors the fixture in test_watchdog.py, which is module-local there.)"""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    yield tmp_path


def _execstart(capsys, *extra):
    rc = run_watchdog(argv=["--install-service", "--cell", "x", "--dry-run", *extra])
    assert rc == 0
    err = capsys.readouterr().err
    line = next((l for l in err.splitlines() if "watchdog --check --cell x" in l), None)
    assert line is not None, "no ExecStart line in dry-run preview"
    return line


# ── safe-by-default: respawn is NOT armed unless explicitly asked ──────────

def test_bare_install_is_safe_by_default(isolated_state, capsys):
    line = _execstart(capsys)
    assert "--no-respawn" in line, "a bare install must NOT arm the destructive A2 respawn"


def test_explicit_no_respawn_present(isolated_state, capsys):
    assert "--no-respawn" in _execstart(capsys, "--no-respawn")


def test_arm_respawn_omits_no_respawn(isolated_state, capsys):
    line = _execstart(capsys, "--arm-respawn")
    assert "--no-respawn" not in line, "--arm-respawn is the explicit opt-in to arm A2"


def test_arm_plus_no_respawn_safe_wins(isolated_state, capsys):
    """If both are passed, safety wins — the recovery layer fails to conservative."""
    line = _execstart(capsys, "--arm-respawn", "--no-respawn")
    assert "--no-respawn" in line


# ── propagation: what you install is what you asked for ───────────────────

def test_propagates_activity_marker_and_threshold(isolated_state, capsys):
    line = _execstart(capsys, "--activity-marker", "/tmp/x-active.txt", "--threshold", "900")
    assert "--activity-marker /tmp/x-active.txt" in line
    assert "--threshold 900" in line


def test_propagates_gateway_and_peer(isolated_state, capsys):
    line = _execstart(capsys, "--gateway", "http://lab-ovh:8788", "--peer", "droplet")
    assert "--gateway http://lab-ovh:8788" in line
    assert "--peer droplet" in line


def test_propagates_model_rung_optin_and_omits_by_default(isolated_state, capsys):
    assert "--model-rung" in _execstart(capsys, "--model-rung")
    assert "--model-rung" not in _execstart(capsys), "model-rung must not appear unless asked"


def test_propagates_no_model_rung(isolated_state, capsys):
    assert "--no-model-rung" in _execstart(capsys, "--no-model-rung")


def test_propagates_emit_health_and_peer_health_poll(isolated_state, capsys):
    line = _execstart(capsys, "--emit-health", "--peer-health-poll")
    assert "--emit-health" in line
    assert "--peer-health-poll" in line


def test_propagates_nonclaude_process_name(isolated_state, capsys):
    """A grok/codex cell must not be liveness-checked as if it ran `claude`."""
    line = _execstart(capsys, "--process-name", "grok")
    assert "--process-name grok" in line


def test_default_process_name_not_emitted(isolated_state, capsys):
    """The default (claude) needn't clutter the ExecStart."""
    assert "--process-name" not in _execstart(capsys)


def test_execstart_still_has_check_and_cell(isolated_state, capsys):
    line = _execstart(capsys, "--no-respawn")
    assert "watchdog --check --cell x" in line, "existing --cell contract preserved"
