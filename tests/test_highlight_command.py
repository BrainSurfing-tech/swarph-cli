"""Tests for ``swarph highlight`` — append to a git-backed timeline.

Offline: each test uses a fresh temp timeline dir with no remote (``--no-push``),
so the real git mechanics run (init + append + commit) without a network.
"""

from __future__ import annotations

import http.client
import subprocess

import pytest

from swarph_cli.commands import highlight as hl


@pytest.fixture(autouse=True)
def _no_ambient_gateway(monkeypatch):
    """Isolate git-path tests: clear any gateway env so a developer/CI with
    SWARPH_BRAIN_GATEWAY set doesn't silently flip these into the gateway path.
    Gateway tests set the env explicitly. Also clear SWARPH_SELF (#657) so a
    box-ambient identity cannot outrank a test's SWARPH_CELL."""
    for v in ("SWARPH_HIGHLIGHT_GATEWAY", "SWARPH_GATEWAY", "SWARPH_BRAIN_GATEWAY",
              "SWARPH_SELF", "GATEWAY_TIMELINE_DIR"):
        monkeypatch.delenv(v, raising=False)


# --- pure helpers ----------------------------------------------------------

def test_collapse_newlines():
    assert hl._collapse("a\nb\rc") == "a b c"


def test_format_line_with_memory():
    line = hl._format_line("2026-06-27T00:00Z", "lab-ovh", "shipped X", "[[mem-x]]")
    assert line == "- 2026-06-27T00:00Z · **lab-ovh** · shipped X · → [[mem-x]]"


def test_format_line_without_memory():
    line = hl._format_line("2026-06-27T00:00Z", "lab-ovh", "shipped X", "")
    assert line == "- 2026-06-27T00:00Z · **lab-ovh** · shipped X"


# --- the verb (real git, no remote) ----------------------------------------

def test_highlight_inits_appends_commits(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "test-cell")
    rc = hl.run_highlight(["my highlight", "[[mem-x]]", "--no-push"])
    assert rc == 0
    tl = (d / "TIMELINE.md").read_text(encoding="utf-8")
    assert "**test-cell**" in tl and "my highlight" in tl and "→ [[mem-x]]" in tl
    log = subprocess.run(["git", "-C", str(d), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "highlight" in log.lower()
    # union-merge attribute set so concurrent cell appends auto-merge
    assert "merge=union" in (d / ".gitattributes").read_text(encoding="utf-8")


def test_highlight_collapses_multiline_anti_spoof(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "c")
    rc = hl.run_highlight(["line one\nFORGED **other** entry", "--no-push"])
    assert rc == 0
    entry_lines = [l for l in (d / "TIMELINE.md").read_text(encoding="utf-8").splitlines()
                   if l.startswith("- ")]
    assert len(entry_lines) == 1  # the newline can't forge a second attributed entry
    assert "line one FORGED" in entry_lines[0]


def test_highlight_second_append_keeps_both(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "c")
    hl.run_highlight(["first", "--no-push"])
    hl.run_highlight(["second", "--no-push"])
    entry_lines = [l for l in (d / "TIMELINE.md").read_text(encoding="utf-8").splitlines()
                   if l.startswith("- ")]
    assert len(entry_lines) == 2


# --- gateway (peer-token) path ---------------------------------------------

def _fake_post(capture, status=200, resp=None):
    """Return a _post_json stand-in that records the call + returns a fixed reply."""
    def _p(url, body, token, **kw):
        capture.update(url=url, body=body, token=token)
        return status, (resp if resp is not None else
                        {"logged": True, "cell": body.get("cell"), "ts": "2026-01-01T00:00Z",
                         "line": "x", "pushed": True})
    return _p


def test_resolve_gateway_precedence(monkeypatch):
    monkeypatch.delenv("SWARPH_HIGHLIGHT_GATEWAY", raising=False)
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://brain:8788")
    assert hl._resolve_gateway(None) == "http://brain:8788"        # brain fallback
    monkeypatch.setenv("SWARPH_GATEWAY", "http://gw:8788")
    assert hl._resolve_gateway(None) == "http://gw:8788"           # SWARPH_GATEWAY wins over brain
    monkeypatch.setenv("SWARPH_HIGHLIGHT_GATEWAY", "http://hl:8788")
    assert hl._resolve_gateway(None) == "http://hl:8788"           # most-specific wins
    assert hl._resolve_gateway("http://cli:8788") == "http://cli:8788"  # --gateway wins over all


def test_gateway_mode_is_default_when_configured(monkeypatch):
    cap = {}
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")   # the mesh-wide var
    monkeypatch.setenv("SWARPH_CELL", "gridiron")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")               # _resolve_token source
    monkeypatch.setattr(hl, "_post_json", _fake_post(cap))
    rc = hl.run_highlight(["a real highlight", "[[project_x]]"])
    assert rc == 0
    assert cap["url"] == "http://gw:8788/highlights"
    assert cap["body"]["highlight"] == "a real highlight"
    assert cap["body"]["cell"] == "gridiron"
    assert cap["body"]["memory"] == "[[project_x]]"
    assert "when" in cap["body"] and cap["body"]["when"]
    assert cap["token"] == "tok"


@pytest.mark.parametrize("status", [502, 401, 422])
def test_63_http_refusal_is_not_a_local_success(status, monkeypatch, tmp_path, capsys):
    """PR #392 / obligation #63: HTTP ≠ 200 → rc 1, no line, no 'logged'.

    401/422/502 are refusals. Treating them as LOCAL FALLBACK was the
    regression: a rejected highlight became a git success under whatever
    _resolve_cell produced.
    """
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post({}, status=status, resp={"detail": "no"}))
    rc = hl.run_highlight(["x", "--no-push"])
    assert rc == 1
    assert not (tmp_path / "tl" / "TIMELINE.md").exists()
    assert "logged" not in capsys.readouterr().out


def test_63_timeout_is_ambiguous_no_local_write(monkeypatch, tmp_path, capsys):
    """TimeoutError is caught in _log_via_gateway, not _post_json.

    A 10s client timeout against a gateway that commits then pushes is
    AMBIGUOUS — fallback here would duplicate a line that likely landed.
    """
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))

    def _boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(hl, "_post_json", _boom)
    rc = hl.run_highlight(["x", "--when", "2026-09-07T21:00Z", "--no-push"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "AMBIGUOUS" in err
    assert "2026-09-07T21:00Z" in err
    assert not (tmp_path / "tl" / "TIMELINE.md").exists()


def test_63_status_0_fallback_when_matches_post(monkeypatch, tmp_path, capsys):
    """Closed port (status 0): local write, LOCAL FALLBACK, ts == posted when.

    SWARPH_TIMELINE_DIR is the reader too — must NOT claim timeline will
    not show this. No origin on a fresh init — must say so.
    """
    cap = {}
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post(cap, status=0, resp={"detail": "down"}))
    rc = hl.run_highlight(["marooned", "--when", "2026-09-07T21:00Z", "--no-push"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LOCAL FALLBACK" in out
    assert "via gateway" not in out
    assert "will NOT show this" not in out
    assert "no origin remote" in out
    assert cap["body"]["when"] == "2026-09-07T21:00Z"
    entries = [l for l in (tmp_path / "tl" / "TIMELINE.md").read_text(encoding="utf-8").splitlines()
               if l.startswith("- ")]
    assert len(entries) == 1
    assert entries[0].startswith("- 2026-09-07T21:00Z")
    assert "marooned" in entries[0]


def test_63_status_0_refuses_the_gateway_clone(monkeypatch, tmp_path, capsys):
    """Status 0 against GATEWAY_TIMELINE_DIR must not write into that tree."""
    d = tmp_path / "gw-clone"
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("GATEWAY_TIMELINE_DIR", str(d))
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post({}, status=0, resp={"detail": "down"}))
    rc = hl.run_highlight(["x", "--no-push"])
    assert rc == 1
    assert "not writing into the gateway's clone" in capsys.readouterr().err
    assert not (d / "TIMELINE.md").exists()


def test_64_status_0_refuses_git_config_clone_without_env(monkeypatch, tmp_path, capsys):
    """#64: env unset is the live pane. The .git-resident flag must bind alone.

    A test that only sets GATEWAY_TIMELINE_DIR passes on the #64 bug.
    """
    d = tmp_path / "gw-clone"
    d.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(d)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "--local",
                    "swarph.gatewayClone", "1"], check=True)
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.delenv("GATEWAY_TIMELINE_DIR", raising=False)
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post({}, status=0, resp={"detail": "down"}))
    rc = hl.run_highlight(["x", "--no-push"])
    assert rc == 1
    assert "not writing into the gateway's clone" in capsys.readouterr().err
    assert not (d / "TIMELINE.md").exists()


def test_64_post_send_exception_is_ambiguous(monkeypatch, tmp_path, capsys):
    """Should-fix 1: ConnectionResetError (post-send) is AMBIGUOUS, not status 0."""
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))

    def _boom(*_a, **_k):
        raise ConnectionResetError("reset after read")

    monkeypatch.setattr(hl, "_post_json", _boom)
    rc = hl.run_highlight(["x", "--when", "2026-09-08T09:00Z", "--no-push"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "AMBIGUOUS" in err
    assert "2026-09-08T09:00Z" in err
    assert not (tmp_path / "tl" / "TIMELINE.md").exists()


def test_64_failed_commit_truncates_append(monkeypatch, tmp_path):
    """Should-fix 2: commit fail leaves the tree clean — NOT logged is true."""
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    assert hl.run_highlight(["first ok", "--no-push"]) == 0
    real = hl._git

    def _fail_commit(repo, *args, **kw):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(["git", *args], 1, "", "nope")
        return real(repo, *args, **kw)

    monkeypatch.setattr(hl, "_git", _fail_commit)
    rc = hl.run_highlight(["must not stick", "--no-push"])
    assert rc == 1
    porcelain = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                               capture_output=True, text=True)
    assert "TIMELINE.md" not in porcelain.stdout
    body = (d / "TIMELINE.md").read_text(encoding="utf-8")
    assert "first ok" in body
    assert "must not stick" not in body


def _flagged_clone(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "--local",
                    "swarph.gatewayClone", "1"], check=True)
    return path


def test_65_nonexistent_subpath_of_flagged_clone_refuses(monkeypatch, tmp_path, capsys):
    """#65 defect 1: <clone>/newsub must not mkdir-and-commit into the clone."""
    clone = _flagged_clone(tmp_path / "swarph-timeline")
    before = list(clone.rglob("*"))
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("GATEWAY_TIMELINE_DIR", raising=False)
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post({}, status=0, resp={"detail": "down"}))
    rc = hl.run_highlight(["x", "--timeline-dir", str(clone / "newsub"), "--no-push"])
    assert rc == 1
    assert "not writing into the gateway's clone" in capsys.readouterr().err
    assert not (clone / "newsub").exists()
    assert list(clone.rglob("*")) == before


def test_65_off_is_not_a_gateway_clone_flag(tmp_path):
    d = _flagged_clone(tmp_path / "tl")
    subprocess.run(["git", "-C", str(d), "config", "--local",
                    "swarph.gatewayClone", "off"], check=True)
    assert hl._git_gateway_clone_flag(d) is False
    assert hl._is_gateway_clone(d) is False


def test_65_revert_does_not_delete_concurrent_commit(tmp_path):
    """#65 defect 2: stale prior-size truncate must not drop another writer's line."""
    d = tmp_path / "tl"
    d.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(d)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    tl = d / "TIMELINE.md"
    tl.write_text("# head\n\n- A\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "TIMELINE.md"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-m", "A"], check=True)
    # other writer committed C; we then appended OUR
    tl.write_text("# head\n\n- A\n- C\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "TIMELINE.md"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-m", "C"], check=True)
    our = "- 2026-09-08T09:00Z · **c** · ours\n"
    tl.write_text("# head\n\n- A\n- C\n" + our, encoding="utf-8")
    hl._revert_append(d, our)
    body = tl.read_text(encoding="utf-8")
    assert "- C\n" in body
    assert "ours" not in body
    porcelain = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                               capture_output=True, text=True)
    assert "TIMELINE.md" not in porcelain.stdout


def test_65_http_200_non_object_is_not_a_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setattr(hl, "_post_json", lambda *a, **k: (200, []))
    rc = hl.run_highlight(["x", "--no-push"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-object" in err
    assert "Traceback" not in err
    assert not (tmp_path / "tl" / "TIMELINE.md").exists()


def test_65_invalid_url_is_not_ambiguous(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))

    def _boom(*_a, **_k):
        raise http.client.InvalidURL("bad port")

    monkeypatch.setattr(hl, "_post_json", _boom)
    rc = hl.run_highlight(["x", "--no-push"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "bad gateway URL" in err
    assert "AMBIGUOUS" not in err


def test_65_dotdot_through_missing_dir_still_sees_the_clone(monkeypatch, tmp_path, capsys):
    """#65: ``x/../clone`` with x missing must not skip the guard via exists()."""
    clone = _flagged_clone(tmp_path / "swarph-timeline")
    sneaky = tmp_path / "x" / ".." / "swarph-timeline"
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("GATEWAY_TIMELINE_DIR", raising=False)
    monkeypatch.setattr(hl, "_post_json",
                        _fake_post({}, status=0, resp={"detail": "down"}))
    rc = hl.run_highlight(["x", "--timeline-dir", str(sneaky), "--no-push"])
    assert rc == 1
    assert "not writing into the gateway's clone" in capsys.readouterr().err
    assert not (clone / "TIMELINE.md").exists()


def test_local_flag_forces_git_even_with_gateway(tmp_path, monkeypatch):
    called = {"n": 0}
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")   # configured...
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setattr(hl, "_post_json",
                        lambda *a, **k: (called.update(n=called["n"] + 1), (200, {}))[1])
    rc = hl.run_highlight(["local please", "--local", "--no-push"])   # ...but --local wins
    assert rc == 0
    assert called["n"] == 0                                        # gateway NOT called
    body = (tmp_path / "tl" / "TIMELINE.md").read_text()
    assert "local please" in body and "**c**" in body             # git path ran


# --- #657 cell identity house order -----------------------------------------

def test_resolve_cell_swarph_self_outranks_cell_and_hostname(tmp_path, monkeypatch):
    """#657 ACCEPT (a): SELF wins; case-fold is NOT the fix (FAIL condition)."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("SWARPH_CELL", "wrong-cell")
    monkeypatch.setattr(hl.socket, "gethostname", lambda: "Lab-ovh")
    cell, source = hl._resolve_cell(None, tmp_path)
    assert cell == "lab-ovh"
    assert source == "$SWARPH_SELF"


def test_resolve_cell_flag_outranks_self(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    cell, source = hl._resolve_cell("explicit", tmp_path)
    assert (cell, source) == ("explicit", "--cell")


def test_resolve_cell_falls_to_hostname_only_when_nothing_else(tmp_path, monkeypatch):
    """Can-fail (b) shape: without SELF, hostname is reached — and may case-mismatch."""
    monkeypatch.delenv("SWARPH_CELL", raising=False)
    monkeypatch.setattr(hl.socket, "gethostname", lambda: "Lab-ovh")
    cell, source = hl._resolve_cell(None, tmp_path)
    assert cell == "Lab-ovh"
    assert source == "hostname"


def test_credential_error_near_match_forbids_register(tmp_path, monkeypatch):
    """#657 ACCEPT (c): case-only mismatch must NOT recommend mesh register.

    `_peer_token_near_match` uses ``Path.home()``, which is USERPROFILE on
    Windows and HOME on Posix. Patch ``Path.home`` directly so both lanes see
    the fixture (pixel REVISE on #359 — HOME-only left Windows red).
    """
    cfg = tmp_path / ".config" / "swarph"
    cfg.mkdir(parents=True)
    (cfg / "lab-ovh.peer_token").write_text("tok\n")
    monkeypatch.setattr(hl.Path, "home", classmethod(lambda cls: tmp_path))
    msg = hl._credential_error("Lab-ovh", "hostname",
                               RuntimeError("cannot resolve a mesh credential"))
    assert "did you mean 'lab-ovh'" in msg
    assert "mesh register" in msg and "Do NOT" in msg
    # The harmful bare advice must not stand alone as the remedy.
    assert "is what mints one" not in msg


def test_gateway_uses_swarph_self_when_cell_unset(monkeypatch):
    cap = {}
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.delenv("SWARPH_CELL", raising=False)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(hl.socket, "gethostname", lambda: "Lab-ovh")
    monkeypatch.setattr(hl, "_post_json", _fake_post(cap))
    rc = hl.run_highlight(["from self"])
    assert rc == 0
    assert cap["body"]["cell"] == "lab-ovh"
