"""Card #1278: a self-notify must be delivered, and index_*.md pointers count."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from swarph_cli.dreaming import run as dreaming_run


def _run_check(mem: Path) -> tuple[int, str]:
    script = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "dreaming" / "index_check.py"
    r = subprocess.run(
        [sys.executable, str(script), "--mem-dir=%s" % mem, "--quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_1278_pointer_only_in_index_md_is_not_an_orphan(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "- [a](a.md)\n- [topics](index_topics.md)\n", encoding="utf-8")
    (mem / "MEMORY_FULL.md").write_text("- [a](a.md)\n", encoding="utf-8")
    (mem / "index_topics.md").write_text("- [b](b.md)\n", encoding="utf-8")
    (mem / "a.md").write_text("# a\n", encoding="utf-8")
    (mem / "b.md").write_text("# b\n", encoding="utf-8")
    rc, out = _run_check(mem)
    assert "ORPHANS" not in out, out
    assert "DANGLING" not in out, out
    assert rc == 0, out


def test_1278_self_notify_is_delivered_not_exit_4(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    posted = []

    def clone(corpus, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        return {}

    monkeypatch.setattr(dreaming_run, "clone_corpus", clone)
    monkeypatch.setattr(
        dreaming_run, "verify",
        lambda out, manifest: [{"verdict": "agree", "file": "a.md", "line": 1, "kind": "x"}],
    )
    monkeypatch.setattr(
        dreaming_run, "organize",
        lambda out: {"findings": [], "index_bytes_before": 0,
                     "index_bytes_after": 0, "trimmed": []},
    )
    monkeypatch.setattr(dreaming_run, "render", lambda *a, **k: "# dreaming report\n")
    monkeypatch.setattr(
        "swarph_cli.dreaming.notify._post_to_card",
        lambda card, body: posted.append((str(card), body)),
        raising=False,
    )
    monkeypatch.setattr("swarph_cli.commands.mesh._run_send", lambda ns: 1)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    rc = dreaming_run.main([
        "--corpus", str(corpus),
        "--out", str(tmp_path / "runs" / "run-1"),
        "--no-enrich",
        "--notify", "lab-ovh",
    ])
    assert rc != 4
    assert posted and posted[0][0] == "656"
    assert "dreaming" in posted[0][1] or posted[0][1]
