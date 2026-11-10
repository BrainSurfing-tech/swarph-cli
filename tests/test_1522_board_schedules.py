"""Card #1522: `swarph board schedules [--json]` against a fake gateway."""
import json
import pytest

from swarph_cli.commands import board

PAYLOAD = {"hosts": {"Lab-ovh": {
    "host": "Lab-ovh", "received_at": "2026-11-09T16:00:00Z",
    "jobs": [{"host": "Lab-ovh", "unit_or_cron_line": "certbot.timer",
              "schedule": "*-*-* 00,12:00:00", "last_run": "2026-11-09T08:20:38Z",
              "last_result": "success", "next_run": "2026-11-09T22:14:22Z",
              "owner_cell": None, "card": None}]}}}


def _run(monkeypatch, capsys, argv, payload=PAYLOAD):
    seen = {}

    def fake_get(url, token, **k):
        seen["url"] = url
        seen["token"] = token
        return 200, payload

    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "SECRET-TOKEN-1522")
    monkeypatch.setattr(board, "_http_get_json", fake_get)
    rc = board.run_board(argv)
    out = capsys.readouterr()
    return rc, out, seen


def test_json_verbatim_and_parses(monkeypatch, capsys):
    rc, out, seen = _run(monkeypatch, capsys, ["schedules", "--json"])
    assert rc == 0
    assert seen["url"].endswith("/board/schedules")
    assert json.loads(out.out) == PAYLOAD
    assert out.out.strip() == json.dumps(PAYLOAD, indent=2)
    assert "SECRET-TOKEN-1522" not in out.out and "SECRET-TOKEN-1522" not in out.err


def test_human_listing(monkeypatch, capsys):
    rc, out, seen = _run(monkeypatch, capsys, ["schedules"])
    assert rc == 0
    assert "Lab-ovh" in out.out and "certbot.timer" in out.out
    assert "2026-11-09T22:14:22Z" in out.out
    assert "SECRET-TOKEN-1522" not in out.out and "SECRET-TOKEN-1522" not in out.err


def test_format_schedules_empty():
    assert board._format_schedules({"hosts": {}}) == \
        "(no snapshot from any host since gateway restart)"


def test_no_snapshot_vs_no_jobs_differ(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["schedules"], payload={"hosts": {}})
    assert rc == 0
    restart_msg = out.out.strip()
    assert "no snapshot" in restart_msg and "gateway restart" in restart_msg
    rc, out, _ = _run(monkeypatch, capsys, ["schedules"],
                      payload={"hosts": {"h": {"jobs": [], "host": "h"}}})
    assert rc == 0
    no_jobs_msg = out.out.strip()
    assert "no jobs" in no_jobs_msg
    assert restart_msg != no_jobs_msg


def test_format_schedules_no_jobs_per_host():
    assert board._format_schedules(
        {"hosts": {"h": {"jobs": []}}}) == "h  (no jobs)"


def test_redacted_collector_rows_sentinel_never_printed(monkeypatch, capsys):
    payload = {"hosts": {"Lab-ovh": {
        "host": "Lab-ovh", "received_at": "2026-11-09T16:00:00Z",
        "jobs": [{"host": "Lab-ovh",
                  "unit_or_cron_line":
                      "MESH_GATEWAY_URL=<redacted> /usr/bin/job "
                      "--token-file <redacted>",
                  "schedule": "*/30 * * * *", "last_run": None,
                  "last_result": None, "next_run": None,
                  "owner_cell": "meta-muse", "card": 1522}]}}}
    rc, out, _ = _run(monkeypatch, capsys, ["schedules"], payload=payload)
    assert rc == 0
    assert "SENTINEL-DO-NOT-LEAK-1522" not in out.out
    assert "SENTINEL-DO-NOT-LEAK-1522" not in out.err
    assert "MESH_GATEWAY_URL=<redacted>" in out.out
    assert "--token-file <redacted>" in out.out


def test_no_jobs_host_is_sanitized_and_one_line(monkeypatch, capsys):
    payload = {"hosts": {"\x1b[31mhost\nFORGED\rentry": {"jobs": []}}}
    rc, out, _ = _run(monkeypatch, capsys, ["schedules"], payload=payload)
    assert rc == 0 and len(out.out.splitlines()) == 1
    assert "\x1b" not in out.out and "\r" not in out.out
    assert out.out == "[31mhost FORGED entry  (no jobs)\n"


@pytest.mark.parametrize("field", ["host", "unit_or_cron_line", "schedule", "next_run"])
@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\t", "\u2028", "\u2029"])
def test_each_job_field_stays_on_one_line(field, separator):
    job = {"host": "h", "unit_or_cron_line": "job", "schedule": "daily", "next_run": "tomorrow"}
    job[field] = "before" + separator + "after\x1b[31m"
    out = board._format_schedules({"hosts": {"h": {"jobs": [job]}}})
    assert len(out.splitlines()) == 1
    assert "\x1b" not in out and "\t" not in out and "\r" not in out
    expected = "before" + ("  " if separator == "\r\n" else " ") + "after[31m"
    assert expected in out


def test_fallback_host_key_is_also_single_line():
    job = {"unit_or_cron_line": "job", "schedule": "daily", "next_run": None}
    out = board._format_schedules({"hosts": {"host\nforged": {"jobs": [job]}}})
    assert out == "host forged  job  daily  next=-"


def test_null_next_run_uses_dash_without_changing_other_fields():
    job = {"host": "h", "unit_or_cron_line": "job", "schedule": "daily", "next_run": None}
    assert board._format_schedules({"hosts": {"h": {"jobs": [job]}}}) == "h  job  daily  next=-"
    job["next_run"] = ""
    assert board._format_schedules({"hosts": {"h": {"jobs": [job]}}}) == "h  job  daily  next="
