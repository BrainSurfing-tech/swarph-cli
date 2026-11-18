"""Card #1684 -- the Python and OS claims in pyproject.toml match the CI legs.

`G10` of the v1.0 plan: pyproject classified 3.10-3.13 and Linux, while CI ran
3.10-3.12 on Linux and 3.11-3.12 on macOS and Windows. Nothing compared the two.

The check reads pyproject.toml and the three workflow files and fails both ways:
a classified Python version or OS with no CI leg, and a CI leg with no classifier.
It also requires every classified Python version on the posix leg (the full range
runs there; macOS and Windows run a decided subset).

The check is a pure function, so the mutants below prove each break turns it red.
"""
import re
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("posix", "macos", "windows")
RUNNER_CLASSIFIER = {
    "ubuntu": "Operating System :: POSIX :: Linux",
    "macos": "Operating System :: MacOS",
    "windows": "Operating System :: Microsoft :: Windows",
}
PY_CLASSIFIER = re.compile(r"^Programming Language :: Python :: (3\.\d+)$")


def _load():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workflows = {
        name: yaml.safe_load((ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8"))
        for name in WORKFLOWS
    }
    return pyproject, workflows


def _legs(doc):
    """(runner classifiers, python versions, problems) for every matrix job in one workflow."""
    oses, versions, problems = set(), set(), []
    for job_name, job in doc["jobs"].items():
        matrix = job.get("strategy", {}).get("matrix", {})
        runners = matrix["os"] if "matrix.os" in str(job["runs-on"]) else [job["runs-on"]]
        for runner in runners:
            family = next((k for k in RUNNER_CLASSIFIER if str(runner).startswith(k)), None)
            if family is None:
                problems.append(f"job {job_name}: runner {runner!r} maps to no OS classifier")
            else:
                oses.add(RUNNER_CLASSIFIER[family])
        for version in matrix.get("python-version", []):
            # An unquoted 3.10 is the float 3.1 in YAML: refuse it rather than guess.
            if not isinstance(version, str):
                problems.append(f"job {job_name}: python-version {version!r} is not a quoted string")
            else:
                versions.add(version)
    return oses, versions, problems


def mismatches(pyproject, workflows):
    """Everything where the classifiers and the CI legs disagree. Empty means they match."""
    classifiers = pyproject["project"]["classifiers"]
    claimed_versions = {m.group(1) for c in classifiers if (m := PY_CLASSIFIER.match(c))}
    claimed_oses = {c for c in classifiers if c in RUNNER_CLASSIFIER.values()}
    ci_oses, ci_versions, out = set(), set(), []
    per_workflow = {}
    for name, doc in workflows.items():
        oses, versions, problems = _legs(doc)
        ci_oses |= oses
        ci_versions |= versions
        per_workflow[name] = versions
        out += [f"{name}.yml: {p}" for p in problems]
    out += [f"python {v} is classified but no CI leg runs it" for v in sorted(claimed_versions - ci_versions)]
    out += [f"python {v} has a CI leg but no classifier" for v in sorted(ci_versions - claimed_versions)]
    out += [f"{c!r} is classified but no CI leg runs it" for c in sorted(claimed_oses - ci_oses)]
    out += [f"{c!r} has a CI leg but no classifier" for c in sorted(ci_oses - claimed_oses)]
    out += [
        f"python {v} is classified but the posix leg does not run it"
        for v in sorted(claimed_versions - per_workflow.get("posix", set()))
        if v in ci_versions
    ]
    return out


def test_classifiers_match_the_ci_legs():
    pyproject, workflows = _load()
    assert mismatches(pyproject, workflows) == []


def _matrix(workflows, name):
    return next(iter(workflows[name]["jobs"].values()))["strategy"]["matrix"]


def _drop_posix_314(py, wf):
    _matrix(wf, "posix")["python-version"].remove("3.14")


def _add_windows_315(py, wf):
    _matrix(wf, "windows")["python-version"].append("3.15")


def _drop_macos_classifier(py, wf):
    py["project"]["classifiers"].remove("Operating System :: MacOS")


def _drop_windows_leg(py, wf):
    wf["windows"]["jobs"] = {}


def _drop_314_classifier(py, wf):
    py["project"]["classifiers"].remove("Programming Language :: Python :: 3.14")


def _classify_39(py, wf):
    py["project"]["classifiers"].append("Programming Language :: Python :: 3.9")


def _macos_only_315(py, wf):
    _matrix(wf, "macos")["python-version"].append("3.15")
    py["project"]["classifiers"].append("Programming Language :: Python :: 3.15")


def _unknown_runner(py, wf):
    next(iter(wf["macos"]["jobs"].values()))["runs-on"] = "freebsd-latest"


def _unquoted_version(py, wf):
    _matrix(wf, "posix")["python-version"].append(3.1)


MUTANTS = [
    (_drop_posix_314, "python 3.14 is classified but the posix leg does not run it"),
    (_add_windows_315, "python 3.15 has a CI leg but no classifier"),
    (_drop_macos_classifier, "'Operating System :: MacOS' has a CI leg but no classifier"),
    (_drop_windows_leg, "'Operating System :: Microsoft :: Windows' is classified but no CI leg"),
    (_drop_314_classifier, "python 3.14 has a CI leg but no classifier"),
    (_classify_39, "python 3.9 is classified but no CI leg"),
    (_macos_only_315, "python 3.15 is classified but the posix leg does not run it"),
    (_unknown_runner, "maps to no OS classifier"),
    (_unquoted_version, "is not a quoted string"),
]


@pytest.mark.parametrize("mutate,expected", MUTANTS, ids=[m.__name__ for m, _ in MUTANTS])
def test_each_break_turns_the_check_red(mutate, expected):
    pyproject, workflows = _load()
    mutate(pyproject, workflows)
    assert any(expected in m for m in mismatches(pyproject, workflows)), mismatches(pyproject, workflows)
