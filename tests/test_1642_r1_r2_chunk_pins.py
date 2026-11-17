"""Card #1642 / Obligation #5735: R1, R2, and keep regression pins.

Synthetic fixtures only; every secret is assembled at runtime from short
fragments so no realistic credential literal appears in this file.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarph_cli.commands import secret_guard
from swarph_cli.commands.secret_guard import (
    _BOUNDARY_WINDOW,
    _CHUNK_CHARS,
    _MAX_PEM_CHARS,
    _chunk_classes,
    _detect_classes,
    _iter_chunks,
    run_hook,
)

_REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_HEX = "0123456789abcdef"
_B64 = _ALNUM + "+/"
_WORDS = (
    "the quick brown fox jumps over a lazy dog while seven wizards "
    "quietly judge every boxing match"
).split()


def _sk_ant(suffix: str = "") -> str:
    tail = suffix or ("Zx9Qw8" + "Er7Ty6" + "Ui5Op4" + "As3Df2" + "Gh1Jk")
    return "sk-" + "ant-" + tail


def _assert_no_secret_echo(secret: str, *texts: str) -> None:
    blob = "".join(texts)
    for i in range(len(secret) - 3):
        window = secret[i:i + 4]
        assert window not in blob, f"secret window leaked: {window!r}"


def _run(payload: dict, capsys) -> tuple[int, str, str]:
    rc = run_hook(stdin_text=json.dumps(payload))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _run_cli(prompt: str, timeout: float = 5.0) -> tuple[int, str, str, float]:
    raw = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
    env = os.environ.copy()
    env["PYTHONPATH"] = _REPO_SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli", "secret-guard", "hook"],
        input=raw,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started


def _transcript_json_line(rng: random.Random, size: int) -> str:
    def rid(prefix: str, n: int = 24) -> str:
        return prefix + "".join(rng.choice(_ALNUM) for _ in range(n))

    def uuid() -> str:
        h = "".join(rng.choice(_HEX) for _ in range(32))
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    parts: list[str] = []
    while sum(map(len, parts)) < size:
        parts.append(
            json.dumps({
                "type": "assistant",
                "uuid": uuid(),
                "parentUuid": uuid(),
                "requestId": rid("req_01"),
                "message": {
                    "id": rid("msg_01"),
                    "content": [{
                        "type": "tool_use",
                        "id": rid("toolu_01"),
                        "name": "Bash",
                        "input": {"command": "ls " + rng.choice(_WORDS)},
                    }],
                },
            })
        )
    return "look at this row: " + ", ".join(parts)


# --- R1: transcript-shaped JSON lines with generated ids never block --------


@pytest.mark.parametrize("size", [2_500, 5_000, 10_000, 40_000])
def test_r1_transcript_json_line_with_generated_ids_never_blocks(capsys, size):
    blocked_detect = 0
    blocked_hook = 0
    for seed in range(40):
        rng = random.Random(seed * 7 + size)
        line = _transcript_json_line(rng, size)
        classes, complete = _detect_classes(line)
        assert complete is True
        if classes:
            blocked_detect += 1
        rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": line}, capsys)
        assert rc == 0
        if out.strip():
            parsed = json.loads(out)
            if parsed.get("decision") == "block":
                blocked_hook += 1
    assert blocked_detect == 0
    assert blocked_hook == 0


def test_r1_oversized_id_prescrub_does_not_block_or_drop_real_secret(capsys):
    rng = random.Random(5735)
    oversized_id = "toolu_01" + "".join(rng.choice(_ALNUM) for _ in range(_BOUNDARY_WINDOW + 64))
    clean_line = ("word " * 800) + oversized_id + (" tail" * 800)
    classes, complete = _detect_classes(clean_line)
    assert complete is True
    assert classes == []

    secret = _sk_ant()
    dirty_line = clean_line + " " + secret
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": dirty_line}, capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo(secret, out, err)


# --- R2: slow lines inside BEGIN..END obey chunking and budget --------------


@pytest.mark.parametrize("size", [30_000, 200_000])
@pytest.mark.parametrize("label", ["PRIVATE KEY", "NOTES"])
def test_r2_slow_line_inside_pem_markers_returns_under_5s(size, label):
    slow = "mysql " * (size // 6)
    prompt = f"-----BEGIN {label}-----\n{slow}\n-----END {label}-----\n"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 5.0
    assert err == ""
    if out.strip():
        parsed = json.loads(out)
        assert parsed.get("decision") != "block"
        assert "not fully scanned" in parsed["systemMessage"]


def test_r2_cut_short_inside_pem_wrapped_slow_line_emits_system_message(capsys, monkeypatch):
    slow = "mysql " * (200_000 // 6)
    prompt = "-----BEGIN " + "PRIVATE KEY-----\n" + slow + "\n-----END " + "PRIVATE KEY-----\n"
    monkeypatch.setattr(secret_guard, "_SCAN_BUDGET_SECONDS", 0.05)
    started = time.monotonic()
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    took = time.monotonic() - started
    assert rc == 0
    assert took < 5.0
    parsed = json.loads(out)
    assert "decision" not in parsed
    assert "not fully scanned" in parsed["systemMessage"]


def test_r2_1mb_slow_line_inside_pem_markers_cli_returns_under_5s_with_system_message():
    slow = "mysql " * (1_000_000 // 6)
    prompt = "-----BEGIN " + "PRIVATE KEY-----\n" + slow + "\n-----END " + "PRIVATE KEY-----\n"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 5.0
    parsed = json.loads(out)
    assert "decision" not in parsed
    assert "not fully scanned" in parsed["systemMessage"]


# --- keep: PEM 3..51 lines, straddling keys, 10k corpus, harness envelopes --


def _make_pem(lines: int, label: str = "PRIVATE KEY", seed: int = 42) -> tuple[str, str]:
    rng = random.Random(seed + lines)
    rows = ["".join(rng.choice(_B64) for _ in range(64)) for _ in range(lines)]
    body = "\n".join(rows)
    pem = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"
    return pem, rows[0]


@pytest.mark.parametrize("label", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"])
@pytest.mark.parametrize("lines", [3, 10, 26, 51])
def test_keep_real_pem_blocks_3_to_51_lines_block_without_echo(capsys, label, lines):
    pem, first_row = _make_pem(lines, label=label)
    prompt = ("2026-01-01 INFO log line ok\n" * 120) + pem + ("2026-01-01 INFO tail\n" * 40)
    chunks = list(_iter_chunks(prompt))
    pem_chunks = [c for c in chunks if "-----BEGIN" in c and "-----END" in c]
    assert len(pem_chunks) == 1
    assert len(pem_chunks[0]) <= _MAX_PEM_CHARS

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "PEM private key" in parsed["reason"]
    _assert_no_secret_echo(first_row, out, err)


def test_keep_all_pem_sizes_3_to_51_lines_block():
    for lines in range(3, 52):
        pem, _ = _make_pem(lines)
        classes, complete = _detect_classes("prefix\n" * 80 + pem + "suffix\n" * 20)
        assert complete is True
        assert "PEM private key" in classes, lines


def test_keep_anthropic_key_straddling_every_cut_of_long_line_blocks(capsys):
    rng = random.Random(1642)
    base_words = []
    while sum(map(len, base_words)) < 10_000:
        base_words.append(rng.choice(_WORDS))
    base_line = " ".join(base_words)[:10_000]
    secret = _sk_ant()

    # Probe around every ~1536..2048 cut step across the 10,000-char line.
    cut_anchors = list(range(1500, 9000, 1500))
    offsets: list[int] = []
    for anchor in cut_anchors:
        offsets.extend(range(anchor - len(secret) - 8, anchor + 8, 5))

    for offset in offsets:
        prompt = base_line[:offset] + " " + secret + " " + base_line[offset:]
        rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
        assert rc == 0
        assert json.loads(out)["decision"] == "block", offset
        _assert_no_secret_echo(secret, out, err)


def test_keep_10000_corpus_differential_and_harness_envelopes(capsys):
    rng = random.Random(57350)
    body = "Zq7Xw9" + "Er4Ty6" + "Ui2Op8" + "As5Df3" + "Gh1JkLm"
    secrets = [
        _sk_ant(body),
        "sk-" + "proj-" + body,
        "xai-" + body,
        "gh" + "p_" + body + "Qw3Er5",
        "github_" + "pat_" + body + "Qw3Er5",
        "AI" + "za" + (body + "Qw3Er5Ty7")[:35],
        "xox" + "b-" + "1234567890-" + body,
        "AK" + "IA" + "Q7W9E4R6T2Y8U5I3",
        "aws_secret_" + "access_key = " + body + "Xc7Vb6N9",
        "ey" + "J" + body[:20] + "." + "ey" + "J" + body[:24] + "." + body[:22],
        "Authori" + "zation: Bearer " + body + "Xc7Vb6",
        "postgres://app:" + "Hq7" + body[:12] + "@db.example.test:5432/main",
        "host=db pass" + "word=" + "Hq7" + body[:12] + " dbname=main",
        "mysql -u root -p" + "Hq7" + body[:12] + " main",
        "curl -u " + "admin:" + "Hq7" + body[:12] + " https://example.test",
        "MY_SERVICE_" + "TOKEN=" + body,
        body + "Qw3Er5Ty7Ui9",
    ]

    def word_line(n: int) -> str:
        out = []
        size = 0
        while size < n:
            w = rng.choice(_WORDS)
            out.append(w)
            size += len(w) + 1
        return " ".join(out)[:n]

    def log_lines(n: int) -> str:
        out = []
        size = 0
        i = 0
        while size < n:
            ln = f"12:{i % 60:02d} INFO worker {rng.choice(_WORDS)} step {i % 97}\n"
            out.append(ln)
            size += len(ln)
            i += 1
        return "".join(out)

    corpus: list[str] = []

    # 1) 17 shapes x 300 offsets across cuts of a 4,500-char line = 5,100 texts
    base_line = word_line(4_500)
    for secret in secrets:
        for offset in range(1400, 2600, 4):
            corpus.append(base_line[:offset] + " " + secret + " " + base_line[offset:])

    # 2) 17 shapes x 45 log-line boundary positions = 765 texts
    for secret in secrets:
        for target in range(1850, 2210, 8):
            corpus.append(log_lines(target) + secret + "\n" + log_lines(800))

    # 3) Multi-line key=\nvalue around 2,048 boundary = 200 texts
    val = body + "Xc7Vb6N9"
    for first, rest in [
        ("api_" + "key=", val),
        ("pass" + "word:", "    " + val),
        ("Authori" + "zation:", " Bearer " + val),
        ('"client_' + 'secret":', '  "' + val + '"'),
    ]:
        for pad in range(1960, 2060, 2):
            head = log_lines(pad - 90)
            head += "x" * max(0, pad - len(head) - len(first) - 1) + "\n"
            corpus.append(head + first + "\n" + rest + "\n" + log_lines(500))

    # 4) Long secrets & inline PEMs across offsets = 1,050 texts
    def rand_alnum(n: int) -> str:
        return "".join(rng.choice(_ALNUM) for _ in range(n))

    long_shapes = [
        "ey" + "J" + rand_alnum(60) + "." + "ey" + "J" + rand_alnum(1300) + "." + rand_alnum(130),
        "ey" + "J" + rand_alnum(60) + "." + "ey" + "J" + rand_alnum(2800) + "." + rand_alnum(130),
        "Authori" + "zation: Bearer " + rand_alnum(900),
        "MY_SERVICE_" + "TOKEN=" + rand_alnum(800),
        rand_alnum(700),
        "-----BEGIN " + "PRIVATE KEY-----\\n" + "\\n".join(rand_alnum(64) for _ in range(25)) + "\\n-----END " + "PRIVATE KEY-----",
        "-----BEGIN " + "RSA PRIVATE KEY-----\\n" + "\\n".join(rand_alnum(64) for _ in range(50)) + "\\n-----END " + "RSA PRIVATE KEY-----",
    ]
    long_base = word_line(6_000)
    for secret in long_shapes:
        for offset in range(0, 3000, 20):
            corpus.append(long_base[:offset] + " " + secret + " " + long_base[offset:])

    # 5) PEM 3..51 lines x 20 positions = 980 texts
    for lines in range(3, 52):
        pem, _ = _make_pem(lines, seed=lines)
        for pad in range(0, 2400, 120):
            corpus.append(log_lines(pad) + pem + log_lines(600))

    # 6) Clean and id-bearing negative prompts = 2,000 texts
    for i in range(1_000):
        corpus.append(log_lines(2_200 + (i % 500)))
    for i in range(1_000):
        corpus.append(_transcript_json_line(rng, 2_500 + (i % 1_500)))

    assert len(corpus) >= 10_000

    misses = 0
    for text in corpus:
        whole_hit = bool(_chunk_classes(text))
        chunked_hits, complete = _detect_classes(text)
        assert complete is True
        if whole_hit and not bool(chunked_hits):
            misses += 1
    assert misses == 0

    # Harness-envelope prompts must never block even when wrapping secrets.
    secret = _sk_ant()
    envelopes = [
        f"<task-notification>\n<status>completed</status>\n{secret}\n</task-notification>",
        f"<system-reminder>\nNote {secret}\n</system-reminder>",
        f'<channel source="swarph" from="peer">\n{secret}\n</channel>',
        f"<system-reminder>{secret}</system-reminder>\n\n<task-notification>{secret}</task-notification>",
    ]
    for prompt in envelopes:
        rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
        assert rc == 0
        assert out == ""
        assert err == ""
