"""#734 can-fail: missing workers/SLM must not exit 1 as findings."""
from __future__ import annotations

import builtins

import pytest

from swarph_cli.commands import dreaming as dreaming_cmd
from swarph_cli.dreaming.enrich import ENRICH_SKIPPED_NO_SLM


def _tiny_corpus(tmp_path):
    corpus = tmp_path / "mem"
    corpus.mkdir()
    (corpus / "MEMORY.md").write_text("# index\n\n- [[fact-one]]\n", encoding="utf-8")
    (corpus / "fact-one.md").write_text("# Fact one\nplain claim.\n", encoding="utf-8")
    return corpus, tmp_path / "out"


def _block_workers_import(monkeypatch):
    real_import = builtins.__import__

    def blocker(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "workers" or name.startswith("workers."):
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocker)


def test_734_absent_slm_skips_enrich_not_findings_rc(tmp_path, monkeypatch):
    """CAN-FAIL: no workers → report skip line; rc != 1 solely from import crash."""
    _block_workers_import(monkeypatch)
    corpus, out = _tiny_corpus(tmp_path)
    rc = dreaming_cmd.run_dreaming([
        "run", "--corpus", str(corpus), "--out", str(out),
        "--cursor", str(tmp_path / "cursor.json"),
    ])
    assert rc in (0, 1, 3), f"skip must not refuse; got rc={rc}"
    report = (out / "dreaming-report.md").read_text(encoding="utf-8")
    assert ENRICH_SKIPPED_NO_SLM in report
    assert "ModuleNotFoundError" not in report
    # Import crash must not be the only signal: report exists with verify output
    assert "DID WE LOOK?" in report
    # rc=1 only allowed if there are real findings in the report
    if rc == 1:
        assert "disagree" in report or "surface_disagreement" in report


def test_734_explicit_enrich_refuses_when_slm_absent(tmp_path, monkeypatch):
    """Explicit --enrich with no client → rc=2, not findings."""
    _block_workers_import(monkeypatch)
    corpus, out = _tiny_corpus(tmp_path)
    rc = dreaming_cmd.run_dreaming([
        "run", "--corpus", str(corpus), "--out", str(out),
        "--cursor", str(tmp_path / "cursor.json"),
        "--enrich",
    ])
    assert rc == 2


def test_734_help_documents_enrich_skip(capsys):
    rc = dreaming_cmd.run_dreaming(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "enrich skipped: no SLM client" in out
