import subprocess
import swarph_cli.session_bridge as sb


class _CP:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def _fake_capture(monkeypatch, rc, out):
    def fake_run(cmd, **kw):
        return _CP(rc, out)
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")


def test_probe_idle_on_footer_sentinel(monkeypatch):
    # Empty composer PLUS no busy marker. The sentinel alone is not idle —
    # that is the deadlock (#682): the hint vanishes when the box has text.
    _fake_capture(monkeypatch, 0, "some output\n> \n? for shortcuts\n")
    assert sb.probe_pane("%1", provider="claude") == "idle"


def test_probe_busy_on_esc_to_interrupt(monkeypatch):
    _fake_capture(monkeypatch, 0, "Thinking…\nesc to interrupt\n> \n")
    assert sb.probe_pane("%1", provider="claude") == "busy"


def test_probe_modal_on_safe_survey(monkeypatch):
    _fake_capture(monkeypatch, 0, "How is Claude doing this session?\n❯ 1. Bad\n")
    assert sb.probe_pane("%1", provider="claude") == "modal"


def test_claude_sentinel_with_text_in_the_input_box_is_not_idle(monkeypatch):
    """THE DEADLOCK. Today's code returns idle because the sentinel is
    present. The hint is the EMPTY-INPUT hint — any text hides it on a
    live pane; a capture that has BOTH is the synthetic case that proves
    we do not use sentinel-presence as idle.
    """
    _fake_capture(
        monkeypatch, 0,
        "> half-typed human line that nobody has submitted\n"
        "? for shortcuts\n",
    )
    got = sb.probe_pane("%1", provider="claude")
    assert got != "idle", (
        "text in the input box must not be idle; sentinel presence is the bug"
    )


def test_codex_idle_empty_composer(monkeypatch):
    _fake_capture(
        monkeypatch, 0,
        "› \n"
        "gpt-5.6-terra medium · ~/gpt-ops · Main [default]\n",
    )
    assert sb.probe_pane("%1", provider="codex") == "idle"


def test_codex_busy_on_interrupt_marker(monkeypatch):
    _fake_capture(
        monkeypatch, 0,
        "esc to interrupt\n"
        "› \n"
        "gpt-5.6-terra medium · ~/gpt-ops · Main [default]\n",
    )
    assert sb.probe_pane("%1", provider="codex") == "busy"


def test_cursor_idle_placeholder_composer(monkeypatch):
    _fake_capture(
        monkeypatch, 0,
        "→ Add a follow-up\n"
        "Cursor Grok 4.6 High Fast · 51.1% · 27 files edited\n",
    )
    assert sb.probe_pane("%1", provider="cursor") == "idle"


def test_unknown_provider_is_not_idle(monkeypatch):
    _fake_capture(monkeypatch, 0, "› \ngpt-5.6-terra medium · ~/gpt-ops · Main\n")
    got = sb.probe_pane("%1", provider="no-such-tui")
    assert got != "idle", "unknown provider must fail closed, never idle"


def test_no_provider_is_not_idle_even_when_the_claude_hint_is_present(monkeypatch):
    _fake_capture(monkeypatch, 0, "some output\n> \n? for shortcuts\n")
    got = sb.probe_pane("%1")
    assert got != "idle", "no-provider path must fail closed"


def test_probe_busy_on_capture_failure(monkeypatch):
    _fake_capture(monkeypatch, 1, "")
    assert sb.probe_pane("%1") == "busy"


def test_probe_busy_on_empty(monkeypatch):
    _fake_capture(monkeypatch, 0, "   \n")
    assert sb.probe_pane("%1") == "busy"


def test_probe_busy_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.probe_pane("%1") == "busy"


def test_dismiss_returns_false_when_no_safe_modal(monkeypatch):
    _fake_capture(monkeypatch, 0, "esc to interrupt\n")
    assert sb.try_dismiss_safe_modal("%1") is False
