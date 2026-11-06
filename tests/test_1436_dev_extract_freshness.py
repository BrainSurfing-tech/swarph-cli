"""Card #1436: dev-extract must not read fresh from an untrusted local ref.

Each case runs the real ``swarph board cards dev-extract`` verb against a
temporary git repo. Git is not mocked. On main, cases 1–4 read fresh because
``merged_at`` is the local ``origin/main`` time, and the healthy case does
not name the commit the report described or the ref it compared against.
"""

from __future__ import annotations

import json
import os
import pytest
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
          env_extra: dict | None = None,
          timeout: int = 60) -> subprocess.CompletedProcess:
    token = tmp / "token"
    _token(token)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["GIT_TERMINAL_PROMPT"] = "0"
    if env_extra:
        env.update(env_extra)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="git http stall kill timing is verified on posix/macos; Windows CI "
           "does not reliably bound the hung fetch inside the test harness",
)
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


def _assert_url_secret_absent(proc, secret: str, shown: str) -> None:
    assert proc.returncode == 0, proc.stderr
    blob = proc.stdout + proc.stderr
    assert secret not in blob
    assert shown in blob


def test_user_only_url_strips_token_on_line_and_json(tmp_path):
    """User-only, ssh, scp, and query tokens are absent on the line and in --json.

    Stripping only ``user:password`` would still print ``https://TOKEN@host``.
    """
    bare = _bare_at(tmp_path, "someuser")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local)
    cases = (
        ("https", "https://{secret}@github.com/someuser/swarph-cli.git",
         "https://github.com/someuser/swarph-cli.git"),
        ("ssh", "ssh://{secret}@github.com/someuser/swarph-cli.git",
         "ssh://github.com/someuser/swarph-cli.git"),
        ("scp", "{secret}@github.com:someuser/swarph-cli.git",
         "github.com:someuser/swarph-cli.git"),
        ("query", "https://github.com/someuser/swarph-cli.git?token={secret}",
         "https://github.com/someuser/swarph-cli.git"),
    )
    for name, template, shown in cases:
        secret = f"ghp_USERONLY1436{name}"
        _git(work, "remote", "set-url", "origin",
             template.format(secret=secret), tmp=tmp_path)
        text = _verb(tmp_path, db, report, ws)
        _assert_url_secret_absent(text, secret, shown)
        tail = _freshness(text.stdout)
        assert "not fresh" in tail
        assert "not the canonical remote" in tail
        payload = _verb(tmp_path, db, report, ws, extra=["--json"])
        _assert_url_secret_absent(payload, secret, shown)
        data = json.loads(payload.stdout)
        reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
        assert secret not in reason
        assert shown in reason


def test_report_without_repo_stamp_is_not_fresh(tmp_path):
    """A report with no ``# repo`` line for this repo is not fresh.

    Treating a missing stamp as the same commit would read fresh.
    """
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local)
    report.write_text(
        "# swarph federation — cross-repo connection map\n"
        f"# generated {FED_STAMP} by fed_refresh.sh (card #44 part 4)\n"
        "\n"
        "  Layer 2:\n",
        encoding="utf-8",
    )
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "report commit differs from the ref commit" in tail
    assert "fresh (federation build" not in tail
    assert "report described no commit" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="gitlink commit setup for submodule fetch failure is posix/macos "
           "verified; Windows git rejects the test repo layout",
)
def test_failed_fetch_reread_names_new_commit(tmp_path):
    """A fetch that fails after moving origin/main names that new commit.

    Re-reading only when the fetch succeeds would still name the pre-fetch commit.
    """
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    _work, old = _seed_and_clone(tmp_path, bare)
    seed = tmp_path / "seed-BrainSurfing-tech"
    fake = "b" * 40
    _git(seed, "update-index", "--add", "--cacheinfo", f"160000,{fake},sub",
         tmp=tmp_path)
    missing = tmp_path / "missing-sub.git"
    (seed / ".gitmodules").write_text(
        '[submodule "sub"]\n'
        "\tpath = sub\n"
        f"\turl = {missing}\n",
        encoding="utf-8",
    )
    _git(seed, "add", ".gitmodules", tmp=tmp_path)
    dated = {"GIT_AUTHOR_DATE": "2026-11-03T12:00:00Z",
             "GIT_COMMITTER_DATE": "2026-11-03T12:00:00Z"}
    _git(seed, "commit", "-m", "gitlink", env_extra=dated, tmp=tmp_path)
    new = _git(seed, "rev-parse", "HEAD", tmp=tmp_path)
    assert new != old
    assert "160000" in _git(seed, "ls-tree", "HEAD", tmp=tmp_path)
    _git(seed, "push", "origin", "main", tmp=tmp_path)
    work = tmp_path / "work-reread"
    subprocess.run(
        ["git", *_GIT, "clone", str(bare), str(work)],
        check=True, capture_output=True, text=True, env=_env(tmp_path),
    )
    sub = work / "sub"
    if sub.exists():
        subprocess.run(["rm", "-rf", str(sub)], check=True)
    subprocess.run(
        ["git", *_GIT, "init", "-b", "main", str(sub)],
        check=True, capture_output=True, text=True, env=_env(tmp_path),
    )
    (sub / "x").write_text("x\n", encoding="utf-8")
    _commit(sub, tmp_path, OLD, "sub")
    _git(work, "submodule", "init", tmp=tmp_path)
    _git(sub, "remote", "add", "origin", str(missing), tmp=tmp_path)
    _git(work, "config", "fetch.recurseSubmodules", "true", tmp=tmp_path)
    _git(work, "update-ref", "refs/remotes/origin/main", old, tmp=tmp_path)
    assert _git(work, "rev-parse", "refs/remotes/origin/main", tmp=tmp_path) == old
    db, report, ws = _layout(tmp_path, work, old)
    proc = _verb(tmp_path, db, report, ws)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fetch failed" in tail
    assert "fresh (federation build" not in tail
    assert f"report described {old}" in tail
    assert f"compared refs/remotes/origin/main@{new}" in tail
    assert f"compared refs/remotes/origin/main@{old}" not in tail


def _add_public_repo(db: Path, name: str, slug: str, repo_path: Path,
                     symbol_id: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        (name, slug, str(repo_path), "public", "2026-11-01T00:00:00Z", "full"))
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)",
        (symbol_id, name, "chat_sync", "function", "src/chat.py", 1,
         "chat_sync", ""))
    con.commit()
    con.close()


def test_malformed_origin_url_is_not_fresh_and_healthy_repo_prints(tmp_path):
    """A malformed origin URL does not crash. That repo is not fresh.

    ``ValueError`` from ``_strip_userinfo`` used to abort the verb, so a
    healthy repo in the same extract printed nothing.
    """
    ok_bare = _bare_at(tmp_path, "BrainSurfing-tech")
    ok_work, ok_sha = _seed_and_clone(tmp_path, ok_bare)
    bad_bare = _bare_at(tmp_path, "swarph-mesh-owner")
    bad_work, bad_sha = _seed_and_clone(tmp_path, bad_bare)
    db = tmp_path / "index.db"
    report = tmp_path / "federation-report.txt"
    ws = tmp_path / "workspace.json"
    _index(db, ok_work, SLUG)
    _add_public_repo(db, "swarph-mesh", "BrainSurfing-tech/swarph-mesh",
                     bad_work, 2)
    report.write_text(
        "# swarph federation — cross-repo connection map\n"
        f"# generated {FED_STAMP} by fed_refresh.sh (card #44 part 4)\n"
        f"# repo swarph-cli {ok_sha} {FED_STAMP}\n"
        f"# repo swarph-mesh {bad_sha} {FED_STAMP}\n"
        "\n"
        "  Layer 2:\n",
        encoding="utf-8",
    )
    _ws(ws)
    secret = "ghp_MALFORMED1436"
    urls = (
        f"https://u:{secret}@host:notaport/someuser/swarph-mesh.git",
        f"https://u:{secret}@[bad/someuser/swarph-mesh.git",
    )
    for url in urls:
        _git(bad_work, "remote", "set-url", "origin", url, tmp=tmp_path)
        proc = _verb(tmp_path, db, report, ws)
        assert proc.returncode == 0, proc.stderr
        blob = proc.stdout + proc.stderr
        assert secret not in blob
        assert "Traceback" not in blob
        tail = _freshness(proc.stdout)
        assert "swarph-mesh: not fresh" in tail
        assert "swarph-cli: fresh (federation build" in tail


def test_refspec_that_skips_main_is_not_fresh(tmp_path):
    """No refspec, and a narrow refspec, leave origin/main behind FETCH_HEAD.

    Skipping the FETCH_HEAD compare would read fresh: the fetch exits 0
    and the report still matches the old ref.
    """
    for mode in ("none", "narrow"):
        sub = tmp_path / mode
        sub.mkdir()
        bare = _bare_at(sub, "BrainSurfing-tech")
        work, local = _seed_and_clone(sub, bare)
        remote = _push_newer(sub, bare, "seed-BrainSurfing-tech")
        assert remote != local
        if mode == "none":
            _git(work, "config", "--unset-all", "remote.origin.fetch", tmp=sub)
        else:
            _git(work, "remote", "set-branches", "origin", "dev", tmp=sub)
        db, report, ws = _layout(sub, work, local)
        proc = _verb(sub, db, report, ws)
        assert proc.returncode == 0, proc.stderr
        tail = _freshness(proc.stdout)
        assert "not fresh" in tail, mode
        assert "fetched commit differs from the ref commit" in tail, mode
        assert "fetch failed" not in tail, mode
        assert "fresh (federation build" not in tail, mode
        assert f"report described {local}" in tail
        assert f"compared refs/remotes/origin/main@{local}" in tail
        assert _git(work, "rev-parse", "refs/remotes/origin/main", tmp=sub) == local
        fetch_head = _git_path(work, "FETCH_HEAD", sub)
        assert remote in fetch_head.read_text(encoding="utf-8")


def _ssh_deny_stub(path: Path, stderr: str) -> dict:
    """``GIT_SSH_COMMAND`` that prints ``stderr`` and exits the way ssh does.

    A Python stub, so the same denial is what git records on Windows and posix.
    """
    script = path.with_suffix(".py")
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({(stderr + chr(10))!r})\n"
        "raise SystemExit(255)\n",
        encoding="utf-8",
    )
    return {"GIT_SSH_COMMAND": f'"{sys.executable}" "{script}"'}


_QUERY_FRAGMENT_TEMPLATES = (
    "https://github.com/someuser/swarph-cli.git?private_token={secret}",
    "https://github.com/someuser/swarph-cli.git?oauth_token={secret}",
    "https://github.com/someuser/swarph-cli.git?api_key={secret}",
    "https://github.com/someuser/swarph-cli.git?key={secret}",
    "https://github.com/someuser/swarph-cli.git?{secret}",
    "https://github.com/someuser/swarph-cli.git#token={secret}",
)


@pytest.mark.parametrize(
    ("label", "template"),
    list(zip(
        ("private_token", "oauth_token", "api_key", "key",
         "bare_query", "fragment"),
        _QUERY_FRAGMENT_TEMPLATES,
    )),
    ids=("private_token", "oauth_token", "api_key", "key",
         "bare_query", "fragment"),
)
def test_printed_origin_drops_whole_query_and_fragment(tmp_path, label, template):
    """A printed origin keeps no query and no fragment, on the line and in --json.

    Dropping only ``access_token``, ``token``, ``password``, ``secret``, and
    ``auth`` still prints ``private_token``, ``oauth_token``, ``api_key``,
    ``key``, a bare query, and a fragment.
    """
    bare = _bare_at(tmp_path, "someuser")
    work, _local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, _local)
    secret = f"ghp_QUERY1436_{label}"
    shown = "https://github.com/someuser/swarph-cli.git"
    _git(work, "remote", "set-url", "origin",
         template.format(secret=secret), tmp=tmp_path)
    text = _verb(tmp_path, db, report, ws)
    _assert_url_secret_absent(text, secret, shown)
    tail = _freshness(text.stdout)
    assert "not fresh" in tail
    assert "?" not in tail
    assert "#" not in tail
    payload = _verb(tmp_path, db, report, ws, extra=["--json"])
    _assert_url_secret_absent(payload, secret, shown)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert secret not in reason
    assert shown in reason
    assert "?" not in reason
    assert "#" not in reason


def test_scp_fetch_error_redacts_user_on_line_and_json(tmp_path):
    """An scp user's ssh denial is redacted on the line and in --json.

    Redaction that only knows ``scheme://`` URLs prints
    ``user@host: Permission denied`` from the fetch error.
    """
    user = "scpuser1436"
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _git(work, "remote", "set-url", "origin",
         f"{user}@host:BrainSurfing-tech/swarph-cli.git", tmp=tmp_path)
    env = _ssh_deny_stub(
        tmp_path / "ssh-stub",
        f"{user}@host: Permission denied (publickey).",
    )
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws, env_extra=env)
    assert proc.returncode == 0, proc.stderr
    blob = proc.stdout + proc.stderr
    assert user not in blob
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fetch failed" in tail
    assert "Permission denied" in tail
    assert "fresh (federation build" not in tail
    payload = _verb(tmp_path, db, report, ws, extra=["--json"], env_extra=env)
    assert user not in (payload.stdout + payload.stderr)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert user not in reason
    assert "fetch failed" in reason
    assert "Permission denied" in reason
    assert f"{user}@" not in reason


def test_fetch_error_redacts_userinfo_before_cut(tmp_path):
    """Userinfo is redacted before the 180-character cut.

    The token sits entirely inside that window and its closing ``@`` sits
    just past it. Cutting first leaves the token; the redaction regex
    never sees the ``@``.
    """
    secret = "T" * 172
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _git(work, "remote", "set-url", "origin",
         "git@github.com:BrainSurfing-tech/swarph-cli.git", tmp=tmp_path)
    env = _ssh_deny_stub(
        tmp_path / "ssh-cut",
        "https://" + secret + "@github.com/BrainSurfing-tech/swarph-cli.git: "
        "Permission denied",
    )
    db, report, ws = _layout(tmp_path, work, local)
    proc = _verb(tmp_path, db, report, ws, env_extra=env)
    assert proc.returncode == 0, proc.stderr
    blob = proc.stdout + proc.stderr
    assert secret not in blob
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "fetch failed" in tail
    assert "Permission denied" in tail
    payload = _verb(tmp_path, db, report, ws, extra=["--json"], env_extra=env)
    assert secret not in (payload.stdout + payload.stderr)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert secret not in reason
    assert "https://github.com/" in reason


def _git_rev_parse_fetch_head_fails(bin_dir: Path) -> dict:
    """Real git, except ``rev-parse FETCH_HEAD`` exits 128.

    A successful fetch always writes a readable FETCH_HEAD, so the
    fail-closed branch cannot be reached without this one refusal.
    ``rev-parse --git-path FETCH_HEAD`` still runs real git.
    """
    real = subprocess.check_output(["which", "git"], text=True).strip()
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"real = {real!r}\n"
        "args = sys.argv[1:]\n"
        "cmd = None\n"
        "prev = None\n"
        "saw_fetch_head = False\n"
        "saw_git_path = False\n"
        "for arg in args:\n"
        "    if prev in ('-C', '-c'):\n"
        "        prev = None\n"
        "        continue\n"
        "    if arg in ('-C', '-c'):\n"
        "        prev = arg\n"
        "        continue\n"
        "    if arg == '--git-path':\n"
        "        saw_git_path = True\n"
        "    if arg.startswith('-'):\n"
        "        continue\n"
        "    if cmd is None:\n"
        "        cmd = arg\n"
        "        continue\n"
        "    if arg == 'FETCH_HEAD':\n"
        "        saw_fetch_head = True\n"
        "if cmd == 'rev-parse' and saw_fetch_head and not saw_git_path:\n"
        "    sys.stderr.write('fatal: FETCH_HEAD: rev-parse failed\\n')\n"
        "    raise SystemExit(128)\n"
        "os.execv(real, [real, *args])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the FETCH_HEAD rev-parse refusal is a posix git wrapper; "
           "Windows CI still exercises the real rev-parse path",
)
def test_fetch_head_rev_parse_failure_is_not_fresh(tmp_path):
    """A fetch that exits 0 whose ``git rev-parse FETCH_HEAD`` fails is not fresh.

    Skipping that compare would read fresh: the report still matches
    origin/main.
    """
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    db, report, ws = _layout(tmp_path, work, local)
    env = _git_rev_parse_fetch_head_fails(tmp_path / "git-bin")
    proc = _verb(tmp_path, db, report, ws, env_extra=env)
    assert proc.returncode == 0, proc.stderr
    tail = _freshness(proc.stdout)
    assert "not fresh" in tail
    assert "FETCH_HEAD unreadable after fetch" in tail
    assert "fetch failed" not in tail
    assert "fresh (federation build" not in tail
    assert f"report described {local}" in tail
    assert f"compared refs/remotes/origin/main@{local}" in tail
    payload = _verb(tmp_path, db, report, ws, extra=["--json"], env_extra=env)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert reason == "FETCH_HEAD unreadable after fetch"


# Validate #4314: query and fragment on every origin shape that prints (#4320).
_SC4314 = "ghp_SC4314DUMMY"


def _sc4314_origin(tmp_path: Path, url: str):
    bare = _bare_at(tmp_path, "someuser")
    work, local = _seed_and_clone(tmp_path, bare)
    _git(work, "remote", "set-url", "origin", url, tmp=tmp_path)
    db, report, ws = _layout(tmp_path, work, local)
    line = _verb(tmp_path, db, report, ws)
    payload = _verb(tmp_path, db, report, ws, extra=["--json"])
    return line, payload


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            f"https://x:xpass@127.0.0.1:1/someuser/swarph-cli.git"
            f"?private_token={_SC4314}",
            "https://127.0.0.1:1/someuser/swarph-cli.git",
        ),
        (
            f"https://x:xpass@127.0.0.1:1/someuser/swarph-cli.git"
            f"#token={_SC4314}",
            "https://127.0.0.1:1/someuser/swarph-cli.git",
        ),
        (
            f"git@127.0.0.1:someuser/swarph-cli.git?private_token={_SC4314}",
            "127.0.0.1:someuser/swarph-cli.git",
        ),
        (
            f"git@127.0.0.1:someuser/swarph-cli.git#token={_SC4314}",
            "127.0.0.1:someuser/swarph-cli.git",
        ),
        (
            f"https://u:xpass@127.0.0.1:notaport/someuser/swarph-cli.git"
            f"?private_token={_SC4314}",
            "https://127.0.0.1:notaport/someuser/swarph-cli.git",
        ),
        (
            f"https://u:xpass@[bad/someuser/swarph-cli.git#token={_SC4314}",
            "https://[bad/someuser/swarph-cli.git",
        ),
    ],
    ids=[
        "user_query",
        "user_fragment",
        "scp_query",
        "scp_fragment",
        "malformed_query",
        "malformed_fragment",
    ],
)
def test_sc4314_no_query_on_any_printing_branch(
    tmp_path, url: str, shown: str,
):
    """Query and fragment stay off the line and out of --json on every branch."""
    line, payload = _sc4314_origin(tmp_path, url)
    _assert_url_secret_absent(line, _SC4314, shown)
    assert "not fresh" in _freshness(line.stdout)
    _assert_url_secret_absent(payload, _SC4314, shown)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert _SC4314 not in reason
    assert "?" not in reason
    assert "#" not in reason


@pytest.mark.parametrize(
    "said",
    [
        f"fatal: https://127.0.0.1/o/r?api_key={_SC4314} failed",
        f"fatal: https://127.0.0.1/o/r#token={_SC4314} failed",
    ],
    ids=["query", "fragment"],
)
def test_sc4314_error_line_loses_query_and_fragment(tmp_path, said: str):
    """A git error line must not replay query or fragment tokens."""
    bare = _bare_at(tmp_path, "BrainSurfing-tech")
    work, local = _seed_and_clone(tmp_path, bare)
    _git(
        work, "remote", "set-url", "origin",
        "git@127.0.0.1:BrainSurfing-tech/swarph-cli.git",
        tmp=tmp_path,
    )
    env = _ssh_deny_stub(tmp_path / "ssh-stub-sc4314", said)
    db, report, ws = _layout(tmp_path, work, local)
    line = _verb(tmp_path, db, report, ws, env_extra=env)
    payload = _verb(
        tmp_path, db, report, ws, extra=["--json"], env_extra=env,
    )
    assert line.returncode == 0, line.stderr
    assert payload.returncode == 0, payload.stderr
    blob = line.stdout + line.stderr + payload.stdout + payload.stderr
    assert _SC4314 not in blob
    assert "fetch failed" in _freshness(line.stdout)
    data = json.loads(payload.stdout)
    reason = data["pins"]["swarph-cli"]["ref_trust"]["reason"]
    assert _SC4314 not in reason
    assert "?" not in reason
    assert "#" not in reason
