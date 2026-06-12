# tests/test_claude_tmux_template.py
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "sidecar" / "claude-tmux@.service"


def test_template_exists():
    assert TEMPLATE.is_file()


def test_killmode_is_mixed_not_deprecated_none():
    text = TEMPLATE.read_text()
    assert "KillMode=mixed" in text
    assert "KillMode=none" not in text  # science-claude AI² fix


def test_instance_name_passed_as_positional_not_interpolated_into_script():
    # INJECTION fix (drop seat-A): %i must NOT appear inside the sh -lc script
    # text (where `n=%i` let `a$(touch X)` execute). It is passed as a
    # POSITIONAL parameter (`sh %i` → `$1` → n="$1"), so the name is inert data.
    text = TEMPLATE.read_text()
    # inspect the ExecStart directive only (comments legitimately discuss the
    # old `n=%i` form when explaining the fix)
    exec_lines = [l for l in text.splitlines()
                  if l.startswith("ExecStart=") or (l.startswith(" ") and "swarph" in l)]
    execstart = "\n".join(exec_lines)
    # the script reads the name from $1, never `n=%i`
    assert 'n="$1"' in execstart
    assert "n=%i" not in execstart
    # %i is supplied as the trailing positional arg after the `sh` argv0
    assert "' sh %i" in execstart


def test_execstart_is_gated_on_verify_before_spawn():
    text = TEMPLATE.read_text()
    # droplet BLOCKING fix: per-UUID verify gates the per-name has-session;
    # verify also rejects any non-PEER_NAME_RE name → && short-circuits.
    assert 'swarph cell verify "$n"' in text
    verify_at = text.index('swarph cell verify "$n"')
    spawn_at = text.index("swarph spawn $n")
    assert verify_at < spawn_at  # verify must run BEFORE spawn
    assert 'tmux has-session -t "$n"' in text
    assert 'tmux new-session -d -s "$n"' in text


def test_has_explicit_execstop_kill_session():
    # ExecStop is exec'd directly (no shell) so %i is a literal tmux arg — safe.
    assert "ExecStop=/usr/bin/tmux kill-session -t %i" in TEMPLATE.read_text()


def test_is_a_systemd_template_using_instance_specifier():
    # `%i` instance specifier + `@` in filename = real template, not N hand units
    assert "%i" in TEMPLATE.read_text()
    assert TEMPLATE.name == "claude-tmux@.service"
