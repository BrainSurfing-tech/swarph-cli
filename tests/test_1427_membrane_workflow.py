"""Card #1427 -- the macOS/Windows/Linux membrane (.github/workflows/membrane.yml).

Two layers, both without a network:
  * a static guard over the parsed workflow: no secret, no scope on the token,
    no untrusted input interpolated into a shell script, every action pinned,
    only dispatch/call triggers, a closed list of runner families;
  * the real validator (tools/membrane_validate.sh) run under bash with hostile
    strings, checking it rejects them and executes none.
The guard is itself tested against mutants: each break below must be caught.
"""
import copy
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MEMBRANE = ROOT / ".github" / "workflows" / "membrane.yml"
SELFTEST = ROOT / ".github" / "workflows" / "membrane-selftest.yml"
VALIDATOR = ROOT / "tools" / "membrane_validate.sh"

PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
EXPR = re.compile(r"\$\{\{.*?\}\}", re.S)


def _load(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(k)
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)


def problems(doc):
    """Everything wrong with a membrane-shaped workflow. Empty means safe."""
    out = []
    triggers = set(doc.get("on", {}))
    if triggers != {"workflow_dispatch", "workflow_call"}:
        out.append(f"triggers are {sorted(triggers)}, want exactly workflow_dispatch and workflow_call")
    if doc.get("permissions") != {}:
        out.append("top-level permissions is not {}")
    for name, job in doc.get("jobs", {}).items():
        if job.get("permissions") != {}:
            out.append(f"job {name} permissions is not {{}}")
        for i, step in enumerate(job.get("steps", [])):
            run = step.get("run")
            if isinstance(run, str) and EXPR.search(run):
                out.append(f"job {name} step {i}: an expression is interpolated into a shell script")
            use = step.get("uses")
            if use and not PINNED.match(use):
                out.append(f"job {name} step {i}: action {use} is not pinned to a 40-hex sha")
    text = "\n".join(_walk_strings(doc))
    if re.search(r"\bsecrets\b", text):
        out.append("a secrets reference exists")
    if "ACTIONS_ID_TOKEN" in text or "id-token" in text:
        out.append("an OIDC token is requested")
    # The only step allowed to see the token is the public-repo check.
    for name, job in doc.get("jobs", {}).items():
        for i, step in enumerate(job.get("steps", [])):
            envs = " ".join(str(v) for v in (step.get("env") or {}).values())
            if "github.token" in envs and step.get("id") != "pub":
                out.append(f"job {name} step {i}: github.token reaches a step other than the public-repo check")
    run_job = doc.get("jobs", {}).get("run", {})
    if "github.token" in " ".join(str(v) for s in run_job.get("steps", []) for v in (s.get("env") or {}).values()):
        out.append("the run job (which executes the command) can see github.token")
    for i, step in enumerate(run_job.get("steps", [])):
        if step.get("uses", "").startswith("actions/checkout@") and step.get("with", {}).get("persist-credentials") is not False:
            out.append(f"run job step {i}: checkout keeps credentials")
    return out


def test_membrane_workflow_passes_the_static_guard():
    assert problems(_load(MEMBRANE)) == []


def test_selftest_workflow_has_no_secret_and_no_scope():
    doc = _load(SELFTEST)
    assert doc["permissions"] == {}
    assert not re.search(r"\bsecrets\b", "\n".join(_walk_strings(doc)))
    for name, job in doc["jobs"].items():
        assert job.get("permissions") == {}, name
        for step in job.get("steps", []):
            if step.get("uses"):
                assert PINNED.match(step["uses"]), step["uses"]


def test_the_runner_family_list_is_closed_and_comes_from_the_validator():
    text = MEMBRANE.read_text(encoding="utf-8")
    doc = _load(MEMBRANE)
    assert doc["on"]["workflow_dispatch"]["inputs"]["os"]["options"] == ["macos", "windows", "ubuntu"]
    # runs-on is whatever the validator emitted, never the raw input
    assert doc["jobs"]["run"]["runs-on"] == "${{ needs.check.outputs.runner }}"
    assert "runs-on: ${{ inputs" not in text
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert re.findall(r'runner="([a-z]+-latest)"', validator) == ["macos-latest", "windows-latest", "ubuntu-latest"]


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["on"].update(pull_request_target={}), "triggers"),
    (lambda d: d.update(permissions={"contents": "write"}), "top-level permissions"),
    (lambda d: d["jobs"]["run"].update(permissions={"contents": "write"}), "permissions"),
    (lambda d: d["jobs"]["run"]["steps"][2].update(run='echo "${{ inputs.command }}"'), "interpolated"),
    (lambda d: d["jobs"]["run"]["steps"][0].update(uses="actions/checkout@v4"), "not pinned"),
    (lambda d: d["jobs"]["run"]["steps"][2]["env"].update(TOKEN="${{ secrets.MY_TOKEN }}"), "secrets"),
    (lambda d: d["jobs"]["run"]["steps"][2]["env"].update(GH_TOKEN="${{ github.token }}"), "github.token"),
    (lambda d: d["jobs"]["run"]["steps"][0]["with"].update({"persist-credentials": True}), "keeps credentials"),
])
def test_the_guard_catches_each_mutant(mutate, expect):
    doc = copy.deepcopy(_load(MEMBRANE))
    mutate(doc)
    found = problems(doc)
    assert found, "mutant passed the guard"
    assert any(expect in p for p in found), found


# ---------------------------------------------------------------- the validator, for real

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

GOOD = dict(IN_OS="macos", IN_REPO="octocat/Hello-World", IN_REF="master", IN_ARTIFACTS="README\nsub/dir/out.xml", IN_NAME="membrane-out", IN_TIMEOUT="20")


def _validate(tmp_path, **override):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_OUTPUT": str(tmp_path / "gho")}
    env.update(GOOD)
    env.update(override)
    done = subprocess.run(["bash", str(VALIDATOR)], env=env, capture_output=True, text=True, cwd=tmp_path)
    outputs = (tmp_path / "gho").read_text(encoding="utf-8") if (tmp_path / "gho").exists() else ""
    return done, outputs


@needs_bash
def test_the_good_case_is_accepted_and_names_the_runner(tmp_path):
    done, outputs = _validate(tmp_path)
    assert done.returncode == 0, done.stdout
    assert "ok=true" in outputs and "verdict=accepted" in outputs and "runner=macos-latest" in outputs


@needs_bash
@pytest.mark.parametrize("os_name,runner", [("macos", "macos-latest"), ("windows", "windows-latest"), ("ubuntu", "ubuntu-latest")])
def test_one_input_selects_each_family(tmp_path, os_name, runner):
    done, outputs = _validate(tmp_path, IN_OS=os_name)
    assert done.returncode == 0 and f"runner={runner}" in outputs


HOSTILE = [
    ("IN_REPO", "octocat/Hello-World; touch CANARY #"),
    ("IN_REPO", "$(touch CANARY)/x"),
    ("IN_REPO", "a/b`touch CANARY`"),
    ("IN_REPO", "a/b\nc/d"),
    ("IN_REPO", "justaname"),
    ("IN_REPO", "a/b c"),
    ("IN_REF", 'main"; touch CANARY #'),
    ("IN_REF", "--upload-pack=touch CANARY"),
    ("IN_REF", "-x"),
    ("IN_REF", "../../x"),
    ("IN_REF", "main extra"),
    ("IN_REF", "refs/heads/x.lock"),
    ("IN_ARTIFACTS", "ok.txt\n../../etc/passwd"),
    ("IN_ARTIFACTS", "/etc/passwd"),
    ("IN_ARTIFACTS", "a b"),
    ("IN_ARTIFACTS", "$HOME/x"),
    ("IN_ARTIFACTS", "-rf"),
    ("IN_ARTIFACTS", "\n".join(f"f{i}" for i in range(21))),
    ("IN_OS", "self-hosted"),
    ("IN_OS", "macos-latest"),
    ("IN_OS", ""),
    ("IN_NAME", "x/../y"),
    ("IN_NAME", "a b"),
    ("IN_TIMEOUT", "999"),
    ("IN_TIMEOUT", "0"),
    ("IN_TIMEOUT", "1; touch CANARY"),
    ("IN_TIMEOUT", "-5"),
]


@needs_bash
@pytest.mark.parametrize("field,value", HOSTILE)
def test_hostile_text_is_rejected_and_never_executed_or_echoed(tmp_path, field, value):
    done, outputs = _validate(tmp_path, **{field: value})
    assert done.returncode == 1, f"{field}={value!r} was accepted: {done.stdout}"
    assert "ok=false" in outputs and "verdict=rejected" in outputs
    assert not (tmp_path / "CANARY").exists(), "an input was executed"
    # a rejection prints a static message: no line of the input is repeated (a `::` line would be a workflow command)
    for piece in [p for p in re.split(r"\s+", value) if len(p) > 3]:
        assert piece not in done.stdout, f"the rejection echoed {piece!r}"


@needs_bash
@pytest.mark.parametrize("needle,label,field,value", [
    ('|| reject "repo"', "repo", "IN_REPO", "octocat/Hello-World; touch CANARY #"),
    # ref has two overlapping guards (the character class and the case list): each mutant uses an input only ITS guard catches
    ('|| reject "ref" "ref must be a plain', "ref", "IN_REF", 'main"; touch CANARY #'),
    ('-* | *..* | */ | *//* | *.lock) reject "ref"', "ref", "IN_REF", "-x"),
    ('reject "artifacts" "an artifact path has a character', "artifacts", "IN_ARTIFACTS", "a b"),
    ('reject "artifacts" "an artifact path is absolute', "artifacts", "IN_ARTIFACTS", "/etc/passwd"),
    ('*) reject "os"', "os", "IN_OS", "self-hosted"),
    ('|| reject "artifact_name"', "artifact_name", "IN_NAME", "x/../y"),
])
def test_a_validator_with_one_check_removed_is_caught_by_the_hostile_cases(tmp_path, needle, label, field, value):
    """Mutants: delete one check and the matching hostile input no longer gets that check's rejection,
    so the hostile tests above have teeth. (Each mutant removes exactly one guard.)"""
    src = VALIDATOR.read_text(encoding="utf-8").splitlines()
    kept = [line for line in src if needle not in line]
    assert len(kept) == len(src) - 1, f"mutation must remove exactly one line, removed {len(src) - len(kept)}"
    mutant = tmp_path / "mutant.sh"
    mutant.write_text("\n".join(kept) + "\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_OUTPUT": str(tmp_path / "gho")}
    env.update(GOOD)
    env[field] = value
    done = subprocess.run(["bash", str(mutant)], env=env, capture_output=True, text=True, cwd=tmp_path)
    real, _ = _validate(tmp_path, **{field: value})
    assert real.returncode == 1 and f"REJECTED ({label})" in real.stdout, "the real validator did not reject it for this reason"
    assert f"REJECTED ({label})" not in done.stdout, "the mutant still rejected for the same reason: the removed line was not the one doing the work"
