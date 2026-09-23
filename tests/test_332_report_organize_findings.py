"""#332: dreaming-report.md must surface organize findings + line counts."""
from __future__ import annotations

from pathlib import Path

from swarph_cli.dreaming.report import render


def test_332_report_renders_lines_finding_and_line_counts(tmp_path):
    """CAN-FAIL: LINES finding in organized['findings'] appears in report text."""
    corpus = tmp_path / "mem"
    clone = tmp_path / "clone"
    corpus.mkdir()
    clone.mkdir()
    lines_finding = (
        "LINES: MEMORY.md is 197 lines (harness truncates after 200; "
        "target <= 188) — over target by 9"
    )
    cut_finding = (
        "CUT: MEMORY.md 24068 bytes / 197 lines — LINES binds first; "
        "cut_line=189 (harness drops from here; byte ceiling 24985, line ceiling 200, "
        "line target 188)"
    )
    dangling = "DANGLING: 1 pointer(s) to a file that does not exist"
    organized = {
        "index_bytes_before": 24068,
        "index_bytes_after": 24068,
        "index_lines_before": 197,
        "index_lines_after": 197,
        "trimmed": [],
        "findings": [cut_finding, "    first lost pointers:", dangling, lines_finding],
    }
    text = render([], organized, [], corpus, clone)
    assert "197 lines" in text
    assert "24068 bytes" in text
    assert lines_finding in text, "LINES finding must appear verbatim"
    assert cut_finding in text
    assert dangling in text
    assert "organize findings" in text
