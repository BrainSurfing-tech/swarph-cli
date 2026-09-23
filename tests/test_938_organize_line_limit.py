"""#938: MEMORY.md line budget + cut_line findings; SUPERSEDED -> mention."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from swarph_cli.dreaming.index_check import (
    BUDGET, LINE_LIMIT, LINE_TARGET, TARGET,
)
from swarph_cli.dreaming.verify import verify


def _run_check(mem: Path) -> tuple[int, str]:
    script = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "dreaming" / "index_check.py"
    r = subprocess.run(
        [sys.executable, str(script), "--mem-dir=%s" % mem, "--quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _index(mem: Path, n_lines: int, line_bytes: int = 40) -> None:
    """Write MEMORY.md with n_lines short pointer rows (~line_bytes each)."""
    mem.mkdir(parents=True, exist_ok=True)
    pad = max(1, line_bytes - 20)
    rows = []
    for i in range(n_lines):
        name = "f%04d.md" % i
        (mem / name).write_text("# %s\n" % name, encoding="utf-8")
        title = ("t%d" % i).ljust(pad, "x")
        rows.append("- [%s](%s)" % (title[:pad], name))
    (mem / "MEMORY.md").write_text("\n".join(rows), encoding="utf-8")


def test_938_many_lines_under_byte_budget_finding(tmp_path):
    """(b) MANY lines, UNDER byte budget -> LINE finding (lab's 197-line case)."""
    mem = tmp_path / "mem"
    _index(mem, 197, line_bytes=40)
    size = len((mem / "MEMORY.md").read_bytes())
    assert size < BUDGET, "fixture must be under byte budget; got %d" % size
    assert 197 > LINE_TARGET
    rc, out = _run_check(mem)
    assert rc == 1
    assert "LINES:" in out
    assert "CUT:" in out
    assert "cut_line=" in out


def test_938_few_lines_over_byte_budget_finding(tmp_path):
    """(a) FEW lines, OVER byte budget -> BYTE finding (droplet's 94-line case)."""
    mem = tmp_path / "mem"
    mem.mkdir()
    # ~300-byte titles so ~90 lines exceed BUDGET
    rows = []
    for i in range(90):
        name = "long%03d.md" % i
        (mem / name).write_text("# x\n", encoding="utf-8")
        title = ("L%d-" % i) + ("W" * 280)
        rows.append("- [%s](%s)" % (title, name))
    (mem / "MEMORY.md").write_text("\n".join(rows), encoding="utf-8")
    size = len((mem / "MEMORY.md").read_bytes())
    nlines = len(rows)
    assert size > BUDGET, "fixture must exceed byte budget; got %d" % size
    assert nlines < LINE_TARGET, "fixture must be under line target; got %d" % nlines
    rc, out = _run_check(mem)
    assert rc == 1
    assert "BUDGET:" in out
    assert "CUT:" in out
    assert "BYTES binds first" in out
    assert "first lost pointers:" in out


def test_938_under_both_clean(tmp_path):
    """(c) under BOTH budgets -> clean."""
    mem = tmp_path / "mem"
    _index(mem, 150, line_bytes=40)
    # Both indexes must exist or MISSING INDEX fires (not a budget finding).
    (mem / "MEMORY_FULL.md").write_text(
        (mem / "MEMORY.md").read_text(encoding="utf-8"), encoding="utf-8")
    size = len((mem / "MEMORY.md").read_bytes())
    assert size <= BUDGET and 150 <= LINE_TARGET
    rc, out = _run_check(mem)
    assert rc == 0, out


def test_938_over_both_names_first_cut(tmp_path):
    """(d) over BOTH -> finding naming whichever cuts FIRST."""
    mem = tmp_path / "mem"
    mem.mkdir()
    rows = []
    for i in range(220):
        name = "b%04d.md" % i
        (mem / name).write_text("# x\n", encoding="utf-8")
        title = ("B%d-" % i) + ("Z" * 200)
        rows.append("- [%s](%s)" % (title, name))
    (mem / "MEMORY.md").write_text("\n".join(rows), encoding="utf-8")
    text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) > BUDGET
    assert len(rows) > LINE_LIMIT
    rc, out = _run_check(mem)
    assert rc == 1
    assert "CUT:" in out
    assert "BYTES binds first" in out


def test_938_lines_cut_first_when_over_both(tmp_path):
    """(e) 300 lines x ~100 bytes: the line ceiling binds first, at line 201."""
    mem = tmp_path / "mem"
    mem.mkdir()
    rows = []
    for i in range(300):
        name = "e%04d.md" % i
        (mem / name).write_text("# x\n", encoding="utf-8")
        title = "E" + ("Z" * 80)
        rows.append("- [%s](%s)" % (title, name))
    (mem / "MEMORY.md").write_text("\n".join(rows), encoding="utf-8")
    text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert len(rows) == 300
    assert len(text.encode("utf-8")) > BUDGET
    # ~100 bytes a line, so the byte ceiling is past line 201.
    assert 90 <= len(text.encode("utf-8")) // 300 <= 110
    rc, out = _run_check(mem)
    assert rc == 1
    assert "cut_line=201" in out
    assert "LINES binds first" in out


def test_938_dangling_missing_target(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "real.md").write_text("# real\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text(
        "- [gone](missing_file.md)\n- [ok](real.md)\n", encoding="utf-8")
    rc, out = _run_check(mem)
    assert rc == 1
    assert "DANGLING:" in out
    assert "missing_file.md" in out


def test_938_dup_target_listed(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "same.md").write_text("# same\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text(
        "- [Alpha](same.md)\n- [Beta](same.md)\n", encoding="utf-8")
    rc, out = _run_check(mem)
    assert rc == 1
    assert "DUP_TARGET:" in out
    assert "same.md" in out


def test_938_superseded_yields_mention_not_surface_disagreement(tmp_path):
    """Annotated SUPERSEDED line -> mention; no surface_disagreement (#939 addendum)."""
    mem = tmp_path / "mem"
    mem.mkdir()
    # A listen_addr that will disagree with the world, but SUPERSEDED silences it.
    (mem / "stale.md").write_text(
        "old bind 10.0.0.1:9999 ⚠️SUPERSEDED 2026-09-23 (measured)\n",
        encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [stale](stale.md)\n", encoding="utf-8")
    from swarph_cli.dreaming.clone import clone_corpus
    out = tmp_path / "clone"
    manifest = clone_corpus(mem, out)
    verdicts = verify(out, manifest)
    assert not any(v["verdict"] == "surface_disagreement" for v in verdicts)
    assert not any(v["verdict"] == "disagree" for v in verdicts)
    supers = [v for v in verdicts if v.get("reason") == "superseded"]
    assert supers, "expected superseded mention; got %r" % verdicts
    assert all(v["verdict"] == "mention" for v in supers)


def test_938_constants():
    assert LINE_LIMIT == 200
    assert LINE_TARGET == int(LINE_LIMIT * 0.94) == 188
    assert TARGET == int(BUDGET * 0.94)
