"""#689 — pkg_version must demonstrate BOTH answers, not only disagree.

All 24 live disagrees on 2026-09-05 were asserted < installed (dated records,
adjacent-tool versions, CI-matrix numbers). The order rule refuses those as
`older_than_installed`. A genuinely newer present-tense claim must still fire.
"""
from __future__ import annotations

import json
from importlib.metadata import version

from swarph_cli.commands import dreaming as dreaming_cmd
from swarph_cli.dreaming.clone import clone_corpus
from swarph_cli.dreaming.verify import verify, _version_older


def _pkg(verdicts, filename):
    return [v for v in verdicts if v.get("file") == filename and v.get("kind") == "pkg_version"]


def test_version_older_pads_and_compares():
    assert _version_older("0.0.1", "9.0.2") is True
    assert _version_older("0.53.3", "0.54.0") is True
    assert _version_older("99.0.0", "9.0.2") is False
    assert _version_older("9.0.2", "9.0.2") is False
    assert _version_older("3.10", "3.10.0") is False
    assert _version_older("not-a-ver", "1.0") is False


def test_older_than_installed_is_unprobeable_not_disagree(tmp_path):
    """pytest 0.0.1 is what this box runs — the #61 fixture. Under the order
    rule that is a past version, not a finding."""
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "old.md").write_text("pytest 0.0.1 is what this box runs\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [[old]]\n", encoding="utf-8")
    dest = tmp_path / "clone"
    manifest = clone_corpus(mem, dest)
    vs = verify(dest, manifest)
    rows = _pkg(vs, "old.md")
    assert rows, vs
    assert rows[0]["verdict"] == "unprobeable"
    assert rows[0]["reason"] == "older_than_installed"


def test_newer_than_installed_still_disagrees(tmp_path):
    """CAN-FAIL. A present-tense claim ahead of the box must still fire.
    99.0.0 is newer than any installed pytest; 0.0.1 is not this pin."""
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "new.md").write_text("pytest 99.0.0 is what this box runs\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [[new]]\n", encoding="utf-8")
    dest = tmp_path / "clone"
    vs = verify(dest, clone_corpus(mem, dest))
    rows = _pkg(vs, "new.md")
    assert rows and rows[0]["verdict"] == "disagree", rows


def test_matching_installed_version_agrees(tmp_path):
    """The other answer. A kind that cannot agree in a test cannot be trusted
    to disagree in production."""
    installed = version("pytest")
    # extractor wants major.minor or major.minor.patch — strip extra local tags
    pin = ".".join(installed.split(".")[:3])
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "now.md").write_text(f"pytest {pin} is what this box runs\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [[now]]\n", encoding="utf-8")
    dest = tmp_path / "clone"
    vs = verify(dest, clone_corpus(mem, dest))
    rows = _pkg(vs, "now.md")
    assert rows and rows[0]["verdict"] == "agree", rows


def test_can_fail_newer_claim_drives_rc_1(tmp_path):
    """Accept-test replacement for pytest 0.0.1. rc==1 must survive the order
    rule, or (a)-(c) silence the kind and the agree-rate becomes 0/0."""
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "wrong.md").write_text("pytest 99.0.0 is what this box runs\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [[wrong]]\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = dreaming_cmd.run_dreaming([
        "run", "--corpus", str(mem), "--out", str(out),
        "--verify-only", "--no-enrich",
    ])
    findings = json.loads((out / "findings.json").read_text())
    rows = _pkg(findings, "wrong.md")
    assert rows and rows[0]["verdict"] == "disagree"
    assert rc == 1
