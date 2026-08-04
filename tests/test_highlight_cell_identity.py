"""Cell identity on a SHARED timeline must never be guessed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarph_cli.commands import highlight


def _init(repo: Path, *, remote: bool) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "clone-owner"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "o@x"], check=True)
    if remote:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://example.invalid/t.git"], check=True)


def test_shared_timeline_refuses_to_guess_the_cell(tmp_path, monkeypatch):
    """>>> A FALLBACK DEFAULT ON A SHARED RESOURCE IS AN IDENTITY CLAIM MADE ON
    THE CALLER'S BEHALF. <<<

    `git config user.name` on a shared clone is the CLONE OWNER, not the caller.
    Every cell that omitted SWARPH_CELL published under that one name — measured
    on the real mesh timeline as 5 entries whose git author is the real cell
    while the label reads the owner's. The commons is append-only, so the
    mislabel is permanent.
    """
    monkeypatch.delenv("SWARPH_CELL", raising=False)
    repo = tmp_path / "shared"
    _init(repo, remote=True)
    with pytest.raises(ValueError) as exc:
        highlight._resolve_cell(None, repo)
    assert "SHARED" in str(exc.value)
    assert "clone-owner" not in str(exc.value)  # never echo the wrong identity as a suggestion


def test_local_only_timeline_still_falls_back(tmp_path, monkeypatch):
    """Scoped to remotes ON PURPOSE: a local timeline is nobody else's record, so
    the fallback has no victim and 'a fresh solo user just works' is preserved.
    Pins that the fix did NOT over-reach."""
    monkeypatch.delenv("SWARPH_CELL", raising=False)
    repo = tmp_path / "solo"
    _init(repo, remote=False)
    assert highlight._resolve_cell(None, repo) == "clone-owner"


def test_explicit_identity_always_wins(tmp_path, monkeypatch):
    repo = tmp_path / "shared2"
    _init(repo, remote=True)
    monkeypatch.setenv("SWARPH_CELL", "from-env")
    assert highlight._resolve_cell(None, repo) == "from-env"
    assert highlight._resolve_cell("from-arg", repo) == "from-arg"
