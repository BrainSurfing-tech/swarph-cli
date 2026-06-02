"""Assisted memory saver loop (Stage 2) + Restore helper."""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from swarph_cli.cell import Cell, load_cell, CellError


def get_memory_repo_path(cell: Cell) -> Path:
    # Memory repos are stored locally under ~/.local/share/swarph/memory/<role>
    return Path.home() / ".local" / "share" / "swarph" / "memory" / cell.role


def _get_files_to_sync(cell: Cell) -> list[tuple[str, Path]]:
    files_to_sync = []

    # Common
    if (cell.cwd / "CURRENT_TASK.md").exists():
        files_to_sync.append(("CURRENT_TASK.md", cell.cwd / "CURRENT_TASK.md"))

    if cell.provider == "claude":
        if (cell.cwd / "CLAUDE.md").exists():
            files_to_sync.append(("CLAUDE.md", cell.cwd / "CLAUDE.md"))
        mem_dir = Path.home() / ".claude"
        if (mem_dir / "MEMORY.md").exists():
            files_to_sync.append(("MEMORY.md", mem_dir / "MEMORY.md"))
        for p in mem_dir.glob("memory/*.md"):
            files_to_sync.append((f"memory/{p.name}", p))
        if (mem_dir / "inbox-cursor").exists():
            files_to_sync.append(("inbox-cursor", mem_dir / "inbox-cursor"))

    elif cell.provider == "codex":
        if (cell.cwd / "AGENTS.md").exists():
            files_to_sync.append(("AGENTS.md", cell.cwd / "AGENTS.md"))

    elif cell.provider == "antigravity":
        if (cell.cwd / "GEMINI.md").exists():
            files_to_sync.append(("GEMINI.md", cell.cwd / "GEMINI.md"))
        if (cell.cwd / "inbox-cursor.json").exists():
            files_to_sync.append(("inbox-cursor.json", cell.cwd / "inbox-cursor.json"))
        
        gemini_tmp = Path.home() / ".gemini" / "tmp"
        if gemini_tmp.is_dir():
            for proj_dir in gemini_tmp.iterdir():
                if proj_dir.is_dir():
                    for p in proj_dir.glob("memory/*.md"):
                        rel = f"tmp/{proj_dir.name}/memory/{p.name}"
                        files_to_sync.append((rel, p))

        history_proj = Path.home() / ".gemini" / "history" / ".project_root"
        if history_proj.exists():
            files_to_sync.append(("history/.project_root", history_proj))

    return files_to_sync


def _clone_if_missing(repo_url: str, repo_dir: Path) -> bool:
    if not (repo_dir / ".git").is_dir():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if "://" not in repo_url and "@" not in repo_url:
            repo_url = f"git@github.com:{repo_url}.git"
        try:
            subprocess.run(["git", "clone", repo_url, str(repo_dir)], check=True)
        except subprocess.CalledProcessError as exc:
            print(f"swarph: git clone failed: {exc}", file=sys.stderr)
            return False
    return True


def perform_restore(cell: Cell) -> Optional[str]:
    """Stage 3: Restore files from memory repo to the filesystem.
    
    Returns the text of CURRENT_TASK.md if it exists and was restored,
    so the caller can surface it to the context window.
    """
    am = cell.assisted_memory
    if not am or not am.get("enabled"):
        return None

    repo_url = am["repo"]
    repo_dir = get_memory_repo_path(cell)

    if not _clone_if_missing(repo_url, repo_dir):
        return None

    # pull --ff-only
    try:
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", "main"], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        print(f"swarph: memory pull --ff-only failed (diverged/offline?): {exc}", file=sys.stderr)
        return None

    # Walk the repo dir and copy everything back, EXCEPT .git and .gitignore
    for root, dirs, files in os.walk(repo_dir):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            if f == ".gitignore":
                continue
            
            src = Path(root) / f
            if f in ("CLAUDE.md", "AGENTS.md", "GEMINI.md") and src.stat().st_size == 0:
                print(f"swarph: SAFETY: remote {f} is empty. Refusing to restore and clobber local.", file=sys.stderr)
                continue

            rel = src.relative_to(repo_dir)
            
            dest = None
            if rel.parts[0] in ("CURRENT_TASK.md", "CLAUDE.md", "AGENTS.md", "GEMINI.md", "inbox-cursor.json"):
                dest = cell.cwd / rel
            elif cell.provider == "claude" and rel.parts[0] == "MEMORY.md":
                dest = Path.home() / ".claude" / "MEMORY.md"
            elif cell.provider == "claude" and rel.parts[0] == "memory":
                dest = Path.home() / ".claude" / rel
            elif cell.provider == "claude" and rel.parts[0] == "inbox-cursor":
                dest = Path.home() / ".claude" / "inbox-cursor"
            elif cell.provider == "antigravity" and rel.parts[0] == "tmp":
                dest = Path.home() / ".gemini" / rel
            elif cell.provider == "antigravity" and rel.parts[0] == "history":
                dest = Path.home() / ".gemini" / rel
                
            if dest:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    current_task = cell.cwd / "CURRENT_TASK.md"
    if current_task.exists():
        return current_task.read_text(encoding="utf-8")
    return None


def run_memory_sync(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="swarph memory-sync")
    parser.add_argument("cell_yaml", help="Path to cell.yaml")
    args = parser.parse_args(argv)

    try:
        cell = load_cell(Path(args.cell_yaml))
    except CellError as exc:
        print(f"swarph memory-sync: {exc}", file=sys.stderr)
        return 1

    am = cell.assisted_memory
    if not am or not am.get("enabled"):
        print("swarph memory-sync: assisted_memory not enabled for this cell. Exiting.", file=sys.stderr)
        return 0

    repo_url = am["repo"]
    repo_dir = get_memory_repo_path(cell)

    if not _clone_if_missing(repo_url, repo_dir):
        return 1

    gitignore_path = repo_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("secrets/\n.*creds*\n*.token\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", ".gitignore"], check=True)

    try:
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", "main"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass

    # EMPTY-GUARD check
    guard_file = None
    if cell.provider == "claude":
        guard_file = cell.cwd / "CLAUDE.md"
    elif cell.provider == "codex":
        guard_file = cell.cwd / "AGENTS.md"
    elif cell.provider == "antigravity":
        guard_file = cell.cwd / "GEMINI.md"

    if guard_file and (not guard_file.exists() or guard_file.stat().st_size == 0):
        print(f"[auto_sync] {datetime.datetime.now(datetime.timezone.utc).strftime('%FT%TZ')} SAFETY: {guard_file} missing or empty — skipping sync", file=sys.stderr)
        return 0

    files_to_sync = _get_files_to_sync(cell)
    for rel_path, abs_path in files_to_sync:
        dest = repo_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)
        subprocess.run(["git", "-C", str(repo_dir), "add", str(dest)], check=True)

    diff_check = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"])
    if diff_check.returncode != 0:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime('%FT%TZ')
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", f"auto-snapshot {ts}"], check=True, capture_output=True)
        try:
            subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            print(f"swarph memory-sync: push failed: {exc}", file=sys.stderr)

    return 0
