"""Card #1436: dev-extract must not read fresh from an untrusted local ref.

Each case runs the real ``swarph board cards dev-extract`` verb against a
temporary git repo. Git is not mocked. On main, cases 1–4 read fresh because
``merged_at`` is the local ``origin/main`` time, and the healthy case does
not name the commit the report described or the ref it compared against.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OLD = "2020-01-01T00:00:00Z"
FED_STAMP = "2026-11-02T04:45:01Z"
SLUG = "BrainSurfing-tech/swarph-cli"

_GIT = (
    "-c", "user.name=cursor-lin",
    "-c", "user.email=cursor-lin@brainsurfing.tech",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=always",
    "-c", "init.defaultBranch=main",
)


def _env(tmp: Path, extra: dict | None = None) -> dict:
    cfg = tmp / "empty-gitconfig"
    if not cfg.exists():
        cfg.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(cfg)
    env["GIT_CONFIG_SYSTEM"] = str(cfg)
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str, env_extra: dict | None = None, tmp: Path | None = None) -> str:
    home = tmp or repo
    out = subprocess.run(
        ["git", "-C", str(repo), *_GIT, *args],
        check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=_env(home, env_extra),
    )
    return (out.stdout or "").strip()


def _commit(repo: Path, tmp: Path, when: str, msg: str) -> str:
    dated = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    _git(repo, "add", "-A", env_extra=dated, tmp=tmp)
    _git(repo, "commit", "-m", msg, env_extra=dated, tmp=tmp)
    return _git(repo, "rev-parse", "HEAD", tmp=tmp)


def _bare_at(tmp: Path, owner: str, name: str = "swarph-cli.git") -> Path:
    bare = tmp / "remotes" / owner / name
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git_init = subprocess.run(
        ["git", *_GIT, "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True, text=True, env=_env(tmp),
    )
    assert _git_init.returncode == 0
    return bare


def _seed_and_clone(tmp: Path, bare: Path, when: str = OLD, *,
                    fetch: bool = True) -> tuple[Path, str]:
    """Push one old commit to ``bare`` and clone it. Local origin/main is that commit."""
    seed = tmp / f"seed-{bare.parent.name}"
    seed.mkdir()
    subprocess.run(
        ["git", *_GIT, "init", "-b", "main", str(seed)],
        check=True, capture_output=True, text=True, env=_env(tmp),
    )
    (seed / "README").write_text("v1\n", encoding="utf-8")
    sha = _commit(seed, tmp, when, "v1")
    _git(seed, "remote", "add", "origin", str(bare), tmp=tmp)
    _git(seed, "push", "-u", "origin", "main", tmp=tmp)
    work = tmp / f"work-{bare.parent.name}"
    subprocess.run(
        ["git", *_GIT, "clone", str(bare), str(work)],
        check=True, capture_output=True, text=True, env=_env(tmp),
    )
    # A local clone does not write FETCH_HEAD. One fetch records the
    # time while origin/main is still this commit.
    if fetch:
        _git(work, "fetch", "--no-tags", "origin", tmp=tmp)
    return work, sha


def _push_at(tmp: Path, bare: Path, work_seed_name: str, when: str, msg: str) -> str:
    seed = tmp / work_seed_name
    (seed / "README").write_text(msg + "\n", encoding="utf-8")
    sha = _commit(seed, tmp, when, msg)
    _git(seed, "push", "origin", "main", tmp=tmp)
    return sha


def _push_newer(tmp: Path, bare: Path, work_seed_name: str) -> str:
    return _push_at(tmp, bare, work_seed_name, "2026-11-03T12:00:00Z",
                    "remote merge")


def _index(db: Path, repo_path: Path, slug: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE repos(name TEXT, slug TEXT, path TEXT, visibility TEXT,"
        " indexed_at TEXT, mode TEXT)")
    con.execute(
        "CREATE TABLE symbols(id INTEGER PRIMARY KEY, repo TEXT, name TEXT,"
        " kind TEXT, file_path TEXT, start_line INTEGER, qualified_name TEXT,"
        " signature TEXT)")
    con.execute(
        "CREATE TABLE edges(src_symbol INTEGER, dst_symbol INTEGER,"
        " edge_type TEXT, repo TEXT)")
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        ("swarph-cli", slug, str(repo_path), "public",
         "2026-11-01T00:00:00Z", "full"))
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)",
        (1, "swarph-cli", "chat_sync", "function", "src/chat.py", 1,
         "chat_sync", ""))
    con.commit()
    con.close()


def _report(path: Path, sha: str) -> None:
    path.write_text(
        "# swarph federation — cross-repo connection map\n"
        f"# generated {FED_STAMP} by fed_refresh.sh (card #44 part 4)\n"
        f"# repo swarph-cli {sha} {FED_STAMP}\n"
        "\n"
        "  Layer 2:\n",
        encoding="utf-8",
    )


def _ws(path: Path) -> None:
    path.write_text('{"model": {"softwareSystems": []}}\n', encoding="utf-8")


def _token(path: Path) -> None:
    path.write_text("dev-extract-test-token\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _verb(tmp: Path, index: Path, report: Path, ws: Path, *,
          print_only: bool = True, extra: list | None = None,
          timeout: int = 60) -> subprocess.CompletedProcess:
    token = tmp / "token"
    _token(token)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = [
        sys.executable, "-m", "swarph_cli",
        "board", "cards", "dev-extract",
        "--symbol", "chat_sync",
        "--as", "cursor-lin",
        "--token-file", str(token),
        "--index", str(index),
        "--fed-report", str(report),
        "--structurizr-ws", str(ws),
    ]
    if print_only:
        cmd.append("--print-only")
    if extra:
        cmd.extend(extra)
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _freshness(text: str) -> str:
    marker = "freshness (STALE when fed build < last repo.merged):"
    assert marker in text, text
    return text.split(marker, 1)[1]


def _git_path(repo: Path, name: str, tmp: Path) -> Path:
    rel = Path(_git(repo, "rev-parse", "--git-path", name, tmp=tmp))
    return rel if rel.is_absolute() else (repo / rel)


def _age_fetch_head(work: Path, tmp: Path, hours: float) -> None:
    fetch_head = _git_path(work, "FETCH_HEAD", tmp)
    assert fetch_head.is_file()
    old = time.time() - hours * 3600
    os.utime(fetch_head, (old, old))


def _layout(tmp: Path, work: Path, sha: str, slug: str = SLUG):
    db = tmp / "index.db"
    report = tmp / "federation-report.txt"
    ws = tmp / "workspace.json"
    _index(db, work, slug)
    _report(report, sha)
    _ws(ws)
    return db, report, ws


def test_fetch_fails_after_remote_merge_is_not_fresh(tmp_path):
    """Remote main moved; fetch cannot see it. Local origin/main is older than the build."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    remote = _push_newer(tmp_path, bare, "seed-BrainSurfing-tech")
    assert remote != local
    assert _git(work, "rev-parse", "refs/remotes/origin/main", tmp=tmp_path) == local
    db, report, ws = _layout(tmp_path, work, local)
    # Point origin at a missing bare with the same owner/repo tail so fetch
    # fails on every OS (chmod on the real bare is not reliable on Windows).
    missing = tmp_path / "gone" / "BrainSurfing-tech" / "swarph-cli.git"
    assert not missing.exists()
    _git(work, "remote", "set-url", "origin", str(missing), tmp=tmp_path)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fetch failed" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_fork_origin_is_not_fresh(tmp_path):
    """origin URL is a fork. The local main commit is old, so a time compare would read fresh."""
    bare = _bare_at(tmp_path, "someuser")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "not the canonical remote" in tail
    assert SLUG in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_branch_tracking_fork_main_is_not_fresh(tmp_path):
    """origin is the canonical remote, and the branch tracks fork/main."""
    canon = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, canon)
    fork = _bare_at(tmp_path, "someuser")
    _git(work, "remote", "add", "fork", str(fork), tmp=tmp_path)
    _git(work, "push", "fork", "main", tmp=tmp_path)
    _git(work, "fetch", "fork", tmp=tmp_path)
    _git(work, "branch", "--set-upstream-to=fork/main", tmp=tmp_path)
    upstream = _git(work, "rev-parse", "--abbrev-ref", "@{upstream}", tmp=tmp_path)
    assert upstream == "fork/main"
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fork/main" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_last_fetch_older_than_bound_is_not_fresh(tmp_path):
    """FETCH_HEAD is older than the stated bound. The remote is still reachable."""
    from swarph_cli.commands.dev_extract import FETCH_TRUST_BOUND_HOURS

    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    fetch_head = Path(_git(work, "rev-parse", "--git-path", "FETCH_HEAD", tmp=tmp_path))
    if not fetch_head.is_absolute():
        fetch_head = work / fetch_head
    assert fetch_head.is_file()
    old = time.time() - (FETCH_TRUST_BOUND_HOURS + 2) * 3600
    os.utime(fetch_head, (old, old))
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert f"older than {FETCH_TRUST_BOUND_HOURS}h" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_canonical_remote_freshly_fetched_stays_fresh(tmp_path):
    """Canonical origin, branch not on fork/main, fetch inside the bound: still fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    assert _git(work, "rev-parse", "--abbrev-ref", "@{upstream}", tmp=tmp_path) == "origin/main"
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not fresh" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail
    assert FED_STAMP in tail


def test_missing_slug_says_canonical_remote_is_undefined(tmp_path):
    """swarph-cli has no canonical-remote map. An empty index slug must say so."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local, slug="")
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "canonical remote is not defined" in tail
    assert "swarph-cli has no canonical-remote map" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


WITHHELD_REPO = "zz-withheld-ledger"


def test_post_safe_json_omits_withheld_repo_name(tmp_path):
    """Post-safe --json must not carry repo_commits or a withheld repo's name."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        (WITHHELD_REPO, "hidden/zz-withheld-ledger", str(work), "private",
         "2026-11-01T00:00:00Z", "full"))
    con.commit()
    con.close()
    report.write_text(
        report.read_text(encoding="utf-8")
        + f"# repo {WITHHELD_REPO} {local} {FED_STAMP}\n",
        encoding="utf-8",
    )
    proc = _verb(
        tmp_path, db, report, ws, print_only=False,
        extra=["--card", "1436", "--json", "--gateway", "http://127.0.0.1:9"],
    )
    assert proc.returncode == 0, proc.stderr
    assert WITHHELD_REPO not in proc.stdout
    assert WITHHELD_REPO not in proc.stderr
    payload = json.loads(proc.stdout)
    assert "repo_commits" not in json.dumps(payload)
    assert payload["withheld"] == []
    assert payload["withheld_count"] == 1


def test_differing_report_and_ref_commits_are_not_fresh(tmp_path):
    """A commit dated before the report, pushed after it, is not fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    remote = _push_at(tmp_path, bare, "seed-BrainSurfing-tech",
                      "2026-11-01T12:00:00Z", "backdated push")
    assert remote != local
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "report commit differs from the ref commit" in tail
    assert "STALE" not in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{remote}" in tail


def test_branch_tracking_upstream_main_is_not_fresh(tmp_path):
    """upstream/main is not the literal fork/main, and it is not canonical."""
    canon = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, canon)
    other = _bare_at(tmp_path, "elsewhere")
    _git(work, "remote", "add", "upstream", str(other), tmp=tmp_path)
    _git(work, "push", "upstream", "main", tmp=tmp_path)
    _git(work, "fetch", "upstream", tmp=tmp_path)
    _git(work, "branch", "--set-upstream-to=upstream/main", tmp=tmp_path)
    upstream = _git(work, "rev-parse", "--abbrev-ref", "@{upstream}", tmp=tmp_path)
    assert upstream == "upstream/main"
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "upstream/main" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_branch_tracking_fork_dev_is_not_fresh(tmp_path):
    """fork/dev is another remote, not the literal fork/main string."""
    canon = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, canon)
    fork = _bare_at(tmp_path, "someuser")
    _git(work, "remote", "add", "fork", str(fork), tmp=tmp_path)
    _git(work, "checkout", "-b", "dev", tmp=tmp_path)
    _git(work, "push", "-u", "fork", "dev", tmp=tmp_path)
    upstream = _git(work, "rev-parse", "--abbrev-ref", "@{upstream}", tmp=tmp_path)
    assert upstream == "fork/dev"
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fork/dev" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_missing_fetch_head_on_fresh_clone_reads_fresh(tmp_path):
    """No FETCH_HEAD falls through to the fetch. A fresh clone then reads fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare, fetch=False)
    fetch_head = _git_path(work, "FETCH_HEAD", tmp_path)
    assert not fetch_head.is_file()
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not fresh" not in tail
    assert fetch_head.is_file()
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_worktree_without_fetch_head_reads_fresh(tmp_path):
    """A linked worktree has no FETCH_HEAD of its own. The fetch records one."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    wt = tmp_path / "wt"
    _git(work, "worktree", "add", "--detach", str(wt), "HEAD", tmp=tmp_path)
    fetch_head = _git_path(wt, "FETCH_HEAD", tmp_path)
    assert not fetch_head.is_file()
    db, report, ws = _layout(tmp_path, wt, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not fresh" not in tail
    assert fetch_head.is_file()
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_unrelated_branch_conflict_does_not_fail_fresh_main(tmp_path):
    """Fetching every branch fails here. ``origin main`` still reads fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _git(work, "branch", "side", tmp=tmp_path)
    _git(work, "push", "origin", "side", tmp=tmp_path)
    _git(work, "config", "--unset-all", "remote.origin.fetch", tmp=tmp_path)
    _git(work, "config", "remote.origin.fetch",
         "+refs/heads/main:refs/remotes/origin/main", tmp=tmp_path)
    _git(work, "config", "--add", "remote.origin.fetch",
         "refs/heads/side:refs/remotes/origin/side", tmp=tmp_path)
    _git(work, "fetch", "origin", tmp=tmp_path)
    other = tmp_path / "unrelated"
    other.mkdir()
    subprocess.run(
        ["git", *_GIT, "init", "-b", "side", str(other)],
        check=True, capture_output=True, text=True, env=_env(tmp_path),
    )
    (other / "README").write_text("unrelated\n", encoding="utf-8")
    _commit(other, tmp_path, OLD, "unrelated root")
    _git(other, "remote", "add", "origin", str(bare), tmp=tmp_path)
    _git(other, "push", "--force", "origin", "HEAD:side", tmp=tmp_path)
    failed = subprocess.run(
        ["git", "-C", str(work), *_GIT, "fetch", "--quiet", "--no-tags", "origin"],
        capture_output=True, text=True, env=_env(tmp_path),
    )
    assert failed.returncode != 0
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not fresh" not in tail
    assert "fetch failed" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_remote_merge_after_build_rereads_ref_and_is_stale(tmp_path):
    """After a successful fetch the compared ref is the new commit, so STALE.

    Skipping the post-fetch re-read leaves the pre-fetch commit and reads fresh.
    """
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    remote = _push_newer(tmp_path, bare, "seed-BrainSurfing-tech")
    assert remote != local
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "STALE (federation build" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{remote}" in tail
    assert f"compared refs/remotes/origin/main@{local}" not in tail


def test_slug_compare_is_case_insensitive(tmp_path):
    """Live shape: origin owner PROVEN, index slug proven. That reads fresh."""
    bare = _bare_at(tmp_path, "PROVEN")
    work, local = _seed_and_clone(tmp_path, bare)
    origin = _git(work, "remote", "get-url", "origin", tmp=tmp_path)
    assert "PROVEN/swarph-cli" in origin.replace("\\", "/")
    db, report, ws = _layout(tmp_path, work, local, slug="proven/swarph-cli")
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not the canonical remote" not in tail
    assert "not fresh" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_printed_origin_strips_userinfo(tmp_path):
    """A token in the origin URL is not printed on the not-fresh line."""
    bare = _bare_at(tmp_path, "someuser")
    work, local = _seed_and_clone(tmp_path, bare)
    secret = "ghp_SECRETTOKEN1436"
    _git(work, "remote", "set-url", "origin",
         f"https://x-access-token:{secret}@github.com/someuser/swarph-cli.git",
         tmp=tmp_path)
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    blob = proc.stdout + proc.stderr
    assert secret not in blob
    assert "x-access-token" not in blob
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "https://github.com/someuser/swarph-cli.git" in tail
    assert "not the canonical remote" in tail


def _stall_http_server():
    """Accept HTTP connections and never answer them."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(0.5)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        held = []
        try:
            while not stop.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.settimeout(0.5)
                held.append(conn)
                try:
                    conn.recv(4096)
                except OSError:
                    pass
        finally:
            for conn in held:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, stop, thread


def test_fetch_gives_up_near_fifteen_seconds(tmp_path):
    """A remote that accepts and never answers fails inside the 15s fetch bound.

    A 1500s timeout would still be waiting when this returns.
    """
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    port, stop, thread = _stall_http_server()
    try:
        _git(work, "remote", "set-url", "origin",
             f"http://127.0.0.1:{port}/BrainSurfing-tech/swarph-cli.git",
             tmp=tmp_path)
        _git(work, "config", "http.lowSpeedLimit", "0", tmp=tmp_path)
        _git(work, "config", "http.lowSpeedTime", "0", tmp=tmp_path)
        db, report, ws = _layout(tmp_path, work, local)
        started = time.monotonic()
        proc = _verb(tmp_path, db, report, ws, timeout=40)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(timeout=2)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 35, elapsed
    assert elapsed >= 10, elapsed
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fetch failed" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail


def test_fetch_head_at_23_9h_reads_fresh(tmp_path):
    """23.9h is inside the 24h bound. A 1h bound would read not fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _age_fetch_head(work, tmp_path, 23.9)
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "fresh (federation build" in tail
    assert "not fresh" not in tail
    assert "older than" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


def test_fetch_head_at_24_1h_is_not_fresh(tmp_path):
    """24.1h is outside the 24h bound. A 240h bound would read fresh."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _age_fetch_head(work, tmp_path, 24.1)
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "older than 24h" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail
