"""Tests for ``swarph highlight`` — append to a git-backed timeline.

Offline: each test uses a fresh temp timeline dir with no remote (``--no-push``),
so the real git mechanics run (init + append + commit) without a network.
"""

from __future__ import annotations

import subprocess

import pytest

from swarph_cli.commands import highlight as hl


@pytest.fixture(autouse=True)
def _no_ambient_gateway(monkeypatch):
    """Isolate git-path tests: clear any gateway env so a developer/CI with
    SWARPH_BRAIN_GATEWAY set doesn't silently flip these into the gateway path.
    Gateway tests set the env explicitly."""
    for v in ("SWARPH_HIGHLIGHT_GATEWAY", "SWARPH_GATEWAY", "SWARPH_BRAIN_GATEWAY"):
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
    assert cap["body"] == {"highlight": "a real highlight", "cell": "gridiron",
                           "memory": "[[project_x]]"}
    assert cap["token"] == "tok"


def test_gateway_failure_returns_1_no_git_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    monkeypatch.setenv("SWARPH_CELL", "c")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(tmp_path / "tl"))
    monkeypatch.setattr(hl, "_post_json", _fake_post({}, status=502, resp={"detail": "boom"}))
    rc = hl.run_highlight(["x"])
    assert rc == 1                                                 # fail loud
    assert not (tmp_path / "tl" / "TIMELINE.md").exists()          # no silent git double-write


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
