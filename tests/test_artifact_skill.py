"""``swarph add`` (T3-skill) — the ``skill`` handler + skills-dir file drop.

Exercises the pure skill-file install machinery and the full ``run_add``
install path with an injected ``skills_home`` (no CLI shell-out). Covers:

* ``resolve_builtin_skill`` known/unknown (unknown names "swarph-intro")
* ``_install_skill_files`` writes ``<home>/<name>/SKILL.md``; idempotent
* ``_remove_skill`` removes the dir; no-op on absent
* builtin install via the full ``run_add`` path (returns 0, writes a valid
  SKILL.md, emits the restart/reopen activation note)
* published publisher FAILS CLOSED — non-zero, mutates NOTHING
* an unknown builtin name (ValueError caught) writes nothing
* the builtin's SKILL.md frontmatter has non-empty name + description
"""

from __future__ import annotations

import pytest

from swarph_cli.commands.add import (
    BUILTIN_SKILLS,
    SkillBundle,
    _install_skill_files,
    _remove_skill,
    resolve_builtin_skill,
    run_add,
)


# --------------------------------------------------------------------------- #
# resolve_builtin_skill
# --------------------------------------------------------------------------- #


def test_resolve_builtin_swarph_intro():
    bundle = resolve_builtin_skill("swarph-intro")
    assert isinstance(bundle, SkillBundle)
    assert bundle.trust == "builtin"
    assert bundle.publisher == "swarph-builtin"
    relpaths = [relpath for relpath, _ in bundle.files]
    assert "SKILL.md" in relpaths


def test_resolve_builtin_unknown_raises_naming_swarph_intro():
    with pytest.raises(ValueError) as exc:
        resolve_builtin_skill("does-not-exist")
    assert "swarph-intro" in str(exc.value)


def test_builtin_skills_catalog_has_swarph_intro():
    assert "swarph-intro" in BUILTIN_SKILLS


# --------------------------------------------------------------------------- #
# _install_skill_files
# --------------------------------------------------------------------------- #


def _skill_md(bundle: SkillBundle) -> str:
    return dict(bundle.files)["SKILL.md"]


def test_install_skill_files_writes_skill_md(tmp_path):
    bundle = resolve_builtin_skill("swarph-intro")
    _install_skill_files(tmp_path, "swarph-intro", bundle.files)
    skill_md = tmp_path / "swarph-intro" / "SKILL.md"
    assert skill_md.exists()
    assert skill_md.read_text(encoding="utf-8") == _skill_md(bundle)


def test_install_skill_files_is_idempotent(tmp_path):
    bundle = resolve_builtin_skill("swarph-intro")
    _install_skill_files(tmp_path, "swarph-intro", bundle.files)
    _install_skill_files(tmp_path, "swarph-intro", bundle.files)
    skill_md = tmp_path / "swarph-intro" / "SKILL.md"
    assert skill_md.exists()
    assert skill_md.read_text(encoding="utf-8") == _skill_md(bundle)


# --------------------------------------------------------------------------- #
# _remove_skill
# --------------------------------------------------------------------------- #


def test_remove_skill_removes_dir(tmp_path):
    bundle = resolve_builtin_skill("swarph-intro")
    _install_skill_files(tmp_path, "swarph-intro", bundle.files)
    assert (tmp_path / "swarph-intro").exists()
    _remove_skill(tmp_path, "swarph-intro")
    assert not (tmp_path / "swarph-intro").exists()


def test_remove_skill_noop_on_absent(tmp_path):
    # no raise even though nothing is there
    _remove_skill(tmp_path, "swarph-intro")
    assert not (tmp_path / "swarph-intro").exists()


# --------------------------------------------------------------------------- #
# builtin install via the full run_add path
# --------------------------------------------------------------------------- #


def test_builtin_skill_installs(tmp_path, capsys):
    rc = run_add(
        ["swarph://skill/swarph-builtin/swarph-intro", "--yes"],
        skills_home=tmp_path,
    )
    assert rc == 0
    skill_md = tmp_path / "swarph-intro" / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name:" in text
    assert "description:" in text
    out = capsys.readouterr().out
    assert "restart" in out or "reopen" in out


def test_builtin_skill_install_is_idempotent(tmp_path):
    run_add(
        ["swarph://skill/swarph-builtin/swarph-intro", "--yes"],
        skills_home=tmp_path,
    )
    rc = run_add(
        ["swarph://skill/swarph-builtin/swarph-intro", "--yes"],
        skills_home=tmp_path,
    )
    assert rc == 0
    assert (tmp_path / "swarph-intro" / "SKILL.md").exists()


# --------------------------------------------------------------------------- #
# published publisher FAILS CLOSED
# --------------------------------------------------------------------------- #


def test_published_skill_fails_closed(tmp_path, capsys):
    rc = run_add(
        ["swarph://skill/lab-ovh/foo", "--yes"],
        skills_home=tmp_path,
    )
    assert rc != 0
    assert not (tmp_path / "foo").exists()
    combined = capsys.readouterr()
    assert "not yet trusted" in (combined.out + combined.err)


# --------------------------------------------------------------------------- #
# unknown builtin name → ValueError caught, nothing written
# --------------------------------------------------------------------------- #


def test_unknown_builtin_skill_writes_nothing(tmp_path, capsys):
    rc = run_add(
        ["swarph://skill/swarph-builtin/does-not-exist", "--yes"],
        skills_home=tmp_path,
    )
    assert rc != 0
    assert not (tmp_path / "does-not-exist").exists()


# --------------------------------------------------------------------------- #
# SKILL.md frontmatter validity smoke
# --------------------------------------------------------------------------- #


def test_builtin_skill_md_frontmatter_valid():
    bundle = resolve_builtin_skill("swarph-intro")
    text = _skill_md(bundle)
    assert text.startswith("---")
    # Frontmatter region is between the first two '---' fence lines.
    lines = text.splitlines()
    assert lines[0] == "---"
    end = next(i for i in range(1, len(lines)) if lines[i] == "---")
    frontmatter = lines[1:end]
    fm = {}
    for line in frontmatter:
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    assert fm.get("name")
    assert fm.get("description")
