"""``swarph highlight`` — append a highlight to the git-backed swarph timeline.

The swarph timeline is an append-only, multi-author ``TIMELINE.md``: every cell's
highlights converge into one file (a ``merge=union`` git attribute makes
concurrent appends auto-merge, never conflicting). This verb is the deterministic
MECHANICS — the JUDGMENT (what's worth a highlight, which memory it links to)
stays with the caller (a human or an agent).

Usage:
  swarph highlight "<one-line highlight>" [memory-pointer]
    [--cell NAME] [--timeline-dir DIR] [--when ISO8601] [--no-push]

Timeline dir: ``--timeline-dir`` > ``SWARPH_TIMELINE_DIR`` / ``SWARPH_TIMELINE``
  > ``~/swarph-timeline`` (same default ``swarph timeline`` reads; #716).
  Auto-created + ``git init``'d + given a ``merge=union`` .gitattributes if absent.
Cell identity (#657 / house order #332): ``--cell`` > ``SWARPH_SELF`` >
``SWARPH_CELL`` > git user.name > hostname. SELF outranks CELL — psmux leaks
``SWARPH_CELL`` (#538), so CELL-first posts under another cell's name.
Push: only if an ``origin`` remote exists and ``--no-push`` is not set; otherwise
  the highlight is committed locally (solo/offline timelines work).
"""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from swarph_cli.commands.mesh import _post_json, _resolve_token
from swarph_cli.timeline_paths import timeline_dir

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


def _peer_token_near_match(name: str) -> str | None:
    """Case-insensitive near-match against on-disk peer tokens (#657 / #510 shape).

    When the resolved cell name has no credential, suggesting `mesh register`
    mints a DUPLICATE peer if the real name differs only in case (Lab-ovh vs
    lab-ovh). Prefer "did you mean …" over that advice.
    """
    root = Path.home() / ".config" / "swarph"
    if not root.is_dir():
        return None
    want = name.casefold()
    for path in sorted(root.glob("*.peer_token")):
        peer = path.name[: -len(".peer_token")]
        if peer.casefold() == want and peer != name:
            return peer
    return None


def _credential_error(cell: str, source: str, exc: RuntimeError) -> str:
    """Name WHERE the cell identity came from; never push register on a near-match."""
    near = _peer_token_near_match(cell)
    header = (
        f"swarph highlight: cannot resolve a mesh credential for {cell!r} "
        f"(resolved from {source})"
    )
    if near is not None:
        return (
            f"{header}.\n"
            f"  did you mean {near!r}? A peer token exists at "
            f"{Path.home() / '.config' / 'swarph' / (near + '.peer_token')}.\n"
            f"  Do NOT `swarph mesh register` under {cell!r} — that mints a "
            f"second identity for a cell that already has one (#657)."
        )
    # No near-match: keep the underlying resolver text (includes register advice
    # for a genuinely unknown name).
    return f"swarph highlight: {exc}"


def _normalise_repo(repo: Path) -> Path:
    return Path(os.path.normpath(os.path.expanduser(str(repo))))


def _existing_ancestor(repo: Path) -> Path:
    """Walk up to the nearest existing path. ``normpath`` first so
    ``x/../swarph-timeline`` (x missing) still names the clone (#65)."""
    repo = _normalise_repo(repo)
    while not repo.exists() and repo != repo.parent:
        repo = repo.parent
    return repo


def _git_gateway_clone_flag(repo: Path) -> bool:
    """``.git``-resident declaration. Survives clean/stash/reset; cannot be committed.

    ``git config --local swarph.gatewayClone 1`` (ops, once, on the gateway host).
    A worktree marker dies to ``git clean -fdx`` and propagates on ``git add -A``.
    Ask git on the nearest existing ancestor — a nonexistent subpath of the
    flagged clone is still the clone (#65).
    """
    repo = _existing_ancestor(repo)
    if not _is_git_repo(repo):
        return False
    r = _git(repo, "config", "--local", "--type=bool", "--get", "swarph.gatewayClone")
    return r.returncode == 0 and (r.stdout or "").strip().lower() == "true"


def _is_gateway_clone(repo: Path) -> bool:
    """True when ``repo`` is the live gateway working tree (PR #392 / #64 / #65).

    The property belongs to the directory, not the caller's environ.
    Honour env match (including subpaths) when present, OR
    ``swarph.gatewayClone`` in the repo's local git config. Walk up to an
    existing ancestor before either check so ``<clone>/newsub`` cannot
    mkdir-and-commit into the gateway history.
    """
    if _git_gateway_clone_flag(repo):
        return True
    raw = (os.environ.get("GATEWAY_TIMELINE_DIR") or "").strip()
    if not raw:
        return False
    try:
        flagged = Path(os.path.normpath(os.path.expanduser(raw))).resolve()
        cand = _existing_ancestor(repo).resolve()
    except OSError:
        return False
    if cand == flagged:
        return True
    try:
        return cand.is_relative_to(flagged) or flagged.is_relative_to(cand)
    except ValueError:
        return flagged in cand.parents or cand in flagged.parents


def _log_via_gateway(gateway: str, cell: str, highlight: str,
                     memory: str, when: str, token_file: str | None,
                     *, cell_source: str = "unknown") -> int | None:
    """POST the highlight to the gateway `/highlights` — the gateway holds the git
    push credential, so the cell needs only its mesh peer token (no GitHub PAT).

    Returns 0 on success. Returns 1 (no local write) on credential failure,
    any HTTP status ≠ 200 (401/422/502 are refusals, not "unreachable"), or
    a post-send exception (timeout, reset-after-read, bad status line,
    incomplete body, JSON/Unicode decode of a 200) — the client died after
    the request may have been committed; that is AMBIGUOUS, not a fallback.
    Returns None only when status == 0 (no HTTP response: refused/DNS/reset
    *before* send) so the caller may write locally and announce LOCAL
    FALLBACK (#716 / PR #392).

    The wrap is HERE, not in ``_post_json`` (17 callers). Catching OSError
    there and returning status 0 would turn a committed-but-unseen timeout
    into a duplicate local line. A reset belongs under status 0 only when
    it beats the send (URLError); post-send resets escape ``_post_json``.
    """
    url = gateway.rstrip("/") + "/highlights"
    body: dict = {"highlight": highlight, "cell": cell, "when": when}
    if memory:
        body["memory"] = memory
    try:
        token = _resolve_token(cell, token_file)
    except RuntimeError as exc:
        print(_credential_error(cell, cell_source, exc), file=sys.stderr)
        return 1
    try:
        status, resp = _post_json(url, body, token)
    except http.client.InvalidURL as exc:
        print(f"swarph highlight: bad gateway URL ({exc})", file=sys.stderr)
        return 1
    except (OSError, http.client.HTTPException, ValueError):
        print(
            "swarph highlight: AMBIGUOUS: the gateway may have committed — "
            f"check swarph timeline since {when} before retrying",
            file=sys.stderr,
        )
        return 1
    if status == 200:
        if not isinstance(resp, dict):
            print("swarph highlight: gateway POST failed (HTTP 200): "
                  "non-object body", file=sys.stderr)
            return 1
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
    if status == 0:
        return None  # no HTTP response — local fallback is the closed-port case
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
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def _resolve_dir(arg) -> Path:
    """Same default ``swarph timeline`` reads. Call-time — see timeline_paths."""
    return timeline_dir(dir_arg=arg)


def _is_git_repo(repo: Path) -> bool:
    if not repo.exists():
        return False
    r = _git(repo, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def _resolve_cell(arg, repo: Path) -> tuple[str, str]:
    """Return ``(cell, source)``.

    Order is the house rule (#332 / #538 / #657), matching
    ``memory_emit_hook._cell``: flag, then SWARPH_SELF, then SWARPH_CELL, then
    git user.name, then hostname. NEVER case-fold — the FAIL condition on #657
    is normalising case instead of fixing this order.
    """
    if arg:
        return _collapse(arg), "--cell"
    self_env = os.environ.get("SWARPH_SELF")
    if self_env:
        return _collapse(self_env), "$SWARPH_SELF"
    cell_env = os.environ.get("SWARPH_CELL")
    if cell_env:
        return _collapse(cell_env), "$SWARPH_CELL"
    if _is_git_repo(repo):
        r = _git(repo, "config", "user.name")
        if r.returncode == 0 and r.stdout.strip():
            return _collapse(r.stdout.strip()), "git user.name"
    return socket.gethostname(), "hostname"


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


def _revert_append(repo: Path, line: str) -> None:
    """Undo our failed add/commit without deleting another writer's bytes (#65).

    If the worktree already equals HEAD, our line was swept into their commit
    — leave it. Else strip our line from the tail only when HEAD does not
    already end with it. Then ``git reset -q -- TIMELINE.md``. Not
    checkout/restore: those are rc 128 under index.lock (the add-failure case).
    """
    if _git(repo, "diff", "--quiet", "HEAD", "--", "TIMELINE.md").returncode == 0:
        return
    our = line if line.endswith("\n") else line + "\n"
    tl = repo / "TIMELINE.md"
    try:
        body = tl.read_text(encoding="utf-8")
    except OSError:
        _git(repo, "reset", "-q", "--", "TIMELINE.md")
        return
    if body.endswith(our):
        head = _git(repo, "show", "HEAD:TIMELINE.md")
        head_txt = head.stdout if head.returncode == 0 else ""
        if not head_txt.endswith(our):
            try:
                tl.write_text(body[: -len(our)], encoding="utf-8")
            except OSError:
                pass
    _git(repo, "reset", "-q", "--", "TIMELINE.md")


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
                   help="cell identity (else SWARPH_SELF / SWARPH_CELL / "
                        "git user / hostname)")
    p.add_argument("--timeline-dir", default=None,
                   help="timeline repo (else SWARPH_TIMELINE_DIR / "
                        "SWARPH_TIMELINE / ~/swarph-timeline)")
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
    cell, cell_source = _resolve_cell(args.cell, repo)
    highlight = _collapse(args.highlight)
    memory = _collapse(args.memory)
    # Always stamp `when` so a gateway commit and a local fallback are
    # byte-identical (merge=union collapses the duplicate). The gateway
    # accepts the client's minute verbatim.
    when = _collapse(args.when) if args.when else _now_ts()

    # Peer-token path (default when a gateway is configured): the gateway holds the
    # git credential, so a cell logs with only its mesh peer token — no GitHub PAT.
    # `--local` forces the legacy git path; with no gateway configured it's the git
    # path too, so existing solo/offline timelines are unaffected.
    gateway = "" if args.local else _resolve_gateway(args.gateway)
    fallback = False
    if gateway:
        gw_rc = _log_via_gateway(gateway, cell, highlight, memory, when,
                                 args.token_file, cell_source=cell_source)
        if gw_rc is not None:
            return gw_rc
        fallback = True

    if fallback and _is_gateway_clone(repo):
        print("swarph highlight: gateway unreachable; not writing into the "
              "gateway's clone; retry or use --timeline-dir", file=sys.stderr)
        return 1

    _ensure_timeline(repo, cell)
    ts = when
    branch = _current_branch(repo)
    pushing = (not args.no_push) and _has_remote(repo)

    # Converge on a clean tree first (shared timeline).
    if pushing:
        if _git(repo, "pull", "--rebase", "origin", branch).returncode != 0:
            _git(repo, "rebase", "--abort")
            print("swarph highlight: pull --rebase failed (dirty tree / non-union "
                  "conflict) — NOT logged", file=sys.stderr)
            return 1

    tl = repo / "TIMELINE.md"
    entry = _format_line(ts, cell, highlight, memory) + "\n"
    with tl.open("a", encoding="utf-8") as f:
        f.write(entry)
    add = _git(repo, "add", "TIMELINE.md")
    if add.returncode != 0:
        _revert_append(repo, entry)
        print(f"swarph highlight: git add failed: {add.stderr.strip()}", file=sys.stderr)
        return 1
    commit = _git(repo, "commit", "-m", f"highlight({cell}): {highlight[:60]}")
    if commit.returncode != 0:
        _revert_append(repo, entry)
        print(f"swarph highlight: commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 1

    if fallback:
        done = f"logged -> {repo} (LOCAL FALLBACK — gateway unreachable)."
        from swarph_cli.timeline_paths import timeline_file
        wrote = repo / "TIMELINE.md"
        if wrote.resolve() != timeline_file().resolve():
            done += "\n`swarph timeline` will NOT show this until it is drained."
        if not _has_remote(repo):
            done += ("\nno origin remote — this commit stays on this filesystem "
                     "until it is drained.")
    else:
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
