"""session_state_path() charset gate — PR #65 fast-follow (drop seat-A residual).

The one role->path builder + entry (spawn's requested_role CLI arg) that the
capture-layer validate_role did not cover. Gating the builder closes it for
every caller in one choke point."""
import pytest

from swarph_cli.cell import session_state_path, CellError
from swarph_cli.commands import spawn


@pytest.mark.parametrize("bad", [
    "../../../../tmp/x/forged",
    "../../etc/cron.d/evil",
    "a/b",
    "a$(touch X)",
    "a;touch X",
    "a b",
    "UPPER",
    "",
    "-lead",
    "trail-",
])
def test_session_state_path_refuses_unsafe_role(bad):
    with pytest.raises(CellError):
        session_state_path(bad)


@pytest.mark.parametrize("good", ["lab", "lab-test", "lab-test-2", "drop-on-meta-edge", "a1"])
def test_session_state_path_accepts_kebab(good):
    # builds a path, no raise
    assert str(session_state_path(good)).endswith(f"{good}.session-id")


def test_spawn_metachar_requested_role_is_refused(tmp_path, monkeypatch, capsys):
    # `swarph spawn 'a$(touch X)'` — the CLI requested_role flows into
    # session_state_path via sidecar_role; the builder now refuses it, and
    # run_spawn catches CellError → exit 1 (fail-closed, no path written).
    import yaml as _yaml
    config_root = tmp_path / "config"
    (config_root / "swarph" / "cells").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # plant a cell.yaml whose FILE resolves but whose requested_role is hostile.
    # (resolve_cell_path treats a bare metachar token as <cells>/<token>.yaml;
    # we make that file exist so resolution succeeds and the role reaches the
    # sidecar path builder — proving the builder is the backstop.)
    evil_name = "a$(touch X)"
    (config_root / "swarph" / "cells" / f"{evil_name}.yaml").write_text(_yaml.safe_dump({
        "schema_version": "v1", "name": "evil-cell", "role": "evil-cell",
        "cwd": str(tmp_path), "provider": "claude",
    }))
    rc = spawn.run_spawn([evil_name, "--no-banner"])
    # cell.role is gated at load_cell too, but the point: no crash, fail-closed
    assert rc == 1


def test_spawn_metachar_requested_role_refused_even_when_pinned(tmp_path, monkeypatch):
    # PR #67 (drop seat-A): the PINNED-session_id early-return in
    # load_or_create_session_id used to skip the #66 name-gate — it returns
    # `role` (the CLI positional) directly without routing through
    # session_state_path(). A cell.yaml with a CLEAN internal role FIELD (passes
    # load_cell) + a pinned session_id would hand the unvalidated requested_role
    # straight to claude --name / tmux -s <name>. The entry-gate in
    # load_or_create_session_id must refuse it here too. (Without the fix this
    # asserts rc==1 but gets rc==0 — the exact false-green the original test hid
    # by omitting session_id.)
    import yaml as _yaml
    config_root = tmp_path / "config"
    (config_root / "swarph" / "cells").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    evil_name = "a$(touch X)"
    (config_root / "swarph" / "cells" / f"{evil_name}.yaml").write_text(_yaml.safe_dump({
        "schema_version": "v1", "name": "evil-cell", "role": "evil-cell",
        "cwd": str(tmp_path), "provider": "claude",
        # the load-cell-clean cell.yaml PINS a session_id → hits the pinned
        # early-return branch that previously bypassed the name-gate.
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
    }))
    rc = spawn.run_spawn([evil_name, "--no-banner"])
    assert rc == 1  # pinned branch now gated too — fail-closed, no metachar to the sink
