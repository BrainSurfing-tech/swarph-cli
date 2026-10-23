"""Card #1243. The ten is read from origin/main by commit."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from swarph_cli.cell import SCHEMA_VERSION_V1
from swarph_cli.commands.hook_output import run_hook_output
from swarph_cli.the_ten import (
    cycle_4_close,
    cycle_4_open,
    cycle_bounds,
    discover_repo,
    law_citations,
    render_block,
    session_block,
    stamp_line,
    stamp_of,
    top_ten,
)

_LEAK = "LEAK_DO_NOT_INJECT"
_LOCAL = "LOCAL_MAIN_ONLY"
_BENCH = "bench bullet must stay out"
_ELEVENTH = "eleventh must stay out"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.check_output(
        [
            "git", "-C", str(repo),
            "-c", "user.name=grok-researcher",
            "-c", "user.email=grok-researcher@brainsurfing.tech",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        text=True,
    )
    return out.strip()


def _repo_with_origin_main(tmp_path: Path) -> tuple[Path, str]:
    """Local main and a dirty worktree stay on the old ten.

    ``refs/remotes/origin/main`` is a second commit. That is the only
    text ``session_block`` may return.
    """
    repo = tmp_path / "proven"
    (repo / "docs").mkdir(parents=True)
    subprocess.check_call(
        ["git", "init", "-b", "main", str(repo)],
        stdout=subprocess.DEVNULL,
    )
    ten = repo / "docs" / "THE_TEN.md"
    ten.write_text(
        f"1. **L1** · **{_LOCAL}**\n"
        "cycle 1 (2026-09-01): old stamp\n"
    )
    _git(repo, "add", "docs/THE_TEN.md")
    _git(repo, "commit", "-m", "old ten")
    lines = [f"{n}. **L{n}** · **law {n}**" for n in range(1, 11)]
    lines[3] = "4. **L4** · **re-derive before counting**"
    lines.append(f"- **L9** · **{_BENCH}**")
    lines.append(f"11. **L99** · **{_ELEVENTH}**")
    lines.append("cycle 1 (2026-09-01): old stamp")
    lines.append("cycle 2 (2026-09-29): fresh stamp")
    ten.write_text("\n".join(lines) + "\n")
    _git(repo, "add", "docs/THE_TEN.md")
    _git(repo, "commit", "-m", "current ten")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    _git(repo, "reset", "--hard", "HEAD~1")
    ten.write_text(ten.read_text() + f"\n{_LEAK}\n")
    return repo, sha


def _hook(capsys) -> dict:
    capsys.readouterr()
    rc = run_hook_output(argv=[])
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out)


def _cell(directory: Path) -> None:
    (directory / "starter.md").write_text("you are lab; act AI-to-AI.")
    (directory / "cell.yaml").write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test-cell",
        "role": "test",
        "cwd": str(directory),
        "starter_prompt_path": "starter.md",
        "provider": "claude",
    }))


def test_session_block_reads_origin_main_not_the_worktree(tmp_path):
    repo, sha = _repo_with_origin_main(tmp_path)
    block = session_block(repo)
    assert sha in block
    assert "cycle 2 (2026-09-29): fresh stamp" in block
    assert "L4 · re-derive before counting" in block
    assert _LEAK not in block
    assert _LOCAL not in block
    assert "old stamp" not in block
    assert _BENCH not in block
    assert _ELEVENTH not in block
    assert block.splitlines()[0] == f"THE_TEN {sha}"


def test_top_ten_keeps_ten_numbered_laws_and_drops_bullets():
    text = "\n".join(
        [f"{n}. **L{n}** · **law {n}**" for n in range(1, 12)]
        + ["- **B3** · **a bench row**"]
    )
    found = top_ten(text)
    assert len(found) == 10
    assert found[0] == ("L1", "law 1")
    assert all(label != "B3" for label, _title in found)
    assert "L11" not in [label for label, _title in found]


def test_law_citations_are_strict_and_first_seen():
    evidence = "used law=L4, then law=B12, then law=L4 again. flaw=L9 x-law=B2 law=L4extra"
    assert law_citations(evidence) == ["L4", "B12"]
    assert law_citations("") == []
    assert law_citations("no token here") == []
    assert law_citations("law=l4") == []


def test_stamp_line_records_a_quiet_week_as_no_change():
    assert stamp_line(4, "2026-10-26", None) == "cycle 4 (2026-10-26): no change"
    assert stamp_line(4, "2026-10-26", "") == "cycle 4 (2026-10-26): no change"
    assert stamp_line(4, "2026-10-26", "no change") == "cycle 4 (2026-10-26): no change"
    assert stamp_line(4, "2026-10-26", "L12 in") == "cycle 4 (2026-10-26): L12 in"


def test_stamp_of_uses_the_last_cycle_line_else_the_source_header():
    text = "cycle 1 (2026-09-01): old\ncycle 2 (2026-09-29): fresh\n"
    assert stamp_of(text) == "cycle 2 (2026-09-29): fresh"
    header = (
        "the commander, 2026-09-29\n"
        "Source: docs/prereg/RESULT-cycle-2.md\n"
    )
    assert stamp_of(header) == "cycle 2 (2026-09-29)"
    assert stamp_of("no header") == "cycle (no stamp)"
    assert render_block("a" * 40, "no laws here").splitlines()[-1] == (
        "THE_TEN: no top-ten block parsed"
    )


def test_cycle_bounds_are_monday_open_and_sunday_score_and_close():
    moment = datetime(2026, 10, 23, 15, 30, tzinfo=timezone.utc)
    bounds = cycle_bounds(moment)
    assert bounds["open"] == datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert bounds["open"].weekday() == 0
    assert bounds["score"] == datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
    assert bounds["close"] == datetime(2026, 10, 25, 23, tzinfo=timezone.utc)
    assert bounds["score"].weekday() == 6
    with pytest.raises(ValueError):
        cycle_bounds(datetime(2026, 10, 23, 15, 30))


def test_cycle_4_opens_on_26_october_only_when_both_gates_hold():
    opened = datetime(2026, 10, 26, tzinfo=timezone.utc)
    slipped = datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert cycle_4_open(commander_go=True, prereg_on_main=True) == opened
    assert cycle_4_close(commander_go=True, prereg_on_main=True) == datetime(
        2026, 11, 1, 23, tzinfo=timezone.utc
    )
    for go, on_main in ((True, False), (False, True), (False, False)):
        assert cycle_4_open(commander_go=go, prereg_on_main=on_main) == slipped
        assert cycle_4_close(commander_go=go, prereg_on_main=on_main) == datetime(
            2026, 11, 8, 23, tzinfo=timezone.utc
        )


def test_session_block_returns_empty_when_the_commit_cannot_be_read(tmp_path):
    assert session_block(tmp_path / "missing") == ""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main", str(repo)], stdout=subprocess.DEVNULL)
    assert session_block(repo) == ""


def test_discover_repo_is_off_when_the_env_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARPH_PROVEN_REPO", "")
    assert discover_repo() is None
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(tmp_path))
    assert discover_repo() is None
    repo, _sha = _repo_with_origin_main(tmp_path)
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(repo))
    assert discover_repo() == repo


def test_spawn_emits_the_ten_and_not_the_starter(tmp_path, monkeypatch, capsys):
    repo, sha = _repo_with_origin_main(tmp_path)
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SWARPH_SPAWN", "1")
    _cell(tmp_path)
    monkeypatch.chdir(tmp_path)
    ctx = _hook(capsys)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(f"THE_TEN {sha}")
    assert "AI-to-AI" not in ctx
    assert _LEAK not in ctx


def test_no_cell_still_emits_the_ten(tmp_path, monkeypatch, capsys):
    repo, sha = _repo_with_origin_main(tmp_path)
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    empty = tmp_path / "empty-cwd"
    empty.mkdir()
    monkeypatch.chdir(empty)
    ctx = _hook(capsys)["hookSpecificOutput"]["additionalContext"]
    assert f"THE_TEN {sha}" in ctx
    assert "AI-to-AI" not in ctx


def test_starter_and_ten_are_both_emitted(tmp_path, monkeypatch, capsys):
    repo, sha = _repo_with_origin_main(tmp_path)
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    _cell(tmp_path)
    monkeypatch.chdir(tmp_path)
    ctx = _hook(capsys)["hookSpecificOutput"]["additionalContext"]
    assert "AI-to-AI" in ctx
    assert f"THE_TEN {sha}" in ctx
    assert ctx.index("AI-to-AI") < ctx.index(f"THE_TEN {sha}")
    assert "\n\nTHE_TEN " in ctx


def test_empty_proven_repo_emits_the_starter_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_PROVEN_REPO", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    _cell(tmp_path)
    monkeypatch.chdir(tmp_path)
    ctx = _hook(capsys)["hookSpecificOutput"]["additionalContext"]
    assert "AI-to-AI" in ctx
    assert "THE_TEN" not in ctx


def test_a_missing_origin_main_does_not_block_the_starter(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "noref"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main", str(repo)], stdout=subprocess.DEVNULL)
    monkeypatch.setenv("SWARPH_PROVEN_REPO", str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    _cell(tmp_path)
    monkeypatch.chdir(tmp_path)
    parsed = _hook(capsys)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "AI-to-AI" in ctx
    assert "THE_TEN" not in ctx
