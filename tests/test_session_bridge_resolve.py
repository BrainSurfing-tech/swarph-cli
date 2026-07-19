import swarph_cli.session_bridge as sb


class _CP:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def test_resolve_returns_claude_pane(monkeypatch):
    out = "%0 bash\n%1 claude\n"
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, out))
    assert sb.resolve_session_pane("lab-ovh") == "%1"


def test_resolve_returns_node_pane(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, "%2 node\n"))
    assert sb.resolve_session_pane("cell") == "%2"


def test_resolve_none_when_only_shell_panes(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, "%0 bash\n%1 vim\n"))
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_on_nonzero(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(1, ""))
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.resolve_session_pane("cell") is None
