import swarph_cli.session_bridge as sb


def test_sanitize_collapses_newlines_and_strips_control():
    # Newlines collapse to single spaces; the ESC control byte (\x1b) is
    # STRIPPED so no ANSI interpretation is possible. The security property is
    # "no ESC byte reaches the pane" (a broken ANSI seq's residual bracket
    # chars are inert literal text) — not "all bracket chars removed".
    out = sb._sanitize("hello\n\nworld \x1b more  x")
    assert out == "hello world more x"
    assert "\x1b" not in out and "\n" not in out


def test_sanitize_empty():
    assert sb._sanitize("") == ""


def test_inject_sends_literal_then_enter(monkeypatch):
    calls = []

    class _CP:
        returncode = 0

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _CP()

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    assert sb.inject("%1", "reply now") is True
    # exactly two calls: literal body (-l) then a bare Enter
    assert calls[0] == ["tmux", "send-keys", "-t", "%1", "-l", "reply now"]
    assert calls[1] == ["tmux", "send-keys", "-t", "%1", "Enter"]


def test_inject_defangs_leading_slash(monkeypatch):
    calls = []

    class _CP:
        returncode = 0

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: (calls.append(cmd) or _CP()))
    sb.inject("%1", "/model haiku")
    # leading slash is space-prefixed so the TUI never reads a slash-command
    assert calls[0] == ["tmux", "send-keys", "-t", "%1", "-l", " /model haiku"]


def test_inject_false_on_nonzero_exit(monkeypatch):
    class _CP:
        returncode = 1

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP())
    assert sb.inject("%1", "x") is False


def test_inject_false_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.inject("%1", "x") is False
