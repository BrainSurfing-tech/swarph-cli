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
    _fake_capture(monkeypatch, 0, "some output\n? for shortcuts\n")
    assert sb.probe_pane("%1") == "idle"


def test_probe_busy_on_esc_to_interrupt(monkeypatch):
    _fake_capture(monkeypatch, 0, "Thinking…\nesc to interrupt\n")
    assert sb.probe_pane("%1") == "busy"


def test_probe_modal_on_safe_survey(monkeypatch):
    _fake_capture(monkeypatch, 0, "How is Claude doing this session?\n❯ 1. Bad\n")
    assert sb.probe_pane("%1") == "modal"


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


def test_codex_probe_uses_only_the_current_composer_line(monkeypatch):
    assert sb._codex_composer_state("old output\n›\nnot a composer\n") == "busy"
    assert sb._codex_composer_state("old output\n›\n") == "idle"
    assert sb._codex_composer_state("old output\n› preserve this draft\n") == "draft"


def test_codex_probe_defers_on_unreadable_capture(monkeypatch):
    monkeypatch.setattr(sb, "_capture", lambda pane: None)
    assert sb.probe_codex_pane("%1") == "busy"


def test_codex_requires_two_blank_idle_captures(monkeypatch):
    captures = iter(["›\n", "›\n"])
    monkeypatch.setattr(sb, "_capture", lambda pane: next(captures))
    monkeypatch.setattr(sb.time, "sleep", lambda _: None)
    assert sb.codex_stable_state("%1") == "idle"


def test_codex_changed_second_capture_is_not_idle(monkeypatch):
    captures = iter(["›\n", "› keep my draft\n"])
    monkeypatch.setattr(sb, "_capture", lambda pane: next(captures))
    monkeypatch.setattr(sb.time, "sleep", lambda _: None)
    assert sb.codex_stable_state("%1") == "unstable-idle"
