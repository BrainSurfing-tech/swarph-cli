"""Card #122 — the OBSERVATION CURSOR and the DELIVERY LEDGER are different questions.

This file generalizes tests/test_mesh_sidecar_cursor_decoupled.py, which fixed the
one-sink instance of the bug (PR #138): `last_msg_id` meant both "what I have read
from the gateway" and "what I have told the pane about", so a dead tmux pane froze
the cursor forever.

THE RULE, now expressed per-sink:
  * the observation cursor advances on OBSERVATION, always, gated on nothing;
  * each sink owns its OWN ledger, which advances only when THAT sink is satisfied
    and may lag arbitrarily far behind the cursor.

`--deliver none` is the falsifier for the whole model: it keeps no ledger at all, so
if any of this were secretly one variable, `none` would either stall the cursor or
grow a ledger. Both are asserted against below.

Run: .venv/bin/python -m pytest tests/test_monitor_state_split.py -v
"""
from __future__ import annotations

import json

import pytest

from swarph_cli.commands import mesh


def _state(tmp_path, sinks, *, min_interval_s=0, self_name="lab-ovh", replay_limit=50):
    return mesh.MonitorState(
        self_name=self_name,
        state_dir=tmp_path,
        gateway="http://gw:8788",
        token="tok",
        sinks=sinks,
        poll_s=90,
        min_interval_s=min_interval_s,
        replay_limit=replay_limit,
    )


def _window(*dms):
    def fake_get(url, token, **kw):
        return (200, {"messages": list(dms)})
    return fake_get


def _dm(msg_id, *, frm="droplet", read_at=None, content="hello"):
    return {
        "id": msg_id, "from_node": frm, "kind": "question",
        "content": content, "read_at": read_at,
    }


# ── sink parsing ─────────────────────────────────────────────────────────────

def test_parse_sink_covers_the_shipped_axis():
    assert mesh.parse_sink("pull").name == "pull"
    assert mesh.parse_sink("none").name == "none"
    assert mesh.parse_sink("stdout").name == "stdout"
    tmux = mesh.parse_sink("tmux:lab:0.0")
    assert tmux.name == "tmux:lab:0.0" and tmux.target == "lab:0.0"
    notify = mesh.parse_sink("tmux-notify:lab:0.0")
    assert notify.name == "tmux-notify:lab:0.0" and notify.target == "lab:0.0"


def test_tmux_notify_reports_delivery_without_waking_a_pane(monkeypatch, tmp_path):
    """A status-line notice is distinct from a prompt injection."""
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(5000)))
    notices = []
    monkeypatch.setattr(
        mesh, "_tmux_notify", lambda target, count: notices.append((target, count)) or True
    )
    monkeypatch.setattr(
        mesh, "_tmux_wake", lambda target: (_ for _ in ()).throw(AssertionError("must not wake"))
    )

    sink = mesh.parse_sink("tmux-notify:lab:0.0")
    state = _state(tmp_path, [sink])
    mesh._monitor_iteration(state)

    assert notices == [("lab:0.0", 1)]
    assert state.ledger(sink.name)["last_delivered_id"] == 5000


def test_pull_keeps_a_ledger_and_none_does_not():
    """The whole naming ruling (droplet DM #8532) in one assertion.

    A `none` that kept a ledger would be the fourth flag whose name and behaviour
    diverge; a `pull` that kept none could not answer "do I have DMs".
    """
    assert mesh.parse_sink("pull").keeps_ledger is True
    assert mesh.parse_sink("none").keeps_ledger is False


def test_unknown_sink_is_rejected_not_ignored():
    try:
        mesh.parse_sink("carrier-pigeon")
    except mesh.MonitorSinkError as exc:
        assert "carrier-pigeon" in str(exc)
    else:
        raise AssertionError("an unknown sink must not be silently dropped")


def test_webhook_sink_is_held_and_says_so():
    """The hold must be LOUD. A held feature that silently no-ops rots into a
    phantom capability: someone configures it, sees no error, believes it delivers."""
    try:
        mesh.parse_sink("webhook:https://example.test/hook")
    except mesh.MonitorSinkError as exc:
        msg = str(exc).lower()
        assert "held" in msg, "the error must name the GATE, not just fail to parse"
        assert "commander" in msg or "egress" in msg
    else:
        raise AssertionError("webhook: must not be accepted while the gate is held")


# ── the observation cursor ───────────────────────────────────────────────────

def test_cursor_advances_when_every_sink_fails(monkeypatch, tmp_path):
    """The core generalization: no sink, and no NUMBER of sinks, gates the cursor."""
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(5001)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)

    dead_a = mesh.parse_sink("tmux:gone-a:0.0")
    dead_b = mesh.parse_sink("tmux:gone-b:0.0")
    state = _state(tmp_path, [dead_a, dead_b])
    mesh._monitor_iteration(state)

    assert state.cursor["last_msg_id"] == 5001, (
        "observed => recorded, even with EVERY sink failing"
    )
    assert state.ledger("tmux:gone-a:0.0")["consecutive_failures"] == 1
    assert state.ledger("tmux:gone-b:0.0")["consecutive_failures"] == 1


def test_none_advances_the_cursor_and_creates_no_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(4242)))

    state = _state(tmp_path, [mesh.parse_sink("none")])
    mesh._monitor_iteration(state)

    assert state.cursor["last_msg_id"] == 4242
    assert state.ledgers == {}, "`none` means nothing: no ledger, no unread tracking"
    assert not (tmp_path / "ledgers.json").exists()
    log = (tmp_path / "inbox.log").read_text().splitlines()
    assert [json.loads(line)["id"] for line in log] == [4242], (
        "the archive is what makes a LATE-attached sink able to replay"
    )


# ── per-sink independence ────────────────────────────────────────────────────

def test_one_dead_sink_does_not_stall_another(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(700)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)

    dead = mesh.parse_sink("tmux:gone:0.0")
    live = mesh.parse_sink("stdout")
    state = _state(tmp_path, [dead, live])
    mesh._monitor_iteration(state)

    assert state.ledger("stdout")["last_delivered_id"] == 700, "the live sink is satisfied"
    assert state.ledger("tmux:gone:0.0")["last_delivered_id"] == 0, "the dead one still owes"
    assert state.ledger("tmux:gone:0.0")["consecutive_failures"] == 1


def test_failed_delivery_names_the_sink_on_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(701)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)

    state = _state(tmp_path, [mesh.parse_sink("tmux:gone:0.0")])
    mesh._monitor_iteration(state)

    err = capsys.readouterr().err
    assert "tmux:gone:0.0" in err, "a failure nobody can attribute is a failure nobody fixes"


# ── the ledger IS the retry mechanism (re-selection is not) ──────────────────

def test_owed_delivery_survives_an_empty_poll(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(9006)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)

    sink = mesh.parse_sink("tmux:pane:0.0")
    state = _state(tmp_path, [sink])
    mesh._monitor_iteration(state)
    assert state.ledger(sink.name)["last_delivered_id"] == 0

    woke = []
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)
    mesh._monitor_iteration(state)

    assert woke == ["pane:0.0"], "no NEW mail will ever arrive to carry the owed delivery"
    assert state.ledger(sink.name)["last_delivered_id"] == 9006


def test_owed_delivery_survives_a_process_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(9007)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)

    sink = mesh.parse_sink("tmux:pane:0.0")
    mesh._monitor_iteration(_state(tmp_path, [sink]))

    # A fresh state object == a restarted monitor reading its files off disk.
    revived = _state(tmp_path, [mesh.parse_sink("tmux:pane:0.0")])
    assert revived.cursor["last_msg_id"] == 9007, "no rewind"
    assert revived.ledger("tmux:pane:0.0")["last_delivered_id"] == 0, "still owed"
    assert revived.ledger("tmux:pane:0.0")["consecutive_failures"] == 1, (
        "the failure streak is durable too, or a restart loop looks healthy"
    )


def test_throttled_sink_does_not_lose_the_message_when_the_window_rolls(
    monkeypatch, tmp_path
):
    """The trap that made PR #138 non-trivial, now per-sink.

    Re-selection is NOT the retry mechanism: the gateway only returns the last 50
    messages, so a long-throttled delivery whose message has rolled out of the
    window would vanish. The ledger carries it instead.
    """
    now = [1000.0]
    monkeypatch.setattr(mesh.time, "time", lambda: now[0])
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(11)))
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    sink = mesh.parse_sink("tmux:pane:0.0")
    state = _state(tmp_path, [sink], min_interval_s=600)
    state.ledger(sink.name)["last_delivery_at"] = 999.0
    mesh._monitor_iteration(state)
    assert woke == [], "inside the guard window (precondition)"
    assert state.cursor["last_msg_id"] == 11

    # id 11 has now rolled out of the gateway's window entirely.
    now[0] = 5000.0
    monkeypatch.setattr(mesh, "_http_get_json", _window(*[_dm(i) for i in range(60, 110)]))
    mesh._monitor_iteration(state)
    assert woke == ["pane:0.0"], "the ledger carried it; re-selection never could"


# ── late-attached sink: bounded, REPORTED replay ─────────────────────────────

def test_late_attached_sink_replays_from_inbox_log(monkeypatch, tmp_path):
    """Novelty is a property of a SINK'S LEDGER, not of the cursor."""
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(31), _dm(32)))
    observer = _state(tmp_path, [mesh.parse_sink("none")])
    mesh._monitor_iteration(observer)

    printed = []
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    late = _state(tmp_path, [mesh.parse_sink("stdout")])
    monkeypatch.setattr(
        mesh.StdoutSink, "_emit", lambda self, line: printed.append(line)
    )
    mesh._monitor_iteration(late)

    assert any("31" in line for line in printed) and any("32" in line for line in printed), (
        "the data was never lost — only that sink's pointer is new"
    )
    assert late.ledger("stdout")["last_delivered_id"] == 32


def test_replay_is_bounded_and_says_what_it_skipped(monkeypatch, tmp_path, capsys):
    """A silent cap reads as 'delivered everything' when it did not."""
    monkeypatch.setattr(mesh, "_http_get_json", _window(*[_dm(i) for i in range(1, 21)]))
    mesh._monitor_iteration(_state(tmp_path, [mesh.parse_sink("none")]))

    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    late = _state(tmp_path, [mesh.parse_sink("stdout")], replay_limit=5)
    mesh._monitor_iteration(late)

    captured = capsys.readouterr()
    both = captured.out + captured.err
    assert "15" in both and "skip" in both.lower(), (
        f"the 15 skipped DMs must be REPORTED, not silently dropped; got: {both!r}"
    )
    assert late.ledger("stdout")["last_delivered_id"] == 20


def test_replay_helper_returns_the_newest_and_counts_the_rest(tmp_path):
    log = tmp_path / "inbox.log"
    log.write_text("".join(json.dumps(_dm(i)) + "\n" for i in range(1, 11)), encoding="utf-8")
    dms, skipped = mesh._replay_from_inbox_log(log, after_id=2, limit=3)
    assert [d["id"] for d in dms] == [8, 9, 10]
    assert skipped == 5, "ids 3..7 were dropped by the cap and must be counted"


# ── the `pull` sink: ledger advances on ACK, NEVER on observation ────────────

def test_pull_ledger_does_not_advance_on_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(50, read_at=None)))
    state = _state(tmp_path, [mesh.parse_sink("pull")])
    mesh._monitor_iteration(state)

    assert state.cursor["last_msg_id"] == 50, "observed"
    assert state.ledger("pull")["last_delivered_id"] == 0, (
        "nobody has READ it yet — observation is not delivery"
    )


def test_pull_ledger_advances_when_the_consumer_acks(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(50, read_at=None)))
    state = _state(tmp_path, [mesh.parse_sink("pull")])
    mesh._monitor_iteration(state)

    # `swarph mesh inbox` ran: the gateway now reports the DM as read.
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(50, read_at="2026-07-26T00:00:00Z")))
    mesh._monitor_iteration(state)

    assert state.ledger("pull")["last_delivered_id"] == 50, "the ACK is the delivery event"


def test_pull_ledger_stops_at_the_oldest_unread(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(
        _dm(60, read_at="z"), _dm(61, read_at=None), _dm(62, read_at="z"),
    ))
    state = _state(tmp_path, [mesh.parse_sink("pull")])
    mesh._monitor_iteration(state)

    assert state.cursor["last_msg_id"] == 62
    assert state.ledger("pull")["last_delivered_id"] == 60, (
        "id 61 is still unread; the ledger must not jump over it"
    )


# ── inbox.log is written whatever the sinks do ───────────────────────────────

def test_inbox_log_written_regardless_of_sink_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(80), _dm(81)))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)

    state = _state(tmp_path, [mesh.parse_sink("tmux:gone:0.0")])
    mesh._monitor_iteration(state)

    ids = [json.loads(line)["id"] for line in (tmp_path / "inbox.log").read_text().splitlines()]
    assert ids == [80, 81]


# ── upgrading a peer that is running the pre-#122 sidecar right now ──────────

def _legacy_cursor(tmp_path, **fields):
    base = {"last_msg_id": 8500, "last_wake_at": 1000.0, "pending_wake": False}
    base.update(fields)
    (tmp_path / "cursor.json").write_text(json.dumps(base), encoding="utf-8")


def test_pre_122_cursor_seeds_the_ledger_instead_of_replaying(monkeypatch, tmp_path):
    """`pending_wake` WAS the ledger. Adopt it, don't start from zero.

    Starting from zero would give every upgraded peer a redundant wake plus a
    capped-replay warning about mail it delivered days ago.
    """
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    _legacy_cursor(tmp_path, pending_wake=False)

    sink = mesh.parse_sink("tmux:pane:0.0")
    state = _state(tmp_path, [sink])
    assert state.ledger(sink.name)["last_delivered_id"] == 8500
    assert state.new_ledgers == set(), "a migrated ledger is not a NEW ledger"

    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)
    mesh._monitor_iteration(state)
    assert woke == [], "nothing is owed; the upgrade must be silent"


def test_pre_122_pending_wake_survives_the_upgrade(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    _legacy_cursor(tmp_path, pending_wake=True, last_wake_at=0.0)

    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)
    mesh._monitor_iteration(_state(tmp_path, [mesh.parse_sink("tmux:pane:0.0")]))

    assert woke == ["pane:0.0"], "a wake owed before the upgrade is still owed after"


def test_a_cursor_written_by_the_new_engine_is_not_migrated(monkeypatch, tmp_path):
    """The migration keys off `pending_wake`, which only the pre-#122 sidecar
    wrote — otherwise a late-attached sink would be seeded instead of replaying."""
    monkeypatch.setattr(mesh, "_http_get_json", _window(_dm(77)))
    mesh._monitor_iteration(_state(tmp_path, [mesh.parse_sink("none")]))
    assert "pending_wake" not in json.loads((tmp_path / "cursor.json").read_text())

    late = _state(tmp_path, [mesh.parse_sink("stdout")])
    assert late.ledger("stdout")["last_delivered_id"] == 0, "it must replay, not skip"


def test_idle_polls_do_not_churn_the_state_files(monkeypatch, tmp_path):
    """A monitor that rewrites its state every 30s for no reason is a monitor
    whose mtimes cannot be used to spot a stall.

    Ledgers ARE materialized once (found by driving the real CLI: a pure `pull`
    monitor otherwise never writes ledgers.json, so `status` shows the
    late-attached-sink warning forever on the DEFAULT path). Once is not churn.
    """
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    state = _state(tmp_path, [mesh.parse_sink("tmux:pane:0.0")])
    mesh._monitor_iteration(state)

    assert not (tmp_path / "cursor.json").exists(), "nothing was observed"
    ledgers = (tmp_path / "ledgers.json").read_text()

    for _ in range(4):
        mesh._monitor_iteration(state)
    assert (tmp_path / "ledgers.json").read_text() == ledgers, "idle == no rewrite"
    assert not (tmp_path / "cursor.json").exists()


# ── #454 cursor-print sink: DM content via cursor-agent --print, no send-keys ──

def test_parse_sink_cursor_print():
    sink = mesh.parse_sink("cursor-print:cursor-win")
    assert sink.name == "cursor-print:cursor-win"
    assert sink.cell_name == "cursor-win" and sink.is_push
    with pytest.raises(mesh.MonitorSinkError):
        mesh.parse_sink("cursor-print:")


def test_cursor_print_prompt_carries_the_FULL_body_not_the_160_char_display_line():
    """_format_inbox_line truncates for the terminal; delivery must not."""
    sink = mesh.parse_sink("cursor-print:cursor-win")
    body = "x" * 400
    prompt = sink._prompt([_dm(7, content=body)])
    assert body in prompt and "id=7" in prompt and "from=droplet" in prompt


def test_cursor_print_deliver_maps_confirmed_envelope_to_true(monkeypatch, tmp_path):
    import types as _t
    seen = {}
    monkeypatch.setattr("swarph_cli.cell.load_cell", lambda p: _t.SimpleNamespace(name="cursor-win"))
    monkeypatch.setattr("swarph_cli.cell.resolve_cell_path", lambda n: tmp_path / "c.yaml")
    def fake_print(cell, prompt, *, timeout):
        seen["prompt"], seen["timeout"] = prompt, timeout
        return 0
    monkeypatch.setattr("swarph_cli.commands.spawn.run_cursor_print", fake_print)
    sink = mesh.parse_sink("cursor-print:cursor-win")
    assert sink.deliver(None, [_dm(1), _dm(2)], 2) is True
    assert "2 new mesh DM(s)" in seen["prompt"]
    assert seen["timeout"] == sink.DEFAULT_TIMEOUT_S


def test_cursor_print_deliver_failure_leaves_the_ledger(monkeypatch, tmp_path):
    import types as _t
    monkeypatch.setattr("swarph_cli.cell.load_cell", lambda p: _t.SimpleNamespace(name="cursor-win"))
    monkeypatch.setattr("swarph_cli.cell.resolve_cell_path", lambda n: tmp_path / "c.yaml")
    monkeypatch.setattr("swarph_cli.commands.spawn.run_cursor_print", lambda *a, **k: 1)
    sink = mesh.parse_sink("cursor-print:cursor-win")
    assert sink.deliver(None, [_dm(1)], 1) is False


def test_cursor_print_deliver_never_claims_an_empty_recovery(monkeypatch):
    sink = mesh.parse_sink("cursor-print:cursor-win")
    assert sink.deliver(None, [], 9) is False


def test_cursor_print_deliver_unloadable_cell_is_visible_failure(monkeypatch, capsys):
    from swarph_cli.cell import CellError
    monkeypatch.setattr("swarph_cli.cell.resolve_cell_path", lambda n: (_ for _ in ()).throw(CellError("nope")))
    sink = mesh.parse_sink("cursor-print:cursor-win")
    assert sink.deliver(None, [_dm(1)], 1) is False
    assert "cannot load cell" in capsys.readouterr().err
