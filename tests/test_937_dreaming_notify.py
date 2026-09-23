"""#937 — dreaming run --notify sends only when the finding set changes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.dreaming import run as dreaming_run
from swarph_cli.dreaming.notify import NotifySendError


class _Sent:
    def __init__(self, fail: bool = False):
        self.bodies: list[str] = []
        self.fail = fail

    def __call__(self, cell: str, body: str) -> None:
        if self.fail:
            raise NotifySendError("boom")
        self.bodies.append(body)


def _patch(monkeypatch, verdicts):
    def clone(corpus, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        return {}

    monkeypatch.setattr(dreaming_run, "clone_corpus", clone)
    monkeypatch.setattr(dreaming_run, "verify", lambda out, manifest: verdicts)
    monkeypatch.setattr(
        dreaming_run, "organize",
        lambda out: {"findings": [], "index_bytes_before": 0,
                     "index_bytes_after": 0, "trimmed": []},
    )
    monkeypatch.setattr(dreaming_run, "render", lambda *a, **k: "# dreaming report\n")


def _run(tmp_path: Path, verdicts, sender) -> int:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    out = tmp_path / "runs" / "run-1"
    monkey_sender = sender

    def mesh_sender(cell, body):
        monkey_sender(cell, body)

    # patched per test via the sender object; mesh_sender closed over it
    import swarph_cli.dreaming.notify as notify
    notify.mesh_sender = mesh_sender  # type: ignore
    return dreaming_run.main([
        "--corpus", str(corpus),
        "--out", str(out),
        "--no-enrich",
        "--notify", "lab-ovh",
    ])


@pytest.fixture(autouse=True)
def _restore_sender():
    import swarph_cli.dreaming.notify as notify
    original = notify.mesh_sender
    yield
    notify.mesh_sender = original


def test_unchanged_set_sends_nothing(tmp_path, monkeypatch):
    verdicts = [{"verdict": "surface_disagreement", "file": "a.md", "line": 1, "kind": "unit_bind"}]
    _patch(monkeypatch, verdicts)
    state = tmp_path / "runs" / ".last-findings.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"keys": ["a.md:1:unit_bind"]}) + "\n", encoding="utf-8")
    sent = _Sent()
    rc = _run(tmp_path, verdicts, sent)
    assert sent.bodies == []
    assert rc == 1


def test_new_finding_is_named_in_the_dm(tmp_path, monkeypatch):
    verdicts = [{"verdict": "surface_disagreement", "file": "a.md", "line": 2, "kind": "unit_bind"}]
    _patch(monkeypatch, verdicts)
    state = tmp_path / "runs" / ".last-findings.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"keys": ["a.md:1:unit_bind"]}) + "\n", encoding="utf-8")
    sent = _Sent()
    rc = _run(tmp_path, verdicts, sent)
    assert rc == 1
    assert len(sent.bodies) == 1
    assert "a.md:2:unit_bind" in sent.bodies[0]
    assert "new:" in sent.bodies[0]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["keys"] == ["a.md:2:unit_bind"]


def test_cleared_finding_is_named_in_the_dm(tmp_path, monkeypatch):
    verdicts = [{"verdict": "agree", "file": "a.md", "line": 1, "kind": "unit_bind"}]
    _patch(monkeypatch, verdicts)
    state = tmp_path / "runs" / ".last-findings.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"keys": ["a.md:1:unit_bind"]}) + "\n", encoding="utf-8")
    sent = _Sent()
    rc = _run(tmp_path, verdicts, sent)
    assert rc == 0
    assert len(sent.bodies) == 1
    assert "a.md:1:unit_bind" in sent.bodies[0]
    assert "cleared:" in sent.bodies[0]


def test_memory_byte_size_does_not_change_keys_or_send(tmp_path, monkeypatch):
    """Two runs differ only by MEMORY.md's byte size and a file count."""
    counts = {"bytes": 19803, "orphans": 98}

    def organize(_out):
        return {
            "findings": [
                "memory-index-check: clean — 349 files, all indexed; "
                f"MEMORY.md {counts['bytes']}/24985 bytes (target 23485)",
                f"ORPHANS: {counts['orphans']} files in no index",
            ],
            "index_bytes_before": counts["bytes"],
            "index_bytes_after": counts["bytes"],
            "trimmed": [],
        }

    _patch(monkeypatch, [])
    monkeypatch.setattr(dreaming_run, "organize", organize)
    sent = _Sent()
    assert _run(tmp_path, [], sent) == 3
    counts["bytes"] = 21000
    counts["orphans"] = 101
    assert _run(tmp_path, [], sent) == 3
    from swarph_cli.dreaming.notify import finding_keys
    keys = finding_keys([], organize(None))
    assert keys == finding_keys([], {
        "findings": [
            "memory-index-check: clean — 349 files, all indexed; "
            "MEMORY.md 1/24985 bytes (target 1)",
            "ORPHANS: 1 files in no index",
        ]
    })
    assert len(sent.bodies) == 1
    blob = " ".join(keys)
    assert "clean" not in blob
    assert not any(ch.isdigit() for key in keys if key.startswith("organize:") for ch in key)
    assert "98 files" in sent.bodies[0]
    state = tmp_path / "runs" / ".last-findings.json"
    assert json.loads(state.read_text(encoding="utf-8"))["keys"] == keys


def test_lines_count_does_not_change_the_key_and_stays_in_the_dm(tmp_path, monkeypatch):
    line = (
        "LINES: MEMORY.md is {n} lines (harness truncates after 200; "
        "target <= 188) — over target by {over}"
    )
    counts = {"n": 197}

    def organize(_out):
        return {
            "findings": [line.format(n=counts["n"], over=counts["n"] - 188)],
            "index_bytes_before": 0,
            "index_bytes_after": 0,
            "trimmed": [],
        }

    _patch(monkeypatch, [])
    monkeypatch.setattr(dreaming_run, "organize", organize)
    from swarph_cli.dreaming.notify import finding_keys
    before = finding_keys([], organize(None))
    counts["n"] = 198
    after = finding_keys([], organize(None))
    assert before == after == ["organize:LINES:MEMORY.md"]
    assert not any(ch.isdigit() for key in before for ch in key)
    sent = _Sent()
    assert _run(tmp_path, [], sent) == 3
    assert "198" in sent.bodies[0]


def test_dup_target_keys_its_header_and_a_longer_index_changes_nothing(tmp_path, monkeypatch):
    def block(lines, warn_bytes):
        return [
            f"LINES: MEMORY.md is {lines} lines (harness truncates after 200; "
            f"target <= 188) — over target by {lines - 188}",
            "DUP_TARGET: file2.md",
            "    child-a.md",
            "    child-b.md",
            "ORPHANS: 2 memory file(s) in no index",
            "    alpha.md",
            "    beta.md",
            "CUT: MEMORY.md lost pointers",
            "    gone.md",
            f"WARN: MEMORY.md is {warn_bytes} BYTES — under the ceiling",
        ]

    from swarph_cli.dreaming.notify import finding_keys, organize_keys
    short = block(197, 19000)
    long = block(198, 19040)
    assert organize_keys(short) == organize_keys(long)
    keys = organize_keys(short)
    assert "organize:DUP_TARGET:file2.md" in keys
    assert "organize:LINES:MEMORY.md" in keys
    assert "organize:ORPHANS" in keys
    assert "organize:CUT:MEMORY.md" in keys
    assert "organize:WARN:MEMORY.md" in keys
    assert not any("child-a" in k or "alpha.md" in k or "gone.md" in k for k in keys)
    assert not any(k.startswith("organize:LINES:child") for k in keys)
    # A filename may contain a digit. A count may not add a key.
    assert any(ch.isdigit() for ch in "organize:DUP_TARGET:file2.md")

    def organize(_out):
        return {"findings": short, "index_bytes_before": 0,
                "index_bytes_after": 0, "trimmed": []}

    _patch(monkeypatch, [])
    monkeypatch.setattr(dreaming_run, "organize", organize)
    sent = _Sent()
    assert _run(tmp_path, [], sent) == 3
    assert "child-a.md" in sent.bodies[0]
    assert finding_keys([], {"findings": short}) == finding_keys([], {"findings": long})


def test_send_failure_exits_4_and_does_not_advance_state(tmp_path, monkeypatch):
    verdicts = [{"verdict": "surface_disagreement", "file": "a.md", "line": 9, "kind": "unit_bind"}]
    _patch(monkeypatch, verdicts)
    state = tmp_path / "runs" / ".last-findings.json"
    state.parent.mkdir(parents=True)
    before = json.dumps({"keys": ["old.md:1:unit_bind"]}) + "\n"
    state.write_text(before, encoding="utf-8")
    sent = _Sent(fail=True)
    rc = _run(tmp_path, verdicts, sent)
    assert rc == 4
    assert state.read_text(encoding="utf-8") == before
    assert sent.bodies == []
