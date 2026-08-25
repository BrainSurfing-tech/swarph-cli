"""Skills shipped as PACKAGE DATA must actually ship, and must not auto-install.

WHY (2026-08-25). `i-have-adhd` is a verbosity control the commander runs on chosen
cells. Before this, it existed as a git clone on whichever boxes happened to have one —
lab-ovh's was 143 commits behind upstream and carried a local-only commit, and nobody
could say which version any other box had. A content-addressable artifact makes that
answerable instead of a guess.

Two properties, and the second is the commander's requirement, not a nicety:

1. THE FILES MUST BE IN THE WHEEL. This repo has a recorded failure where a new module
   silently did not ship. Package data is worse than a module: `find_packages` picks up
   modules automatically, but a `.md` file needs an explicit `package-data` glob, and the
   symptom is a clean install that raises at first use.

2. NOTHING MAY INSTALL IT BY ITSELF. >>> "I don't want anyone opting in, I want to run it
   for my choice of AI." <<< The commander decides which cells get it. A skill that
   installs on spawn, on init, or on any path he did not name would take that choice away
   silently — and an over-eager install is invisible, because the result looks like a cell
   that simply has the skill.
"""
import pathlib
import re

import pytest

from swarph_cli.commands import add

_SRC = pathlib.Path(add.__file__).resolve().parent.parent
_REPO = _SRC.parent.parent


def test_the_packaged_skill_resolves_with_all_its_files() -> None:
    bundle = add.resolve_builtin_skill("i-have-adhd")

    assert bundle.publisher == add._BUILTIN_PUBLISHER
    assert bundle.trust == "builtin"
    names = [relpath for relpath, _ in bundle.files]
    assert "SKILL.md" in names
    assert "LICENSE" in names, (
        "the LICENSE must travel with the skill — it is MIT work by a third party "
        "(Ayoub Ghriss) vendored into a package we publish under our own name")


def test_the_license_and_attribution_survive_the_vendoring() -> None:
    """Vendoring third-party MIT into a package we publish is legal WITH attribution.
    That attribution is a shipped file, not a commit message nobody reads."""
    bundle = add.resolve_builtin_skill("i-have-adhd")
    license_text = dict(bundle.files)["LICENSE"]

    assert "MIT License" in license_text
    assert "Ayoub Ghriss" in license_text, "the copyright holder must not be stripped"


def test_the_skill_carries_rule_0() -> None:
    """Rule 0 is swarph's own addition to the upstream skill and the reason we vendor it
    at all. It lived as an UNPUSHED local commit on one box; if the vendored copy loses
    it, the packaging has shipped the wrong thing while looking correct."""
    body = dict(add.resolve_builtin_skill("i-have-adhd").files)["SKILL.md"]

    assert "### 0." in body
    assert "do not announce" in body.lower()
    assert "a stated next action is not a done action" in body.lower()


def test_the_model_cannot_invoke_it_by_itself() -> None:
    """>>> THE COMMANDER CHOOSES WHICH AI RUNS THIS. <<< `disable-model-invocation: true`
    in the frontmatter is what stops a model deciding for itself that it should be
    terse — the difference between a control he applies and a behaviour that spreads."""
    body = dict(add.resolve_builtin_skill("i-have-adhd").files)["SKILL.md"]
    frontmatter = body.split("---", 2)[1]

    assert re.search(r"^disable-model-invocation:\s*true\s*$", frontmatter, re.M), (
        "without this the model may self-invoke the skill, which takes the choice of "
        "WHICH cells run it away from the commander")


def test_resolution_reads_the_file_rather_than_a_frozen_import_time_constant() -> None:
    """>>> NOTHING THAT CAN FAIL BELONGS AT IMPORT. <<< #578's whole lesson: a module
    constant is evaluated when the package is imported, so an unreadable data file would
    break `swarph --help` rather than `swarph add`. This asserts the file is the source of
    truth by changing it and re-resolving."""
    src = _SRC / "skills" / "i-have-adhd" / "SKILL.md"
    original = src.read_text(encoding="utf-8")
    try:
        src.write_text(original + "\nSENTINEL-NOT-IN-THE-CONSTANT\n", encoding="utf-8")
        body = dict(add.resolve_builtin_skill("i-have-adhd").files)["SKILL.md"]
        assert "SENTINEL-NOT-IN-THE-CONSTANT" in body, (
            "resolution is reading a frozen copy, not the shipped file")
    finally:
        src.write_text(original, encoding="utf-8")


def test_a_missing_data_file_names_the_packaging_bug() -> None:
    """A wheel that did not ship its package data must say so, not fail obscurely.
    Absent-vs-broken again: an empty result and a missing file must not read alike."""
    src = _SRC / "skills" / "i-have-adhd" / "LICENSE"
    original = src.read_text(encoding="utf-8")
    src.unlink()
    try:
        with pytest.raises(FileNotFoundError) as excinfo:
            add.resolve_builtin_skill("i-have-adhd")
        assert "packaging bug" in str(excinfo.value)
    finally:
        src.write_text(original, encoding="utf-8")


def test_pyproject_ships_the_skill_files() -> None:
    """The guard for property 1, at the only place that decides it.

    `find_packages` collects modules automatically; package DATA needs an explicit glob,
    and forgetting it produces a clean install that raises at first use. This asserts the
    glob exists rather than trusting that a build happened to work."""
    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")

    assert "skills/*/SKILL.md" in pyproject, (
        "pyproject does not ship skills/*/SKILL.md as package data — the wheel will "
        "install a package whose skill files are absent")
    assert "skills/*/LICENSE" in pyproject, (
        "the vendored LICENSE would not ship, which is the attribution requirement")


def test_unknown_skill_lists_BOTH_sources() -> None:
    """Two registries, one namespace. A caller who names a skill wrong must be told
    everything that exists, not just the half that happens to be string-backed."""
    with pytest.raises(ValueError) as excinfo:
        add.resolve_builtin_skill("no-such-skill")

    msg = str(excinfo.value)
    assert "i-have-adhd" in msg, "packaged skills missing from the available list"
    assert "swarph-intro" in msg, "string-constant skills missing from the available list"


def test_NOTHING_INSTALLS_THE_SKILL_WITHOUT_BEING_ASKED() -> None:
    """>>> THE COMMANDER'S REQUIREMENT, AS A TEST. <<<

    "I don't want anyone opting in, I want to run it for my choice of AI."

    So no code path outside `swarph add` may install a skill. This greps the shipped
    source for calls to the installer from spawn/init/onboard/monitor — the paths that run
    without him — because an over-eager install is invisible: the result looks exactly
    like a cell he chose.

    If a future push mechanism lands (a `skills:` list in cell.yaml, which HE owns), this
    test should be updated deliberately to allow that ONE path and no other.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name in {"add.py", "__init__.py"}:
            continue
        if path.parent.name == "commands" and path.name in {
            "spawn.py", "init.py", "onboard.py", "monitor.py", "daemon.py", "ratify.py",
        }:
            text = path.read_text(encoding="utf-8")
            for marker in ("_install_skill_files", "resolve_builtin_skill",
                           "_resolve_packaged_skill"):
                if marker in text:
                    offenders.append(f"{path.name} calls {marker}")

    assert not offenders, (
        "a path the commander does not run installs a skill by itself:\n  "
        + "\n  ".join(offenders))
