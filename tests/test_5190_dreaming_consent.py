"""Rework #5190: consent via public propose/apply, never a live memory store."""
import json
import pytest

from test_1570_dreaming_consolidation import dream, propose, apply  # noqa: F401


@pytest.mark.parametrize("name", ["MEMORY.md", "_index.md", "index.md", "index_topic.md", "INDEX.md"])
def test_index_instruction_rewrite_requires_owner_consent(dream, name):
    index = dream["corpus"] / name
    original = "# Index\n- [feedback one](feedback_one.md)\n- [feedback two](feedback_two.md)\n"
    index.write_text(original)
    for feedback in ("feedback_one.md", "feedback_two.md"):
        (dream["corpus"] / feedback).write_text("owner feedback\n")
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": [name],
                                          "target": name, "text": "UNTRUSTED INSTRUCTION\n"}]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    assert result["applied"] == []
    assert result["propose_only"] == [{"op": "rewrite", "files": [name]}]
    assert result["index_edits_held"] == 1
    assert json.loads((dream["out"] / "report.json").read_text())["counts"]["index_edits_held"] == 1
    assert index.read_bytes() == original.encode()
    assert apply(dream, "--allow-owner-notes") == 0
    assert index.read_text() == "UNTRUSTED INSTRUCTION\n"


def test_index_source_is_held_even_when_target_is_not_an_index(dream):
    originals = {p.name: p.read_bytes() for p in dream["corpus"].glob("*.md")}
    files = list(originals)
    assert "MEMORY.md" in files
    target = next(name for name in files if name != "MEMORY.md")
    dream["reply"].write_text(json.dumps([{"op": "merge", "files": files,
                                          "target": target, "text": "merged\n"}]))
    assert propose(dream) == 0 and apply(dream) == 0
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    assert result["applied"] == [] and result["index_edits_held"] == 1
    assert {p.name: p.read_bytes() for p in dream["corpus"].glob("*.md")} == originals


@pytest.mark.parametrize("kind", ["feedback", "user", "'feedback' # owner note", '"user" # owner note'])
def test_nested_metadata_owner_note_requires_consent(dream, kind):
    note = dream["corpus"] / "neutral.md"
    original = f"---\nmetadata:\n  type: {kind}\n---\nowner fact\n"
    note.write_text(original)
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["neutral.md"],
                                          "target": "neutral.md", "text": "revised\n"}]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    assert result["applied"] == [] and len(result["propose_only"]) == 1
    assert note.read_text() == original
    assert apply(dream, "--allow-owner-notes") == 0
    assert note.read_text() == "revised\n"


def test_report_counts_share_the_consent_detector(dream):
    notes = {
        "feedback_prefixed.md": "prefix only\n",
        "flat.md": "---\ntype: 'feedback' # owner\n---\nflat\n",
        "nested.md": '---\nmetadata:\n  type: "feedback" # owner\n---\nnested\n',
        "user_prefixed.md": "prefix only\n",
        "flat_user.md": "---\ntype: user\n---\nflat\n",
        "nested_user.md": "---\nmetadata:\n  type: user\n---\nnested\n",
    }
    for name, text in notes.items():
        (dream["corpus"] / name).write_text(text)
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["nested.md"],
                                          "target": "nested.md", "text": "ordinary\n"}]))
    assert propose(dream) == 0
    counts = json.loads((dream["out"] / "report.json").read_text())["counts"]
    assert counts["before_feedback_files"] == 3
    assert counts["after_feedback_files"] == 2
    assert counts["before_user_files"] == counts["after_user_files"] == 3
    assert apply(dream) == 0
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    assert len(result["propose_only"]) == 1 and result["index_edits_held"] == 0
