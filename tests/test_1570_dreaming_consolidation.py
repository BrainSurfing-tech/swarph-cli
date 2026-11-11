"""Public dreaming verbs: scratch corpora and fake subscription CLI binaries only."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from swarph_cli.commands.dreaming import run_dreaming


@pytest.fixture
def dream(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("real fake-CLI executable fixtures use POSIX shebangs; bench runner tests cover Windows")
    home = tmp_path / "home"
    home.mkdir()
    corpus = tmp_path / "memory"
    corpus.mkdir()
    (corpus / "MEMORY.md").write_text("# Index\n- [lesson](lesson.md)\n")
    (corpus / "lesson.md").write_text("# Lesson\nKeep the exact command.\n")
    state, out = tmp_path / "state", tmp_path / "proposal"
    binary = tmp_path / "bin"
    binary.mkdir()
    reply, log = tmp_path / "reply.json", tmp_path / "cli.jsonl"
    reply.write_text("[]")
    fake = f"""#!{sys.executable}
import json, os, pathlib, sys
prompt = sys.stdin.read()
with open({str(log)!r}, 'a') as f:
    f.write(json.dumps({{'argv':sys.argv, 'env':{{k:'present' for k in os.environ}}, 'prompt':prompt, 'cwd':os.getcwd()}})+'\\n')
text = pathlib.Path({str(reply)!r}).read_text()
if pathlib.Path(sys.argv[0]).name == 'claude':
    print(json.dumps({{'type':'system','subtype':'init','apiKeySource':'none'}}))
    print(json.dumps({{'type':'result','result':text,'usage':{{'input_tokens':10,'output_tokens':5}}}}))
else:
    print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':text}}}}))
    print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':10,'output_tokens':5}}}}))
"""
    for name in ("claude", "codex"):
        path = binary / name
        path.write_text(fake)
        path.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    return {"home": home, "corpus": corpus, "state": state, "out": out,
            "reply": reply, "log": log}


def propose(dream, harness="claude", cell="fixture", **kwargs):
    args = ["propose", "--cell", cell, "--corpus", str(dream["corpus"]),
            "--out", str(dream["out"]), "--state-root", str(dream["state"]),
            "--harness", harness, "--model", "fixture-model"]
    for key, value in kwargs.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    return run_dreaming(args)


def test_propose_is_toolless_and_leaves_live_corpus_identical(dream, monkeypatch):
    before = {p.name: p.read_bytes() for p in dream["corpus"].iterdir()}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-billing-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-auth-token")
    monkeypatch.setenv("SWARPH_CHANNEL_TEST", "fake-channel")
    assert propose(dream) == 0
    assert {p.name: p.read_bytes() for p in dream["corpus"].iterdir()} == before
    assert json.loads((dream["out"] / "edits.json").read_text()) == []
    manifest = json.loads((dream["out"] / "clone-manifest.json").read_text())
    assert manifest["files"]["lesson.md"]["sha256"] == hashlib.sha256(before["lesson.md"]).hexdigest()
    call, = [json.loads(line) for line in dream["log"].read_text().splitlines()]
    from swarph_cli.bench.claude_cli import build_argv
    assert call["argv"][1:] == build_argv("fixture-model", call["argv"][call["argv"].index("--system-prompt") + 1])[1:]
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SWARPH_CHANNEL_TEST"} & call["env"].keys()


@pytest.mark.parametrize("name", ["../escape.md", "/tmp/x.md", "sub/note.md", "unknown.md", "x\\note.md"])
def test_untrusted_edit_names_are_refused(dream, name):
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": [name], "target": name, "text": "bad"}]))
    assert propose(dream) == 2
    assert not (dream["out"] / "edits.json").exists()


def apply(dream, *extra):
    return run_dreaming(["apply", "--cell", "fixture", "--corpus", str(dream["corpus"]),
                        "--proposal", str(dream["out"]), "--state-root", str(dream["state"]),
                        "--idle-minutes", "0", *extra])


def test_rewrite_preserves_every_preimage_and_whole_backup_restores(dream):
    import tarfile
    (dream["corpus"] / "other.bin").write_bytes(b"\x00\x01opaque")
    (dream["corpus"] / "nested").mkdir()
    (dream["corpus"] / "nested/untouched").write_text("not in the clone")
    before = {str(p.relative_to(dream["corpus"])): p.read_bytes()
              for p in dream["corpus"].rglob("*") if p.is_file()}
    inode = dream["corpus"].stat().st_ino
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["lesson.md"],
                                          "target": "lesson.md", "text": "# Better lesson\n"}]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    assert (dream["corpus"] / "lesson.md").read_text() == "# Better lesson\n"
    assert dream["corpus"].stat().st_ino == inode
    for name in set(before) - {"lesson.md"}:
        assert (dream["corpus"] / name).read_bytes() == before[name]
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    backup = Path(result["backup"])
    assert backup.stat().st_mode & 0o777 == 0o600
    with tarfile.open(backup) as archive:
        assert len([m for m in archive.getmembers() if m.isfile()]) == len(before)
        assert archive.extractfile("other.bin").read() == before["other.bin"]
    assert any(p.read_bytes() == before["lesson.md"] for p in dream["state"].rglob("lesson.md"))
    assert run_dreaming(["restore", "--cell", "fixture", "--corpus", str(dream["corpus"]),
                        "--backup", str(backup), "--state-root", str(dream["state"]),
                        "--idle-minutes", "0"]) == 0
    assert {str(p.relative_to(dream["corpus"])): p.read_bytes()
            for p in dream["corpus"].rglob("*") if p.is_file()} == before


def test_a_note_changed_after_proposal_is_skipped_and_reported(dream):
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["lesson.md"],
                                          "target": "lesson.md", "text": "model text"}]))
    assert propose(dream) == 0
    (dream["corpus"] / "lesson.md").write_text("writer's newer fact\n")
    assert apply(dream) == 0
    assert (dream["corpus"] / "lesson.md").read_text() == "writer's newer fact\n"
    result = json.loads((dream["out"] / "apply-result.json").read_text())
    assert result["skipped"] == [{"op": "rewrite", "files": ["lesson.md"]}]


@pytest.mark.parametrize("name,text", [
    ("user_person.md", "private user fact\n"),
    ("feedback_lesson.md", "private feedback\n"),
    ("neutral.md", "---\ntype: user\n---\nprivate user fact\n"),
    ("neutral.md", "---\ntype: 'feedback' # owner note\n---\nprivate feedback\n"),
    ("neutral.md", "---\ntype: !!str user\n---\nprivate user fact\n"),
])
def test_owner_notes_are_propose_only_until_explicit_owner_flag(dream, name, text):
    (dream["corpus"] / name).write_text(text)
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": [name], "target": name, "text": "approved\n"}]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    assert (dream["corpus"] / name).read_text() == text
    assert apply(dream, "--allow-owner-notes") == 0
    assert (dream["corpus"] / name).read_text() == "approved\n"


@pytest.mark.parametrize("harness", ["muse", "opencode", "grok", "gemini"])
def test_unsupported_harness_refused_before_spawn(dream, harness, capsys):
    assert propose(dream, harness=harness) == 2
    assert "only isolated claude/codex" in capsys.readouterr().err
    assert not dream["log"].exists()


def test_codex_transport_uses_exact_bench_flags_scrub_and_billing_guard(dream, monkeypatch):
    auth = dream["home"] / ".codex/auth.json"
    auth.parent.mkdir()
    auth.write_text('{"auth_mode":"api"}')
    monkeypatch.setenv("CODEX_HOME", str(auth.parent))
    assert propose(dream, harness="codex") == 2
    assert not dream["log"].exists()
    dream["out"] = dream["out"].with_name("second-proposal")
    auth.write_text('{"auth_mode":"chatgpt"}')
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "SWARPH_CHANNEL_TEST"):
        monkeypatch.setenv(name, "fake-billing-or-channel")
    assert propose(dream, harness="codex") == 0
    call, = [json.loads(line) for line in dream["log"].read_text().splitlines()]
    from swarph_cli.bench.codex_cli import build_argv
    assert call["argv"][1:] == build_argv("fixture-model")[1:]
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "SWARPH_CHANNEL_TEST"} & call["env"].keys()
    assert list(Path(call["cwd"]).glob("*")) == []


def test_recent_write_refuses_apply_without_mutation(dream):
    assert propose(dream) == 0
    assert run_dreaming(["apply", "--cell", "fixture", "--corpus", str(dream["corpus"]),
                        "--proposal", str(dream["out"]), "--state-root", str(dream["state"])]) == 2
    assert not list(dream["state"].rglob("*.tar.gz"))


def test_incremental_cursor_is_per_cell_not_shared_home(dream):
    assert propose(dream, cell="first") == 0
    dream["out"] = dream["out"].with_name("unchanged")
    assert propose(dream, cell="first") == 0
    assert len(dream["log"].read_text().splitlines()) == 1
    dream["out"] = dream["out"].with_name("other-cell")
    assert propose(dream, cell="second") == 0
    assert len(dream["log"].read_text().splitlines()) == 2
    cursors = list(dream["state"].glob("*/*/cursor.json"))
    assert len(cursors) == 2
    assert {p.parent.parent.name for p in cursors} == {"first", "second"}


def test_tiny_token_cap_never_starts_a_model_and_defers_notes(dream):
    assert propose(dream, token_cap=1) == 0
    assert not dream["log"].exists()
    report = json.loads((dream["out"] / "report.json").read_text())
    assert report["counts"]["deferred"] == 2
    assert not json.loads(next(dream["state"].rglob("cursor.json")).read_text())


def test_capped_run_then_retry_covers_each_unchanged_file_once(dream):
    for n in range(2):
        (dream["corpus"] / f"bulk_{n}.md").write_text("x" * 2500)
    assert propose(dream, token_cap=1000) == 0
    first = json.loads((dream["out"] / "report.json").read_text())
    assert first["counts"]["deferred"] > 0
    dream["out"] = dream["out"].with_name("retry-budget")
    assert propose(dream, token_cap=1000) == 0
    calls = [json.loads(line) for line in dream["log"].read_text().splitlines()]
    names = [name for call in calls for name in json.loads(call["prompt"])]
    assert sorted(names) == sorted(p.name for p in dream["corpus"].glob("*.md"))
    assert len(names) == len(set(names))


def test_card_report_contains_counts_basenames_and_ops_never_note_text(dream, capsys):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"id": 1570, "thread_uuid": "fixture-thread", "assignee": "lab-ovh"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":1}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    token = dream["home"] / "fixture-token"
    token.write_text("fake-gateway-token")
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["lesson.md"],
                                          "target": "lesson.md", "text": "PRIVATE-NOTE-TEXT-1570"}]))
    try:
        assert propose(dream, notify_card=1570, gateway=f"http://127.0.0.1:{server.server_port}",
                       token_file=token) == 0
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    body, = calls
    assert body["kind"] == "fyi" and body["thread_id"] == "fixture-thread"
    assert "PRIVATE-NOTE-TEXT-1570" not in json.dumps(body)
    report = json.loads(body["content"])
    assert report["operations"] == [{"op": "rewrite", "files": ["lesson.md"]}]
    assert all(isinstance(value, int) for value in report["counts"].values())
    output = capsys.readouterr()
    assert "PRIVATE-NOTE-TEXT-1570" not in output.out + output.err


def test_merge_retire_and_rewrite_keep_every_original_line_in_archive_or_corpus(dream):
    (dream["corpus"] / "duplicate.md").write_text("unique duplicate fact\n")
    (dream["corpus"] / "old.md").write_text("retired but never lost\n")
    before_lines = {line for p in dream["corpus"].glob("*.md") for line in p.read_text().splitlines()}
    dream["reply"].write_text(json.dumps([
        {"op": "merge", "files": ["lesson.md", "duplicate.md"], "target": "lesson.md", "text": "merged result\n"},
        {"op": "retire", "files": ["old.md"]},
    ]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    assert not (dream["corpus"] / "old.md").exists()
    assert not (dream["corpus"] / "duplicate.md").exists()
    assert (dream["corpus"] / "lesson.md").read_text() == "merged result\n"
    saved = list(dream["corpus"].glob("*.md")) + list(dream["state"].glob("*/*/archive/*/*.md"))
    assert before_lines <= {line for p in saved for line in p.read_text().splitlines()}


@pytest.mark.parametrize("op", ["promote", "demote"])
def test_index_operations_only_change_explicit_target(dream, op):
    note_before = (dream["corpus"] / "lesson.md").read_bytes()
    dream["reply"].write_text(json.dumps([{"op": op, "files": ["MEMORY.md", "lesson.md"],
                                          "target": "MEMORY.md", "text": "# Reviewed index\n"}]))
    assert propose(dream) == 0
    assert apply(dream) == 0
    assert (dream["corpus"] / "MEMORY.md").read_text() == "# Reviewed index\n"
    assert (dream["corpus"] / "lesson.md").read_bytes() == note_before


def test_backup_retention_and_checked_file_count_precede_live_writes(dream):
    import tarfile
    assert propose(dream) == 0
    for _ in range(3):
        assert apply(dream, "--keep-backups", "2") == 0
    backups = sorted(dream["state"].rglob("*.tar.gz"))
    assert len(backups) == 2
    for path in backups:
        with tarfile.open(path) as archive:
            assert set(m.name for m in archive.getmembers()) == {"MEMORY.md", "lesson.md"}


def test_default_backup_location_is_persistent_and_per_cell(dream):
    assert propose(dream) == 0
    assert run_dreaming(["apply", "--cell", "fixture", "--corpus", str(dream["corpus"]),
                        "--proposal", str(dream["out"]), "--idle-minutes", "0"]) == 0
    backup = Path(json.loads((dream["out"] / "apply-result.json").read_text())["backup"])
    relative = backup.relative_to(dream["home"])
    assert relative.parts[:5] == (".local", "state", "swarph", "dreaming", "fixture")
    assert "tmp" not in relative.parts  # Persistent HOME state, not tempfile.gettempdir().


def test_claude_billing_source_refusal_produces_no_edit_list(dream):
    binary = dream["log"].parent / "bin/claude"
    binary.write_text(binary.read_text().replace("'apiKeySource':'none'", "'apiKeySource':'env'"))
    assert propose(dream) == 2
    assert not (dream["out"] / "edits.json").exists()


def test_one_lock_per_corpus_even_for_different_cells(dream):
    import threading
    import time
    binary = dream["log"].parent / "bin/claude"
    block = dream["log"].parent / "hold"
    block.touch()
    original = binary.read_text()
    original = original.replace("text = pathlib.Path", f"""import time
deadline = time.monotonic() + 3
while pathlib.Path({str(block)!r}).exists() and time.monotonic() < deadline:
    time.sleep(.01)
text = pathlib.Path""")
    binary.write_text(original)
    results = []
    first = threading.Thread(target=lambda: results.append(propose(dream)))
    first.start()
    try:
        deadline = time.monotonic() + 3
        while not dream["log"].exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert dream["log"].exists()
        other = {**dream, "out": dream["out"].with_name("concurrent")}
        assert propose(other, cell="other-cell") == 2
    finally:
        block.unlink(missing_ok=True)
        first.join(timeout=5)
    assert results == [0]
    assert len(dream["log"].read_text().splitlines()) == 1


def test_backup_count_mismatch_refuses_before_first_live_write(dream, monkeypatch):
    import tarfile
    dream["reply"].write_text(json.dumps([{"op": "rewrite", "files": ["lesson.md"],
                                          "target": "lesson.md", "text": "must not land"}]))
    assert propose(dream) == 0
    before = (dream["corpus"] / "lesson.md").read_bytes()
    real = tarfile.TarFile.getmembers
    monkeypatch.setattr(tarfile.TarFile, "getmembers", lambda archive: real(archive)[:-1])
    assert apply(dream) == 2
    assert (dream["corpus"] / "lesson.md").read_bytes() == before


def test_recall_instrument_pins_questions_before_first_pass_and_reports_paired_difference(dream, capsys):
    questions = dream["home"] / "questions.json"
    questions.write_text(json.dumps({"tag": "frozen-v1", "questions": [
        {"id": f"q{n}", "question": f"Fixture question {n}?", "answer": str(n)} for n in range(30)]}))
    assert propose(dream, questions=questions) == 0
    sha = hashlib.sha256(questions.read_bytes()).hexdigest()
    before, after = dream["home"] / "before.json", dream["home"] / "after.json"
    data = {"model": "same-reader", "question_set_tag": "frozen-v1", "question_set_sha256": sha,
            "answers": {f"q{n}": str(n) for n in range(30)}}
    after.write_text(json.dumps(data))
    data["answers"]["q0"] = "wrong"
    before.write_text(json.dumps(data))
    args = ["recall-score", "--cell", "fixture", "--corpus", str(dream["corpus"]),
            "--state-root", str(dream["state"]), "--questions", str(questions),
            "--before", str(before), "--after", str(after)]
    capsys.readouterr()
    assert run_dreaming(args) == 0
    score = json.loads(capsys.readouterr().out)
    assert score == {"questions": 30, "before_correct": 29, "after_correct": 30, "paired_difference": 1}
    questions.write_text(questions.read_text() + " ")
    assert run_dreaming(args) == 2


def test_nightly_templates_are_propose_only_and_contain_no_billing_values():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/dreaming/swarph-dreaming@.service").read_text()
    timer = (root / "deploy/dreaming/swarph-dreaming@.timer").read_text()
    assert "dreaming propose --cell %i" in service
    assert "dreaming apply" not in service
    assert "OnCalendar=*-*-* 01:00:00 UTC" in timer
    assert "RandomizedDelaySec=30min" in timer
    assert "Persistent=true" not in timer
    assert "UnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN" in service
    assert not any("KEY=" in line or "TOKEN=" in line for line in service.splitlines() if line.startswith("Environment="))


@pytest.mark.parametrize("failure", ["short", "model", "tag", "late", "unmatchable"])
def test_recall_refuses_invalid_frozen_measurement(dream, failure):
    questions = dream["home"] / "frozen.json"
    questions.write_text(json.dumps({"tag": "frozen", "questions": [
        {"id": str(n), "question": "fixture", "answer": n} for n in range(29 if failure == "short" else 30)]}))
    rc = propose(dream, questions=questions)
    if failure == "short":
        assert rc == 2 and not dream["log"].exists()
        return
    assert rc == 0
    answers = {"model": "same", "question_set_tag": "frozen", "question_set_sha256": hashlib.sha256(questions.read_bytes()).hexdigest(),
               "answers": {str(n): n for n in range(30)}}
    before, after = dream["home"] / "before.json", dream["home"] / "after.json"
    before.write_text(json.dumps(answers))
    if failure == "model":
        answers["model"] = "different"
    elif failure == "tag":
        answers["question_set_tag"] = "different"
    elif failure == "unmatchable":
        answers["answers"]["0"] = {}
    elif failure == "late":
        receipt = next(dream["state"].rglob("first-propose.json"))
        pinned = json.loads(receipt.read_text())
        pinned["recorded_at"] = pinned["first_pass_at"] + 1
        receipt.write_text(json.dumps(pinned))
    after.write_text(json.dumps(answers))
    assert run_dreaming(["recall-score", "--cell", "fixture", "--corpus", str(dream["corpus"]),
                        "--state-root", str(dream["state"]), "--questions", str(questions),
                        "--before", str(before), "--after", str(after)]) == 2


def test_nightly_default_creates_unique_private_run_directories(dream):
    args = ["propose", "--cell", "fixture", "--corpus", str(dream["corpus"]),
            "--state-root", str(dream["state"]), "--harness", "claude", "--model", "fixture-model"]
    assert run_dreaming(args) == 0
    assert run_dreaming(args) == 0
    runs = list(dream["state"].glob("*/*/proposals/*"))
    assert len(runs) == 2
    assert all((run / "report.json").is_file() and run.stat().st_mode & 0o777 == 0o700 for run in runs)
