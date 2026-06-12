# tests/test_claude_tmux_template.py
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "sidecar" / "claude-tmux@.service"


def test_template_exists():
    assert TEMPLATE.is_file()


def test_killmode_is_mixed_not_deprecated_none():
    text = TEMPLATE.read_text()
    assert "KillMode=mixed" in text
    assert "KillMode=none" not in text  # science-claude AI² fix


def test_execstart_is_gated_on_verify_before_spawn():
    text = TEMPLATE.read_text()
    execstart = next(l for l_no, l in enumerate(text.splitlines())
                     if l.startswith("ExecStart="))
    # %i is expanded ONCE into a shell var; later refs are quoted "$n"
    # (review fix: sidesteps nested-quote ambiguity in sh -lc)
    assert "n=%i" in execstart
    # droplet BLOCKING fix: per-UUID verify gates the per-name has-session
    assert 'swarph cell verify "$n"' in text
    verify_at = text.index('swarph cell verify "$n"')
    spawn_at = text.index("swarph spawn $n")
    assert verify_at < spawn_at  # verify must run BEFORE spawn
    assert 'tmux has-session -t "$n"' in text
    assert 'tmux new-session -d -s "$n"' in text


def test_has_explicit_execstop_kill_session():
    assert "ExecStop=/usr/bin/tmux kill-session -t %i" in TEMPLATE.read_text()


def test_is_a_systemd_template_using_instance_specifier():
    # `%i` instance specifier + `@` in filename = real template, not N hand units
    assert "%i" in TEMPLATE.read_text()
    assert TEMPLATE.name == "claude-tmux@.service"
