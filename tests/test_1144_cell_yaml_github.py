"""#1144 — a cell's GitHub login lives in its own cell yaml (`github: <login>`).

The commander, 2026-10-14: "you are all suppose to have that identity in your cell
yaml". gh-identities.json held 2 cells for two months; every other cell was refused at
its first push (#1096 was blocked that way), days after it was created. These tests pin
the four properties the move must keep:

  1. the yaml is read FIRST, and the cell is found by its `name:`, never its filename;
  2. yaml and json DISAGREEING is a refusal, not "yaml wins";
  3. doctor asks the same question as the hook (one resolve() for both);
  4. `swarph init` writes the field only when told, and never defaults it.
"""
from __future__ import annotations

import io
import contextlib
import json

import pytest
import yaml

from swarph_cli import gh_identity as ghid
from swarph_cli.cell import cells_dir, load_cell
from swarph_cli.commands import gh_route
from swarph_cli.commands.init import run_init


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An isolated cells dir (via XDG_CONFIG_HOME, so cell.cells_dir() — the path the
    router really uses — points here) and an ABSENT json mapping. Returns a writer."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv(ghid.MAPPING_ENV, str(tmp_path / "gh-identities.json"))
    d = cells_dir()
    d.mkdir(parents=True)

    def write(fname, **fields):
        (d / fname).write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")
        return d / fname
    return write


def _json(tmp_path, table):
    (tmp_path / "gh-identities.json").write_text(json.dumps(table), encoding="utf-8")


# ── 1. the yaml is read first, by mesh name ─────────────────────────────────

def test_a_yaml_login_resolves_with_NO_json_at_all(box):
    """The point of the card: the json can be absent and a cell still routes."""
    f = box("science-claude.yaml", name="science-claude", github="reviewers-pixel")
    res = ghid.resolve("science-claude")
    assert res.login == "reviewers-pixel"
    assert res.source == str(f), "the audit line must name the yaml it came from"


def test_the_cell_is_found_by_its_NAME_not_its_filename(box):
    """lab.yaml carries name: lab-ovh. A filename lookup refuses lab-ovh on day one
    (the doctor bug fixed in #262, one layer down)."""
    box("lab.yaml", name="lab-ovh", role="lab", github="lab-ovh")
    assert ghid.resolve("lab-ovh").login == "lab-ovh"
    with pytest.raises(ghid.RouterRefusal):
        ghid.resolve("lab")          # the FILENAME must not satisfy the router


def test_the_json_still_resolves_a_cell_whose_yaml_has_no_login(box, tmp_path):
    """Fallback release: a cell not yet migrated keeps working."""
    box("cursor-lin.yaml", name="cursor-lin")
    _json(tmp_path, {"cursor-lin": "orchestrators-hue"})
    res = ghid.resolve("cursor-lin")
    assert res.login == "orchestrators-hue"
    assert res.source.endswith("gh-identities.json")


def test_an_unmapped_cell_is_REFUSED_and_both_places_are_named(box, tmp_path):
    box("meta-muse.yaml", name="meta-muse")
    _json(tmp_path, {"lab-ovh": "lab-ovh"})
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("meta-muse")
    msg = str(e.value)
    assert "github:" in msg and "gh-identities.json" in msg
    assert "REFUSING RATHER THAN USING THE ACTIVE ACCOUNT" in msg


@pytest.mark.parametrize("bad", ["", "   ", ["reviewers-pixel"], {"login": "x"}, 7])
def test_a_present_but_malformed_yaml_login_is_REFUSED(box, bad):
    box("drop.yaml", name="drop-on-meta-edge", github=bad)
    with pytest.raises(ghid.RouterRefusal):
        ghid.resolve("drop-on-meta-edge")


def test_an_UNREADABLE_yaml_is_named_in_the_refusal(box, tmp_path):
    """It may be this cell's own config; 'not mapped' would hide that."""
    (cells_dir() / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("broken")
    assert "broken.yaml" in str(e.value)


# ── 2. two sources that disagree ────────────────────────────────────────────

def test_yaml_and_json_DISAGREEING_is_a_refusal_naming_both(box, tmp_path):
    """>>> THE CASE THE CARD DID NOT DEFINE. <<< Letting yaml win silently is the
    substitution class one layer up; the operator decides which one is right."""
    box("opencode.yaml", name="opencode", github="orchestrators-hue")
    _json(tmp_path, {"opencode": "reviewers-pixel"})
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("opencode")
    msg = str(e.value)
    assert "orchestrators-hue" in msg and "reviewers-pixel" in msg


def test_yaml_and_json_AGREEING_resolves_from_the_yaml(box, tmp_path):
    f = box("opencode.yaml", name="opencode", github="orchestrators-hue")
    _json(tmp_path, {"opencode": "orchestrators-hue"})
    res = ghid.resolve("opencode")
    assert res.login == "orchestrators-hue" and res.source == str(f)


def test_two_yamls_claiming_one_name_with_DIFFERENT_logins_are_REFUSED(box):
    box("a.yaml", name="gpt-ops", github="reviewers-pixel")
    box("b.yaml", name="gpt-ops", github="orchestrators-hue")
    with pytest.raises(ghid.RouterRefusal) as e:
        ghid.resolve("gpt-ops")
    assert "a.yaml" in str(e.value) and "b.yaml" in str(e.value)


def test_two_yamls_claiming_one_name_with_the_SAME_login_resolve(box):
    """No ambiguity in the answer, so no refusal: the result is the same either way."""
    box("a.yaml", name="gpt-ops", github="reviewers-pixel")
    box("b.yaml", name="gpt-ops", github="reviewers-pixel")
    assert ghid.resolve("gpt-ops").login == "reviewers-pixel"


def test_an_UNREADABLE_json_still_refuses_a_yaml_mapped_cell(box, tmp_path):
    """A corrupt fallback file means the disagreement check cannot run. Absent is a
    normal state from #1144 on; unreadable is a fault."""
    box("lab.yaml", name="lab-ovh", github="lab-ovh")
    (tmp_path / "gh-identities.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ghid.RouterRefusal):
        ghid.resolve("lab-ovh")


# ── 3. doctor and the hook ask the same question ────────────────────────────

def _doctor(capsys):
    rc = gh_route.run_doctor()
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_doctor_is_GREEN_on_a_yaml_only_box(box, capsys):
    box("lab.yaml", name="lab-ovh", github="lab-ovh")
    box("science-claude.yaml", name="science-claude", github="reviewers-pixel")
    rc, out, _ = _doctor(capsys)
    assert rc == 0, out
    assert "lab-ovh -> lab-ovh" in out and "[yaml:" in out


def test_doctor_REFUSES_a_cell_whose_yaml_and_json_disagree(box, tmp_path, capsys):
    """The hook refuses this cell, so the preflight must too."""
    box("opencode.yaml", name="opencode", github="orchestrators-hue")
    _json(tmp_path, {"opencode": "reviewers-pixel"})
    rc, out, _ = _doctor(capsys)
    assert rc == 1
    assert "REFUSED  opencode" in out


def test_doctor_with_no_json_and_no_yaml_login_is_UNCONFIGURED(box, capsys):
    box("meta-muse.yaml", name="meta-muse")
    rc, _, err = _doctor(capsys)
    assert rc == 1
    assert "DO NOT INSTALL THE HOOK" in err


def test_the_hook_injects_a_yaml_login(box, monkeypatch):
    box("science-claude.yaml", name="science-claude", github="reviewers-pixel")
    monkeypatch.setenv("SWARPH_SELF", "science-claude")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "gh pr view 5"}}))
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["hookSpecificOutput"]["updatedInput"]["command"].startswith(
        "export GH_TOKEN=$(gh auth token --user reviewers-pixel); ")
    assert "science-claude -> reviewers-pixel" in out["systemMessage"]


# ── 4. swarph init ──────────────────────────────────────────────────────────

def test_init_writes_github_when_given(box, tmp_path, capsys):
    rc = run_init(["opencode", "--provider", "opencode", "--cwd", str(tmp_path),
                   "--github", "orchestrators-hue", "-y"])
    assert rc == 0
    c = load_cell(cells_dir() / "opencode.yaml")
    assert c.extra["github"] == "orchestrators-hue"
    assert "github=orchestrators-hue" in capsys.readouterr().out
    assert ghid.resolve("opencode").login == "orchestrators-hue"


def test_init_WITHOUT_github_writes_no_login_and_says_it_will_be_refused(
        box, tmp_path, capsys):
    """>>> NO DEFAULT. <<< Not the active gh account, not anything: blank stays blank,
    and the gap is announced at creation instead of at the first push."""
    rc = run_init(["meta-muse", "--provider", "muse", "--cwd", str(tmp_path), "-y"])
    assert rc == 0
    raw = yaml.safe_load((cells_dir() / "meta-muse.yaml").read_text(encoding="utf-8"))
    assert "github" not in raw
    assert "gh-route will REFUSE" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["-lead", "trail-", "two--hyphens", "a" * 40, "sp ace"])
def test_init_rejects_an_invalid_github_login(box, tmp_path, bad):
    rc = run_init(["x-cell", "--provider", "opencode", "--cwd", str(tmp_path),
                   f"--github={bad}", "-y"])    # = form: argparse reads "-lead" as a flag
    assert rc == 2
    assert not (cells_dir() / "x-cell.yaml").exists()


# ── 5. the login is spliced into a shell command ────────────────────────────

# The whitespace-free ones matter: with only spaces and $( ) here, a LOGIN_RE loosened
# to admit ';' passed every test (drop-on-meta-edge's surviving mutant, #1818).
_HOSTILE = ["x); touch PWNED; #", "x$(touch PWNED)", "x`touch PWNED`", "a b",
            "x;touch PWNED", "a;b", "a|b", "a&b", "a>b", "lab-ovh\n"]


@pytest.mark.parametrize("bad", _HOSTILE[:-1])   # yaml strips, so "lab-ovh\n" is lab-ovh
def test_a_shell_shaped_YAML_login_is_REFUSED(box, bad):
    """A cell yaml can sit behind `<cwd>/cell.yaml` (--symlink-cwd); the hook rewrites
    an approved `gh` command with this value, so it must be a GitHub login or nothing."""
    box("c.yaml", name="c", github=bad)
    with pytest.raises(ghid.RouterRefusal):
        ghid.resolve("c")


@pytest.mark.parametrize("bad", _HOSTILE)
def test_a_shell_shaped_JSON_login_is_REFUSED(box, tmp_path, bad):
    box("c.yaml", name="c")
    _json(tmp_path, {"c": bad})
    with pytest.raises(ghid.RouterRefusal):
        ghid.resolve("c")


def test_inject_QUOTES_the_login_so_a_bad_one_cannot_run(tmp_path):
    """Defence in depth behind the refusal: executed, not string-matched."""
    import subprocess
    cmd = ghid.inject("true", "x); touch PWNED; #")
    subprocess.run(["bash", "-c", cmd], cwd=tmp_path, capture_output=True,
                   env={"PATH": "/usr/bin:/bin"})
    assert not (tmp_path / "PWNED").exists()


# ── 6. a stray file must not crash the hook (#1818) ─────────────────────────
#
# drop-on-meta-edge, reviewing b962a72: a crashed hook is not a refusal. Claude Code
# blocks a PreToolUse call only on exit 2 or a deny, so a resolver exception let `gh`
# run as the active account for every cell sharing the box.

def _hook(monkeypatch, cell):
    monkeypatch.setenv("SWARPH_SELF", cell)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "gh pr view 1"}}))
    return rc, json.loads(buf.getvalue())["hookSpecificOutput"]


_STRAY = {
    "utf16.yaml": "name: other\ngithub: reviewers-pixel\n".encode("utf-16"),  # PS 5.1 redirect
    "badbytes.yaml": b"name: other\ngithub: \xff\xfe\x80\n",
    "deep.yaml": b"[" * 100000,
}


@pytest.mark.parametrize("fname", sorted(_STRAY))
def test_a_stray_unreadable_yaml_beside_a_YAML_mapped_cell_still_rewrites(
        box, monkeypatch, fname):
    box("lab.yaml", name="lab-ovh", github="lab-ovh")
    (cells_dir() / fname).write_bytes(_STRAY[fname])
    rc, out = _hook(monkeypatch, "lab-ovh")
    assert rc == 0
    assert out["updatedInput"]["command"].startswith(
        "export GH_TOKEN=$(gh auth token --user lab-ovh); ")


@pytest.mark.parametrize("fname", sorted(_STRAY))
def test_a_stray_unreadable_yaml_beside_a_JSON_mapped_cell_still_rewrites(
        box, tmp_path, monkeypatch, fname):
    """main 17b4b52 rewrote here; b962a72 exited 1 with empty stdout."""
    _json(tmp_path, {"cursor-lin": "orchestrators-hue"})
    (cells_dir() / fname).write_bytes(_STRAY[fname])
    rc, out = _hook(monkeypatch, "cursor-lin")
    assert rc == 0
    assert "--user orchestrators-hue" in out["updatedInput"]["command"]


@pytest.mark.parametrize("fname", sorted(_STRAY))
def test_an_unmapped_cell_with_an_unreadable_yaml_is_DENIED_naming_it(
        box, monkeypatch, fname):
    (cells_dir() / fname).write_bytes(_STRAY[fname])
    rc, out = _hook(monkeypatch, "meta-muse")
    assert rc == 0 and out["permissionDecision"] == "deny"
    assert fname in out["permissionDecisionReason"]


def test_a_resolver_CRASH_is_a_deny_not_an_exit(box, monkeypatch):
    """Belt and braces for a future resolver bug."""
    def boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(ghid, "resolve", boom)
    rc, out = _hook(monkeypatch, "lab-ovh")
    assert rc == 0 and out["permissionDecision"] == "deny"
    assert "crashed" in out["permissionDecisionReason"]


def test_show_REFUSES_on_a_resolver_crash(box, monkeypatch, capsys):
    monkeypatch.setattr(ghid, "resolve", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    assert gh_route.run_show() == 1
    assert "REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize("fname", sorted(_STRAY))
def test_doctor_names_an_unreadable_yaml_instead_of_crashing(box, capsys, fname):
    box("lab.yaml", name="lab-ovh", github="lab-ovh")
    (cells_dir() / fname).write_bytes(_STRAY[fname])
    rc, out, err = _doctor(capsys)
    assert fname in err and "NOT counted as covered" in err
    assert "ok       lab-ovh -> lab-ovh" in out
