"""`swarph watchdog --check --emit-health` — the last_health PRODUCER (mesh #26).

The watchdog computes a per-cell verdict every run. With --emit-health it POSTs that
verdict to the gateway's POST /peers/{self}/health, so a hung-but-alive cell (fresh
last_seen, null last_health) finally becomes visible. Default OFF: opt-in, zero behavior
change for existing installs. Best-effort: a failed emit never changes the check's exit
code, and the gateway token never lands in the log file.

Run: venv/bin/python -m pytest tests/test_watchdog_emit_health.py -v
"""
from swarph_cli.commands import watchdog as wd


# ── verdict → coarse status mapping ────────────────────────────────────────

def test_healthy_and_noop_map_to_healthy():
    for d in ("healthy_cursor_fresh", "noop_no_unread", "noop_pane_activity_recent"):
        assert wd._health_status_for(d) == "healthy", d


def test_action_and_error_verdicts_map_to_degraded():
    for d in ("a2_respawn_process_dead", "a1_send_keys", "a2_circuit_open",
              "a15_model_swap", "a2_respawn_tmux_missing"):
        assert wd._health_status_for(d) == "degraded", d


# ── _emit_health POSTs, and never raises ───────────────────────────────────

def test_emit_health_posts_status_and_detail(monkeypatch):
    seen = {}

    def fake_post(url, body, token, **k):
        seen["url"] = url
        seen["body"] = body
        seen["token"] = token
        return (200, {})

    monkeypatch.setattr(wd, "_post_json", fake_post, raising=False)
    wd._emit_health("http://gw:8788", "gpt-ops", "tok", "healthy_cursor_fresh")
    assert seen["url"] == "http://gw:8788/peers/gpt-ops/health"
    assert seen["body"]["status"] == "healthy"
    assert seen["body"]["detail"] == "healthy_cursor_fresh"
    assert seen["token"] == "tok"


def test_emit_health_never_raises_on_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("gateway down")
    monkeypatch.setattr(wd, "_post_json", boom, raising=False)
    # must not raise
    wd._emit_health("http://gw:8788", "gpt-ops", "tok", "a2_respawn_process_dead")


def test_emit_health_noop_without_token(monkeypatch):
    called = []
    monkeypatch.setattr(wd, "_post_json", lambda *a, **k: called.append(1) or (200, {}),
                        raising=False)
    wd._emit_health("http://gw:8788", "gpt-ops", None, "healthy_cursor_fresh")
    assert called == [], "no token → no POST (can't authenticate)"


# ── _log_event fires the process emitter exactly on a decision ─────────────

def test_log_event_emits_when_emitter_set_and_decision_present(monkeypatch, tmp_path):
    emitted = []
    monkeypatch.setattr(wd, "_HEALTH_EMITTER", lambda dec: emitted.append(dec))
    wd._log_event(tmp_path / "w.log", "noop", {"decision": "healthy_cursor_fresh"}, False)
    assert emitted == ["healthy_cursor_fresh"]


def test_log_event_no_emit_without_decision(monkeypatch, tmp_path):
    emitted = []
    monkeypatch.setattr(wd, "_HEALTH_EMITTER", lambda dec: emitted.append(dec))
    wd._log_event(tmp_path / "w.log", "error", {"error": "cursor unreadable"}, False)
    assert emitted == [], "an event without a decision must not emit health"


def test_log_event_no_emit_when_emitter_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "_HEALTH_EMITTER", None)
    # must not raise, must not emit
    wd._log_event(tmp_path / "w.log", "noop", {"decision": "healthy_cursor_fresh"}, False)


def test_emitter_failure_never_breaks_logging(monkeypatch, tmp_path):
    def boom(dec):
        raise RuntimeError("emitter exploded")
    monkeypatch.setattr(wd, "_HEALTH_EMITTER", boom)
    log = tmp_path / "w.log"
    wd._log_event(log, "noop", {"decision": "healthy_cursor_fresh"}, False)
    assert log.exists(), "the log line must still be written even if the emitter throws"


# ── flag parsing ───────────────────────────────────────────────────────────

def test_emit_health_flag_defaults_off():
    args = wd._build_parser().parse_args(["--check", "--cell", "gpt-ops"])
    assert args.emit_health is False


def test_emit_health_flag_parses():
    args = wd._build_parser().parse_args(["--check", "--cell", "gpt-ops", "--emit-health"])
    assert args.emit_health is True


def test_token_never_written_to_log(monkeypatch, tmp_path):
    """The gateway token lives in the process emitter closure, never in the diag,
    so it can't leak into the JSONL log."""
    monkeypatch.setattr(wd, "_HEALTH_EMITTER", lambda dec: None)
    log = tmp_path / "w.log"
    diag = {"decision": "healthy_cursor_fresh", "role": "gpt-ops"}
    wd._log_event(log, "noop", diag, False)
    assert "tok" not in log.read_text() and "token" not in log.read_text()
