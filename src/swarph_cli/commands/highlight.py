"""``swarph highlight`` — append a highlight to the git-backed swarph timeline.

The swarph timeline is an append-only, multi-author ``TIMELINE.md``: every cell's
highlights converge into one file (a ``merge=union`` git attribute makes
concurrent appends auto-merge, never conflicting). This verb is the deterministic
MECHANICS — the JUDGMENT (what's worth a highlight, which memory it links to)
stays with the caller (a human or an agent).

Usage:
  swarph highlight "<one-line highlight>" [memory-pointer]
    [--cell NAME] [--timeline-dir DIR] [--when ISO8601] [--no-push]

Timeline dir: ``--timeline-dir`` > ``SWARPH_TIMELINE_DIR`` > ``~/.swarph/timeline``
  (auto-created + ``git init``'d + given a ``merge=union`` .gitattributes if absent).
Cell identity: ``--cell`` > ``SWARPH_CELL`` > git user.name > hostname.
Push: only if an ``origin`` remote exists and ``--no-push`` is not set; otherwise
  the highlight is committed locally (solo/offline timelines work).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from swarph_cli.commands.mesh import _post_json, _resolve_token

_PUSH_RETRIES = 8


def _resolve_gateway(arg: str | None) -> str:
    """Gateway base URL for the peer-token ingest path. Reuses the mesh-wide
    SWARPH_BRAIN_GATEWAY (already set on every cell from the brain-ask rollout)
    as the final fallback, so `swarph highlight` goes peer-token-by-default with
    ZERO new per-cell config. `--gateway` / `--local` override."""
    return (arg
            or os.environ.get("SWARPH_HIGHLIGHT_GATEWAY")
            or os.environ.get("SWARPH_GATEWAY")
            or os.environ.get("SWARPH_BRAIN_GATEWAY")
            or "").strip()


def _log_via_gateway(gateway: str, cell: str, highlight: str,
                     memory: str, when: str, token_file: str | None) -> int:
    """POST the highlight to the gateway `/highlights` — the gateway holds the git
    push credential, so the cell needs only its mesh peer token (no GitHub PAT).
    Fail-loud: a non-200 or connection error returns 1 (never a silent git double-write)."""
    url = gateway.rstrip("/") + "/highlights"
    body: dict = {"highlight": highlight, "cell": cell}
    if memory:
        body["memory"] = memory
    if when:
        body["when"] = when
    try:
        token = _resolve_token(cell, token_file)
    except RuntimeError as exc:
        print(f"swarph highlight: {exc}", file=sys.stderr)
        return 1
    status, resp = _post_json(url, body, token)
    if status == 200:
        ts = resp.get("ts", "")
        print(f"logged -> TIMELINE.md @ {ts} (via gateway)"
              + (f" -> {memory}" if memory else ""))
        if resp.get("pushed") is False:
            print("swarph highlight: committed at the gateway but NOT yet pushed "
                  "(converges on a later push)", file=sys.stderr)
        return 0
    detail = resp.get("detail") if isinstance(resp, dict) else resp
    where = f"HTTP {status}" if status else "connection failed"
    print(f"swarph highlight: gateway POST failed ({where}): {detail}", file=sys.stderr)
    return 1


def _collapse(s: str) -> str:
    """One-line invariant — an embedded newline can't forge a second
    attributed entry."""
    return s.replace("\n", " ").replace("\r", " ")


def _format_line(ts: str, cell: str, highlight: str, memory: str) -> str:
    line = f"- {ts} · **{cell}** · {highlight}"
    if memory:
        line += f" · → {memory}"
    return line


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _git(repo: Path, *args: str, check: bool = False):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _resolve_dir(arg) -> Path:
    raw = arg or os.environ.get("SWARPH_TIMELINE_DIR") or "~/.swarph/timeline"
    return Path(os.path.expanduser(raw))


def _is_git_repo(repo: Path) -> bool:
    if not repo.exists():
        return False
    r = _git(repo, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def _resolve_cell(arg, repo: Path) -> str:
    if arg:
        return _collapse(arg)
    env = os.environ.get("SWARPH_CELL")
    if env:
        return _collapse(env)
    if _is_git_repo(repo):
        r = _git(repo, "config", "user.name")
        if r.returncode == 0 and r.stdout.strip():
            return _collapse(r.stdout.strip())
    return socket.gethostname()


def _ensure_timeline(repo: Path, cell: str) -> None:
    """Create + git-init the timeline if absent, so a fresh user just works."""
    repo.mkdir(parents=True, exist_ok=True)
    if not _is_git_repo(repo):
        if _git(repo, "init", "-b", "main").returncode != 0:
            _git(repo, "init")  # older git without -b
        _git(repo, "config", "user.name", cell)
        _git(repo, "config", "user.email", "swarph@local")
    ga = repo / ".gitattributes"
    if not ga.exists() or "TIMELINE.md merge=union" not in ga.read_text(encoding="utf-8"):
        with ga.open("a", encoding="utf-8") as f:
            f.write("TIMELINE.md merge=union\n")
    tl = repo / "TIMELINE.md"
    if not tl.exists():
        tl.write_text("# swarph timeline — append-only, multi-author highlights\n\n",
                      encoding="utf-8")


def _has_remote(repo: Path) -> bool:
    return _git(repo, "remote", "get-url", "origin").returncode == 0


def _current_branch(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    b = r.stdout.strip()
    return b if (r.returncode == 0 and b and b != "HEAD") else "main"


def run_highlight(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph highlight",
        description="Append a highlight to the git-backed swarph timeline.")
    p.add_argument("highlight", help="the one-line highlight")
    p.add_argument("memory", nargs="?", default="",
                   help="optional memory pointer, e.g. [[some-memory]]")
    p.add_argument("--cell", default=None,
                   help="cell identity (else SWARPH_CELL / git user / hostname)")
    p.add_argument("--timeline-dir", default=None,
                   help="timeline repo (else SWARPH_TIMELINE_DIR / ~/.swarph/timeline)")
    p.add_argument("--when", default=None,
                   help="ISO8601 event time for a backfilled highlight; default now")
    p.add_argument("--no-push", action="store_true",
                   help="commit locally only, never push")
    p.add_argument("--gateway", default=None,
                   help="gateway base URL for the peer-token ingest path "
                   "(else SWARPH_HIGHLIGHT_GATEWAY / SWARPH_GATEWAY / SWARPH_BRAIN_GATEWAY)")
    p.add_argument("--token-file", default=None,
                   help="explicit bearer token file (else MESH_GATEWAY_TOKEN / peer-token)")
    p.add_argument("--local", action="store_true",
                   help="force the local git path even if a gateway is configured")
    args = p.parse_args(argv)

    repo = _resolve_dir(args.timeline_dir)
    cell = _resolve_cell(args.cell, repo)
    highlight = _collapse(args.highlight)
    memory = _collapse(args.memory)
    when = _collapse(args.when) if args.when else ""

    # Peer-token path (default when a gateway is configured): the gateway holds the
    # git credential, so a cell logs with only its mesh peer token — no GitHub PAT.
    # `--local` forces the legacy git path; with no gateway configured it's the git
    # path too, so existing solo/offline timelines are unaffected.
    gateway = "" if args.local else _resolve_gateway(args.gateway)
    if gateway:
        return _log_via_gateway(gateway, cell, highlight, memory, when, args.token_file)

    _ensure_timeline(repo, cell)
    ts = when or _now_ts()
    branch = _current_branch(repo)
    pushing = (not args.no_push) and _has_remote(repo)

    # Converge on a clean tree first (shared timeline).
    if pushing:
        if _git(repo, "pull", "--rebase", "origin", branch).returncode != 0:
            _git(repo, "rebase", "--abort")
            print("swarph highlight: pull --rebase failed (dirty tree / non-union "
                  "conflict) — NOT logged", file=sys.stderr)
            return 1

    with (repo / "TIMELINE.md").open("a", encoding="utf-8") as f:
        f.write(_format_line(ts, cell, highlight, memory) + "\n")
    _git(repo, "add", "TIMELINE.md")
    commit = _git(repo, "commit", "-m", f"highlight({cell}): {highlight[:60]}")
    if commit.returncode != 0:
        print(f"swarph highlight: commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 1

    done = f"logged -> TIMELINE.md @ {ts}" + (f" -> {memory}" if memory else "")
    if not pushing:
        print(done)
        return 0

    # Push, retrying the union-auto-merging rebase on a non-ff race.
    for _ in range(_PUSH_RETRIES):
        if _git(repo, "push", "origin", branch).returncode == 0:
            print(done)
            return 0
        if _git(repo, "pull", "--rebase", "origin", branch).returncode != 0:
            _git(repo, "rebase", "--abort")
            print("swarph highlight: rebase failed — committed locally, NOT pushed",
                  file=sys.stderr)
            return 1
    print("swarph highlight: push failed after retries — committed locally, NOT pushed",
          file=sys.stderr)
    return 1
