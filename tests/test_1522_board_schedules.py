"""Card #1522: `swarph board schedules [--json]` against a fake gateway."""
import json

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
    assert board._format_schedules({"hosts": {}}) == "(no schedules)"
