from swarph_cli.capture import liveness


def test_holder_alive_when_session_and_pid_live(monkeypatch):
    monkeypatch.setattr(liveness, "_tmux_has_session", lambda h: True)
    monkeypatch.setattr(liveness, "_pane_pids", lambda h: [4242])
    monkeypatch.setattr(liveness, "_process_alive", lambda pid: True)
    assert liveness.probe_holder_liveness("droplet") is True


def test_holder_dead_when_no_session(monkeypatch):
    # tmux session gone entirely → holder dead (the common crash case)
    monkeypatch.setattr(liveness, "_tmux_has_session", lambda h: False)
    assert liveness.probe_holder_liveness("droplet") is False


def test_holder_dead_when_session_lingers_but_pane_pid_gone(monkeypatch):
    # poison-pin variant: tmux session exists but its claude pane process died
    monkeypatch.setattr(liveness, "_tmux_has_session", lambda h: True)
    monkeypatch.setattr(liveness, "_pane_pids", lambda h: [9999])
    monkeypatch.setattr(liveness, "_process_alive", lambda pid: False)
    assert liveness.probe_holder_liveness("droplet") is False


def test_holder_dead_when_no_pane_pids(monkeypatch):
    monkeypatch.setattr(liveness, "_tmux_has_session", lambda h: True)
    monkeypatch.setattr(liveness, "_pane_pids", lambda h: [])
    assert liveness.probe_holder_liveness("droplet") is False


def test_falsy_holder_is_not_live(monkeypatch):
    assert liveness.probe_holder_liveness(None) is False
    assert liveness.probe_holder_liveness("") is False
