"""card #1071 (rulings #1403) — provider usage limits: pane parsers on
captured fixtures, PUT-on-change writer, once-per-crossing alerts.

The fixtures are the survey lines lab-ovh captured read-only on 10-09
(card link pane_survey_1009), embedded verbatim. A parser whose fixture
line is absent fails closed to unknown — never 0%, never blocked.
"""
from datetime import datetime, timezone

import pytest

from swarph_cli import quota_sync as qs

NOW = datetime(2026, 10, 10, 21, 30, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()

COPILOT_PANE = """some shell noise
Plan: no limit · Session: 14.4 AIC used
$ """

CODEX_BOUND_PANE = """⚠ Heads up, you have less than 25% of your 5h limit left. Run /status for details
› """

CODEX_LIMIT_PANE = """⚠ Heads up, you have less than 1% of your 5h limit left.
You've hit your usage limit. Try again at 9:45 PM.
› """

GROK_MODAL_PANE = """Something went wrong
Upgrade tier / Buy more credits / Try Again
"""

MISTRAL_PANE = """Error: Rate limits exceeded for this model, upgrade to Pro for higher limits.
$ """

CLAUDE_PANE = """❯ do the thing
⏵⏵ auto mode on · model opus
"""

AGY_PANE = """Antigravity Starter Quota
❯ _
"""


# ── PARSERS ───────────────────────────────────────────────────────────────

def test_copilot_credits_only_never_percent():
    rec = qs.parse_pane_capture(COPILOT_PANE, AS_OF)
    assert rec == {"used_pct": None, "credits": 14.4, "reset_at": None,
                   "blocked": False, "source": "pane", "as_of": AS_OF}


def test_codex_bound_is_not_a_used_pct():
    rec = qs.parse_pane_capture(CODEX_BOUND_PANE, AS_OF)
    assert rec["used_pct"] is None and rec["blocked"] is False
    assert rec["bound_left_pct"] == 25.0


def test_codex_hit_limit_beats_the_bound():
    rec = qs.parse_pane_capture(CODEX_LIMIT_PANE, AS_OF)
    assert rec["used_pct"] == 100 and rec["blocked"] is True
    assert rec["reset_at"] == "9:45 PM" and rec["source"] == "pane"


def test_grok_modal_is_blocked_without_figures():
    rec = qs.parse_pane_capture(GROK_MODAL_PANE, AS_OF)
    assert rec == {"used_pct": None, "credits": None, "reset_at": None,
                   "blocked": True, "source": "pane", "as_of": AS_OF}


def test_mistral_rate_limit_is_source_error():
    rec = qs.parse_pane_capture(MISTRAL_PANE, AS_OF)
    assert rec["blocked"] is True and rec["source"] == "error"
    assert rec["used_pct"] is None and rec["reset_at"] is None


@pytest.mark.parametrize("pane", [CLAUDE_PANE, AGY_PANE, "", "   \n  "])
def test_unknown_panes_fail_closed_to_none(pane):
    assert qs.parse_pane_capture(pane, AS_OF) is None


def test_service_log_limit_line_marks_blocked():
    rec = qs.parse_service_log_tail(
        "2026-10-10T21:00:01Z ERROR usage limit reached, try again at 21:05:00Z\n",
        AS_OF)
    assert rec["blocked"] is True and rec["source"] == "error"
    assert rec["reset_at"] == "21:05:00Z" and rec["used_pct"] == 100


def test_service_log_without_limit_line_is_none():
    assert qs.parse_service_log_tail("all quiet\n", AS_OF) is None


# ── DECIDE ────────────────────────────────────────────────────────────────

def test_first_sight_writes_used_with_as_of():
    body = qs.decide_put(None, {"used_pct": 82.5, "credits": None,
                                "reset_at": None, "blocked": False,
                                "source": "pane", "as_of": AS_OF}, NOW)
    assert body == {"quota_used_pct": 82.5, "quota_as_of": AS_OF}


def test_unchanged_values_write_nothing():
    prior = {"quota_used_pct": 82.5, "quota_as_of": AS_OF,
             "unavailable_until": None}
    body = qs.decide_put(prior, {"used_pct": 82.5, "credits": None,
                                 "reset_at": None, "blocked": False,
                                 "source": "pane", "as_of": AS_OF}, NOW)
    assert body is None


def test_credits_only_read_writes_nothing_and_invents_nothing():
    body = qs.decide_put(None, {"used_pct": None, "credits": 15.1,
                                "reset_at": None, "blocked": False,
                                "source": "pane", "as_of": AS_OF}, NOW)
    assert body is None


def test_blocked_with_reset_sets_until_to_reset():
    body = qs.decide_put(None, {"used_pct": 100, "credits": None,
                                "reset_at": "9:45 PM", "blocked": True,
                                "source": "pane", "as_of": AS_OF}, NOW)
    assert body == {"quota_used_pct": 100, "quota_as_of": AS_OF,
                    "unavailable_until": "9:45 PM"}


def test_blocked_without_reset_refreshes_five_minutes():
    body = qs.decide_put(
        {"quota_used_pct": None, "quota_as_of": AS_OF,
         "unavailable_until": "2026-10-10T21:30:00+00:00"},
        {"used_pct": None, "credits": None, "reset_at": None,
         "blocked": True, "source": "pane", "as_of": AS_OF}, NOW)
    assert body == {"quota_as_of": AS_OF,
                    "unavailable_until": "2026-10-10T21:35:00+00:00"}


def test_non_blocked_read_clears_a_prior_block():
    body = qs.decide_put(
        {"quota_used_pct": 100, "quota_as_of": "old",
         "unavailable_until": "2026-10-10T21:35:00+00:00"},
        {"used_pct": 40.0, "credits": None, "reset_at": None,
         "blocked": False, "source": "pane", "as_of": AS_OF}, NOW)
    assert body == {"quota_used_pct": 40.0, "quota_as_of": AS_OF,
                    "unavailable_until": None}


def test_empty_capture_never_clears():
    prior = {"quota_used_pct": 100, "quota_as_of": "old",
             "unavailable_until": "2026-10-10T21:35:00+00:00"}
    assert qs.decide_put(prior, None, NOW) is None


# ── ALERTS ────────────────────────────────────────────────────────────────

def _alerts(state, **kw):
    rec = {"used_pct": None, "bound_left_pct": None, "blocked": False}
    rec.update(kw)
    return qs.check_alerts(state, rec)


def test_first_sight_above_thresholds_fires_each_once():
    fired, state = _alerts({}, used_pct=96.0)
    assert [f[0] for f in fired] == [80, 95]
    fired2, _ = qs.check_alerts(state, {"used_pct": 97.0,
                                        "bound_left_pct": None,
                                        "blocked": False})
    assert fired2 == []


def test_drop_below_re_arms():
    _, state = _alerts({}, used_pct=96.0)
    fired, state2 = qs.check_alerts(state, {"used_pct": 50.0,
                                            "bound_left_pct": None,
                                            "blocked": False})
    assert fired == []
    fired, _ = qs.check_alerts(state2, {"used_pct": 81.0,
                                        "bound_left_pct": None,
                                        "blocked": False})
    assert [f[0] for f in fired] == [80]


def test_codex_bound_fires_only_past_its_floor():
    fired, _ = _alerts({}, bound_left_pct=25.0)
    assert fired == []
    fired, _ = _alerts({}, bound_left_pct=10.0)
    assert [f[0] for f in fired] == [80]
    fired, _ = _alerts({}, bound_left_pct=3.0)
    assert [f[0] for f in fired] == [80, 95]


def test_blocked_fires_limit_once_then_re_arms():
    fired, state = _alerts({}, blocked=True)
    assert [f[0] for f in fired] == ["limit"]
    fired, state = qs.check_alerts(state, {"used_pct": None,
                                           "bound_left_pct": None,
                                           "blocked": True})
    assert fired == []
    _, state = qs.check_alerts(state, {"used_pct": 10.0,
                                       "bound_left_pct": None,
                                       "blocked": False})
    fired, _ = qs.check_alerts(state, {"used_pct": None,
                                       "bound_left_pct": None,
                                       "blocked": True})
    assert [f[0] for f in fired] == ["limit"]


def test_credits_never_fire_percent_alerts():
    fired, _ = _alerts({}, used_pct=None)
    assert fired == []


# ── SYNC ENGINE ───────────────────────────────────────────────────────────

def test_sync_skips_peers_read_within_five_minutes():
    calls = []

    def capture(peer):
        calls.append(peer)
        return COPILOT_PANE

    out = qs.sync_once(
        peers=["a", "b"], capture=capture,
        get_roster=lambda: [],
        put_roster=lambda peer, body: None,
        send_alert=lambda peer, text: None,
        state={"peers": {"a": {"last_read": NOW.isoformat(), "arm80": True,
                               "arm95": True, "arm_limit": True}}},
        now=NOW)
    assert calls == ["b"]
    assert out["peers"]["a"]["skipped"] == "read-cooldown"


def test_sync_puts_on_change_and_alerts_on_crossing():
    puts, alerts = [], []
    out = qs.sync_once(
        peers=["c"], capture=lambda peer: CODEX_LIMIT_PANE,
        get_roster=lambda: [{"peer": "c", "quota_used_pct": 40.0,
                             "quota_as_of": "old", "unavailable_until": None}],
        put_roster=lambda peer, body: puts.append((peer, body)),
        send_alert=lambda peer, text: alerts.append((peer, text)),
        state={"peers": {}}, now=NOW)
    assert puts == [("c", {"quota_used_pct": 100, "quota_as_of": AS_OF,
                           "unavailable_until": "9:45 PM"})]
    assert len(alerts) == 3  # 80, 95, limit
    assert out["peers"]["c"]["put"] is True


def test_sync_unknown_capture_writes_nothing():
    puts, alerts = [], []
    out = qs.sync_once(
        peers=["d"], capture=lambda peer: CLAUDE_PANE,
        get_roster=lambda: [{"peer": "d", "quota_used_pct": 40.0,
                             "quota_as_of": "old", "unavailable_until": None}],
        put_roster=lambda peer, body: puts.append((peer, body)),
        send_alert=lambda peer, text: alerts.append((peer, text)),
        state={"peers": {}}, now=NOW)
    assert puts == [] and alerts == []
    assert out["peers"]["d"]["record"] == "unknown"


# ── COMMAND SURFACE ───────────────────────────────────────────────────────

def test_sync_parser_flags():
    from swarph_cli.commands import roster
    args = roster._build_parser().parse_args(
        ["sync", "--peer", "a", "--peer", "b",
         "--service-log", "g=./g.log", "--state", "/tmp/q.json"])
    assert args.command == "sync"
    assert args.peer == ["a", "b"]
    assert args.service_log == ["g=./g.log"]
    assert args.state == "/tmp/q.json"


def test_service_log_bad_shape_refused(tmp_path):
    from swarph_cli.commands import roster
    with pytest.raises(ValueError):
        roster._parse_service_logs(["no-equals-here"])
    log = tmp_path / "svc.log"
    log.write_text("x" * 70000 + "usage limit hit, try again at T\n")
    out = roster._parse_service_logs([f"g={log}"])
    assert out["g"].endswith("try again at T\n")
    assert len(out["g"]) <= 65536 + 100  # tail only, not the whole file


def test_run_sync_dispatches_and_saves_state(tmp_path, monkeypatch, capsys):
    from swarph_cli.commands import roster
    monkeypatch.setattr(roster, "_resolve", lambda args: "tok")
    monkeypatch.setattr(roster, "_http_get_json",
                        lambda url, tok, **k: (200, {"roster": []}))
    puts = []

    def _fake_put(url, body, tok, **k):
        puts.append((url, body))
        return 200, {}

    monkeypatch.setattr(roster, "_http_put_json", _fake_put)
    monkeypatch.setattr(roster, "_capture_peer_pane",
                        lambda peer: CODEX_LIMIT_PANE)
    monkeypatch.setattr(roster, "_post_message",
                        lambda gw, tok, to, content: True)
    state_path = tmp_path / "q.json"
    rc = roster.run_roster(["sync", "--peer", "c", "--gateway", "http://gw",
                            "--state", str(state_path)])
    assert rc == 0
    assert "ALERT limit" in capsys.readouterr().out
    assert (tmp_path / "q.json").exists()
    assert puts and puts[0][0].endswith("/roster/c")
    assert puts[0][1]["quota_used_pct"] == 100
