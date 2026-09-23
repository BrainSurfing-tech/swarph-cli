"""#764: enrich collapses agreeing sessions; source_sha256 never null."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from swarph_cli.dreaming.enrich import _collapse_proposals, enrich


class _FakeClient:
    """Every session proposes the same link — the measured 26-of-1 shape."""

    def generate(self, prompt: str) -> str:
        return json.dumps([{
            "file": "project_brain_dr",
            "link": "reference_brainsurfing_tech_deploy",
            "why": "record the same underlying lesson",
        }])


def _clone_with_manifest(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    body = b"# project_brain_dr\n"
    (clone / "project_brain_dr.md").write_bytes(body)
    (clone / "reference_brainsurfing_tech_deploy.md").write_text("# ref\n", encoding="utf-8")
    sha = hashlib.sha256(body).hexdigest()
    manifest = {
        "files": {
            "project_brain_dr.md": {"sha256": sha, "size": len(body), "mtime": 0},
            "reference_brainsurfing_tech_deploy.md": {
                "sha256": hashlib.sha256(b"# ref\n").hexdigest(),
                "size": 6, "mtime": 0,
            },
        }
    }
    (clone / "clone-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return clone


def _two_session_records() -> list[dict]:
    return [
        {"_src": {"session_id": "sess-a", "offset": 0, "end": 10},
         "message": {"content": "working on brain_dr deploy link"}},
        {"_src": {"session_id": "sess-b", "offset": 10, "end": 20},
         "message": {"content": "same brain_dr deploy link again"}},
    ]


def test_764_two_sessions_one_row_supported_by_2(tmp_path):
    """CAN-FAIL: two agreeing sessions -> ONE row with supported_by=2."""
    clone = _clone_with_manifest(tmp_path)
    rows = enrich(clone, _two_session_records(), client=_FakeClient())
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["file"] == "project_brain_dr"
    assert row["proposed_link"] == "reference_brainsurfing_tech_deploy"
    assert row["supported_by"] == 2
    assert set(row["sessions"]) == {"sess-a", "sess-b"}
    assert row.get("source_sha256")
    assert row["source_sha256"] is not None


def test_764_source_sha256_populated_or_absent():
    """Null provenance key must not appear — populate or drop (#764)."""
    collapsed = _collapse_proposals([
        {"file": "a", "proposed_link": "b", "rationale": "x",
         "session_id": "s1", "source_sha256": None},
        {"file": "a", "proposed_link": "b", "rationale": "x",
         "session_id": "s2"},
    ])
    assert len(collapsed) == 1
    assert "source_sha256" not in collapsed[0]
    assert collapsed[0]["supported_by"] == 2


def test_764_manifest_sha_without_md_suffix(tmp_path):
    clone = _clone_with_manifest(tmp_path)
    rows = enrich(clone, _two_session_records(), client=_FakeClient())
    expected = json.loads((clone / "clone-manifest.json").read_text())["files"][
        "project_brain_dr.md"]["sha256"]
    assert rows[0]["source_sha256"] == expected


def test_333_one_session_two_identical_proposals_supported_by_1():
    """ONE session emitting the same link twice -> supported_by 1 (#333)."""
    collapsed = _collapse_proposals([
        {"file": "a.md", "proposed_link": "b.md", "rationale": "x", "session_id": "s1"},
        {"file": "a.md", "proposed_link": "b.md", "rationale": "x", "session_id": "s1"},
    ])
    assert len(collapsed) == 1
    assert collapsed[0]["supported_by"] == 1
    assert collapsed[0]["sessions"] == ["s1"]
    assert collapsed[0]["supported_by"] == len(collapsed[0]["sessions"])


def test_333_normalised_link_key_collapses_whitespace():
    """'b.md' and 'b.md ' -> one row (#333)."""
    collapsed = _collapse_proposals([
        {"file": "a.md", "proposed_link": "b.md", "rationale": "x", "session_id": "s1"},
        {"file": "a.md", "proposed_link": "b.md ", "rationale": "x", "session_id": "s2"},
    ])
    assert len(collapsed) == 1
    assert collapsed[0]["supported_by"] == 2
    assert set(collapsed[0]["sessions"]) == {"s1", "s2"}


def test_333_self_link_dropped():
    """MEMORY.md -> MEMORY.md proposal -> no row (#333)."""
    collapsed = _collapse_proposals([
        {"file": "MEMORY.md", "proposed_link": "MEMORY.md", "rationale": "x",
         "session_id": "s1"},
        {"file": "memory.md", "proposed_link": "MEMORY.md", "rationale": "y",
         "session_id": "s2"},
    ])
    assert collapsed == []
