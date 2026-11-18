"""Card #1642 / Obligation #5735 & #5744: R1, R2, G1, G2, R-1, R-2, and keep pins.

Synthetic fixtures only; every secret is assembled at runtime from short
fragments or generated ephemerally in memory so no realistic credential
literal appears in this file.
"""
from __future__ import annotations

import base64
import json
import os
import random
import shutil
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


def _dashed_msg_token(rng: random.Random, length: int) -> str:
    groups = ["".join(rng.choice(_ALNUM) for _ in range(30)) for _ in range(length // 31 + 1)]
    return ("msg_01" + "-".join(groups))[:length]


def _generate_ed25519_pem(rng: random.Random) -> str:
    openssl = shutil.which("openssl")
    if openssl:
        proc = subprocess.run(
            [openssl, "genpkey", "-algorithm", "ed25519"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and "PRIVATE KEY-----" in proc.stdout:
            return proc.stdout
    raw_seed = bytes(rng.randrange(256) for _ in range(32))
    der = b"\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20" + raw_seed
    b64 = base64.encodebytes(der).decode("ascii").strip()
    return "-----BEGIN " + "PRIVATE KEY-----\n" + b64 + "\n-----END " + "PRIVATE KEY-----\n"


def test_g1_key_beside_400k_and_1m_dashed_msg_token_blocks_under_1s():
    rng = random.Random(57441)
    secret = "sk-" + "ant-" + "api03-" + "".join(rng.choice(_ALNUM) for _ in range(95))
    for size in (400_000, 1_000_000):
        tok = _dashed_msg_token(rng, size)
        layouts = {
            "before_same_line": f"token {secret} then {tok} end",
            "after_same_line": f"see {tok} and token {secret} end",
            "line_before": f"token {secret}\nsee {tok} end",
            "line_after": f"see {tok} end\ntoken {secret}\n",
        }
        for name, prompt in layouts.items():
            t0 = time.monotonic()
            classes, complete = _detect_classes(prompt)
            scan_took = time.monotonic() - t0
            assert complete is True, (size, name)
            assert "Anthropic" in classes, (size, name, classes)
            assert scan_took < 1.0, (size, name, scan_took)

            rc, out, err, took = _run_cli(prompt, timeout=5.0)
            assert rc == 0, (size, name)
            cli_limit = 1.0 if size == 400_000 else 2.0
            assert took < cli_limit, (size, name, took)
            assert err == "", (size, name)
            parsed = json.loads(out)
            assert parsed["decision"] == "block", (size, name)
            assert "Anthropic" in parsed["reason"], (size, name)
            _assert_no_secret_echo(secret, out, err)


def test_g2_eddsa_key_after_9000_and_20000_char_begin_line_prefix_blocks_as_pem_key():
    rng = random.Random(57442)
    keys = [_generate_ed25519_pem(rng) for _ in range(25)]

    def word_prefix(n: int) -> str:
        out, size = [], 0
        while size < n:
            w = rng.choice(_WORDS)
            out.append(w)
            size += len(w) + 1
        return " ".join(out)[:n]

    for prefix_len in (9_000, 20_000):
        for i, pem in enumerate(keys):
            prompt = word_prefix(prefix_len) + " " + pem + "next line of ordinary words\n"
            classes, complete = _detect_classes(prompt)
            assert complete is True, (prefix_len, i)
            assert "PEM private key" in classes, (prefix_len, i, classes)

        sample_prompt = word_prefix(prefix_len) + " " + keys[0] + "next line of ordinary words\n"
        chunks = list(_iter_chunks(sample_prompt))
        pem_chunks = [c for c in chunks if c.startswith("-----BEGIN " + "PRIVATE KEY-----")]
        assert len(pem_chunks) == 1, (prefix_len, [len(c) for c in chunks])
        assert "-----END " + "PRIVATE KEY-----" in pem_chunks[0]

        rc, out, err, took = _run_cli(sample_prompt, timeout=5.0)
        assert rc == 0
        assert took < 5.0
        parsed = json.loads(out)
        assert parsed["decision"] == "block"
        assert "PEM private key" in parsed["reason"]
        _assert_no_secret_echo(keys[0].splitlines()[1][:20], out, err)


# --- R-1, R-2, R2 slow lines, and mutant kills ----------------------------


@pytest.mark.parametrize("size", [10_000, 15_000])
@pytest.mark.parametrize("sep", ["\n", "\\n"])
def test_r_minus_1_large_nonrandom_private_key_block_over_8192_chars_blocks(size, sep):
    rows = ["A" * 64 for _ in range((size // 65) + 2)]
    pem = ("-----BEGIN " + "PRIVATE KEY-----" + sep + sep.join(rows) + sep + "-----END " + "PRIVATE KEY-----")
    assert len(pem) > 8192
    prompt = "prefix log line\n" + pem + "\nsuffix log line\n"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 5.0
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "PEM private key" in parsed["reason"]


@pytest.mark.parametrize("val_len", [30, 200, 1500])
@pytest.mark.parametrize("filler_target", [820, 900])
def test_r_minus_2_carry_512_chars_across_batch_boundary_catches_multiline_key_value(
    capsys, val_len, filler_target
):
    rng = random.Random(57443 + val_len + filler_target)
    line_21 = "12:00 INFO worker ok\n"
    filler = line_21 * (filler_target // len(line_21))
    if filler_target == 820:
        filler += "12:00 ok\n"
    else:
        filler += line_21
    key_line = "x" + ("word " * 238) + "pass" + "word:"
    assert len(key_line) == 1200
    secret_val = "p@ss" + "w0rdX"
    tail_words = " ".join(rng.choice(_WORDS) for _ in range(val_len // 5 + 2))[:val_len]
    value_line = f"  {secret_val} {tail_words}"
    prompt = filler + key_line + "\n" + value_line + "\n" + ("12:01 INFO done\n" * 10)

    # Confirm the short password does NOT block standalone without the carried key name.
    assert _chunk_classes(value_line) == []

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block", (val_len, filler_target)
    assert "DSN credential" in parsed["reason"]
    _assert_no_secret_echo(secret_val, out, err)


def test_m9_inline_pem_block_kept_whole_and_oversized_span_sliced():
    rng = random.Random(57444)
    words_12k = " ".join(rng.choice(_WORDS) for _ in range(2500))[:12_000]
    pem = "-----BEGIN " + "NOTES-----" + ("A" * 3000) + "-----END " + "NOTES-----"
    inline_prompt = '{"pad":"' + words_12k + '","pem":"' + pem + '"}'
    chunks = list(_iter_chunks(inline_prompt))
    assert pem in chunks

    oversized_one_line = "-----BEGIN " + "NOTES-----" + ("A" * 20_000) + "-----END " + "NOTES-----"
    oversized_chunks = list(_iter_chunks(oversized_one_line))
    assert max(len(c) for c in oversized_chunks) <= _CHUNK_CHARS


def test_m14_long_line_overlap_512_catches_385_char_cli_password_gap(capsys):
    rng = random.Random(57445)
    base_words = []
    while sum(map(len, base_words)) < 5_000:
        base_words.append(rng.choice(_WORDS))
    base_line = " ".join(base_words)[:5_000]
    secret_val = "p@ss" + "w0rdX"
    cmd = "cu" + "rl " + ("--header X " * 32) + "-u admin:" + secret_val + " https://example.invalid"
    assert _chunk_classes("-u admin:" + secret_val + " https://example.invalid") == []
    prompt = base_line[:1700] + " " + cmd + " " + base_line[1700:]

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "CLI password" in parsed["reason"]
    _assert_no_secret_echo(secret_val, out, err)


@pytest.mark.parametrize("size", [30_000, 200_000])
@pytest.mark.parametrize("label", ["NOTES", "CERTIFICATE"])
@pytest.mark.parametrize("one_line", [False, True])
def test_r2_slow_line_inside_pem_markers_returns_under_5s(size, label, one_line):
    slow = "mysql " * (size // 6)
    sep = "" if one_line else "\n"
    prompt = f"-----BEGIN {label}-----{sep}{slow}{sep}-----END {label}-----{sep}"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 5.0
    assert err == ""
    if out.strip():
        parsed = json.loads(out)
        assert parsed.get("decision") != "block"
        assert "not fully scanned" in parsed["systemMessage"]


@pytest.mark.parametrize("one_line", [False, True])
def test_r2_cut_short_inside_pem_wrapped_slow_line_emits_system_message(capsys, monkeypatch, one_line):
    slow = "mysql " * (200_000 // 6)
    sep = "" if one_line else "\n"
    prompt = f"-----BEGIN NOTES-----{sep}{slow}{sep}-----END NOTES-----{sep}"
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
    prompt = "-----BEGIN NOTES-----\n" + slow + "\n-----END NOTES-----\n"
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


# --- #5753 G3: single-pass linear marker scan, no 64-marker cap, mutant kills ---


def test_g3_marker_floods_and_grep_cert_prompts_block_under_1s():
    rng = random.Random(57531)
    secret = "sk-" + "ant-" + "api03-" + "".join(rng.choice(_ALNUM) for _ in range(95))
    cli_limit = 1.0 if sys.platform.startswith("linux") else 2.0

    def begin_flood(size: int) -> str:
        return ("-----BEGIN A-----\n" * (size // 18)) + "-----END A-----\n"

    def end_first_flood(size: int) -> str:
        return "-----END A-----\n" + ("-----BEGIN A-----\n" * (size // 18))

    def one_begin_many_end(target_len: int) -> str:
        head = "-----BEGIN A-----\n"
        end_line = "-----END A-----\n"
        rem = target_len - len(head)
        count, tail_rem = divmod(rem, len(end_line))
        pad = ("log ok!\n" if tail_rem == 8 else "ok!\n") if tail_rem else ""
        body = head + (end_line * count) + pad
        assert len(body) == target_len
        return body

    def grep_cert(size: int) -> str:
        cert = (
            "-----BEGIN CERTIFICATE-----\n"
            + ("certificate body line of plain words only\n" * 20)
            + "-----END CERTIFICATE-----\n"
        )
        grep_line = "certs/ca-bundle/intermediate-0001.pem:1:-----BEGIN CERTIFICATE-----\n"
        return (grep_line * (size // len(grep_line))) + cert

    cases: list[tuple[str, str]] = []
    for size, exact in ((420_000, 420_010), (600_000, 600_010), (1_000_000, 1_000_006)):
        b1 = begin_flood(size)
        b2 = end_first_flood(size)
        b3 = one_begin_many_end(exact)
        assert len(b1) == exact
        assert len(b2) == exact
        cases.append((f"begin_flood_{exact}", b1))
        cases.append((f"end_first_{exact}", b2))
        cases.append((f"one_begin_many_end_{exact}", b3))

    for size, exact in ((1_000_000, 1_000_834), (2_000_000, 2_000_842)):
        g = grep_cert(size)
        assert len(g) == exact
        cases.append((f"grep_cert_{exact}", g))

    for name, body in cases:
        prompt = "token " + secret + "\n" + body
        t0 = time.monotonic()
        classes, complete = _detect_classes(prompt)
        scan_took = time.monotonic() - t0
        assert complete is True, name
        assert "Anthropic" in classes, (name, classes)
        assert scan_took < 0.5, (name, scan_took)

        rc, out, err, took = _run_cli(prompt, timeout=5.0)
        assert rc == 0, name
        assert took < cli_limit, (name, took)
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "Anthropic" in parsed["reason"], name
        _assert_no_secret_echo(secret, out, err)


def test_g3_marker_prepass_deadline_enforced_with_system_message(capsys, monkeypatch):
    prompt = ("-----BEGIN A-----\n" * 500) + "-----END A-----\n"
    pem_calls = 0

    def fake_monotonic() -> float:
        nonlocal pem_calls
        if sys._getframe(1).f_code.co_name == "_pem_envelope_classes":
            pem_calls += 1
            return 100.0 if pem_calls == 1 else 104.0
        return 100.0

    monkeypatch.setattr(secret_guard.time, "monotonic", fake_monotonic)
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert "decision" not in parsed
    assert "not fully scanned" in parsed["systemMessage"]


def test_stop_9000_char_repeated_letter_private_key_behind_40_and_70_blocks_blocks():
    pk_block = (
        "-----BEGIN " + "PRIVATE KEY-----\n"
        + ("A" * 64 + "\n") * 140
        + "-----END " + "PRIVATE KEY-----\n"
    )
    assert len(pk_block) > 9_000
    for n_blocks in (40, 70):
        prefix = "".join(
            f"-----BEGIN NOTE {i}-----\nplain words about server logs\n-----END NOTE {i}-----\n"
            for i in range(n_blocks)
        )
        rc, out, err, took = _run_cli(prefix + pk_block, timeout=5.0)
        assert rc == 0, n_blocks
        assert took < 2.0, (n_blocks, took)
        assert err == "", n_blocks
        parsed = json.loads(out)
        assert parsed["decision"] == "block", n_blocks
        assert "PEM private key" in parsed["reason"], n_blocks

    mismatch = (
        "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        + "plain words\n"
        + "-----END CERTIFICATE-----\n"
    )
    assert _detect_classes(mismatch) == ([], True)


def test_c3_carry_tail_token_snap_prevents_splitting_id_into_false_block(capsys):
    tok = "msg_01" + "Zx9Qw8" + "Er7Ty6" + "Ui5Op4" + "As3Df2" + "Gh1JkL" + "m3Np5Qr"
    assert _chunk_classes(tok[6:]) == ["high-entropy secret"]
    assert _chunk_classes(tok) == []

    line_prefix = "12:00 INFO worker ok check\n"
    prefix = line_prefix * (1380 // len(line_prefix))
    while len(prefix) < 1379:
        prefix += "a"
    prefix += " "
    assert len(prefix) == 1380

    # Target len(buf) == 1898 so len(buf) - 512 == 1386 == len(prefix) + len("msg_01").
    rem = 1898 - len(prefix) - len(tok) - 2
    tail = ("word " * (rem // 5 + 2))[:rem]
    buf_part = prefix + tok + " " + tail + "\n"
    assert len(buf_part) == 1898
    assert len(buf_part) <= _CHUNK_CHARS

    next_line = ("plain words " * 26)[:300] + "\n"
    assert len(buf_part) + len(next_line) > _CHUNK_CHARS
    prompt = buf_part + next_line

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    assert err == ""
    assert out == ""


def test_p2_pem_cap_8192_keeps_6400_char_block_whole(capsys, monkeypatch):
    pem_6400 = (
        "-----BEGIN CUSTOMPEM-----\n"
        + ("B" * 64 + "\n") * 98
        + "-----END CUSTOMPEM-----\n"
    )
    assert 4096 < len(pem_6400) <= _MAX_PEM_CHARS
    prompt = ("12:00 INFO log ok\n" * 40) + pem_6400 + ("12:01 INFO tail ok\n" * 10)
    chunks = list(_iter_chunks(prompt))
    assert pem_6400 in chunks

    monkeypatch.setenv(
        "SWARPH_REDACT_CUSTOM_RULES",
        json.dumps([{
            "name": "CUSTPEM",
            "pattern": r"-----BEGIN CUSTOMPEM-----[\s\S]{6000,}?-----END CUSTOMPEM-----",
        }]),
    )
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "custom-rule credential" in parsed["reason"]


def test_p4_nested_begin_skipped_so_inner_pem_is_kept_whole(capsys, monkeypatch):
    outer = "-----BEGIN NOTES-----\n" + ("plain words line\n" * 320)
    inner = (
        "-----BEGIN CUSTOMPEM-----\n"
        + ("B" * 64 + "\n") * 50
        + "-----END CUSTOMPEM-----\n"
    )
    assert len(outer) + len(inner) > _MAX_PEM_CHARS
    assert len(inner) <= _MAX_PEM_CHARS
    prompt = outer + inner
    chunks = list(_iter_chunks(prompt))
    assert inner in chunks

    monkeypatch.setenv(
        "SWARPH_REDACT_CUSTOM_RULES",
        json.dumps([{
            "name": "CUSTPEM",
            "pattern": r"-----BEGIN CUSTOMPEM-----[\s\S]{3000,}?-----END CUSTOMPEM-----",
        }]),
    )
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "custom-rule credential" in parsed["reason"]


def test_c2_carry_after_sliced_long_line_catches_next_line_value(capsys):
    long_line = ("word " * 518) + "pass" + "word:"
    assert len(long_line) > _CHUNK_CHARS
    secret_val = "p@ss" + "w0rdX"
    prompt = long_line + "\n  " + secret_val + "\n"
    assert _chunk_classes("  " + secret_val) == []

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "DSN credential" in parsed["reason"]
    _assert_no_secret_echo(secret_val, out, err)


# --- #1678 R1-R4 fast in-process pins (mutants_5785 N1-N12, R2-R4) ---------


def test_1678_r1_stray_marker_bookkeeping_n1_n2_n3_n4_pins(capsys):
    b_pk = "-----BEGIN " + "PRIVATE KEY-----"
    e_pk = "-----END " + "PRIVATE KEY-----"
    b_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----"
    e_rsa = "-----END " + "RSA PRIVATE KEY-----"
    words = "plain words about a server rack and its logs \n"
    big = ("A" * 64 + "\n") * 140

    layouts = [
        # N1 & N2: stray END, then 9,100-char block, then stray BEGIN
        e_pk + "\n" + (words * 5) + b_pk + "\n" + big + e_pk + "\n" + (words * 5) + b_pk + "\n",
        # N3: 9,100-char block, words, then stray BEGIN RSA PRIVATE KEY
        b_pk + "\n" + big + e_pk + "\n" + (words * 40) + b_rsa + "\n",
        # N4: stray END RSA PRIVATE KEY, words, then 9,100-char block
        e_rsa + "\n" + (words * 40) + b_pk + "\n" + big + e_pk + "\n",
    ]
    for prompt in layouts:
        rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
        assert rc == 0
        assert err == ""
        parsed = json.loads(out)
        assert parsed["decision"] == "block"
        assert "PEM private key" in parsed["reason"]


def test_1678_r1_prepass_budget_pins_n5_n6_n7(capsys, monkeypatch):
    # N5: marker loop budget check at step 256 before min(first_begin) >= max(last_end)
    prompt_n5 = "-----END A-----\n" + ("-----BEGIN A-----\n" * 300)
    calls_n5 = 0

    def mono_n5() -> float:
        nonlocal calls_n5
        if sys._getframe(1).f_code.co_name == "_pem_envelope_classes":
            calls_n5 += 1
            return 100.0 if calls_n5 == 1 else 104.0
        return 100.0

    monkeypatch.setattr(secret_guard.time, "monotonic", mono_n5)
    assert secret_guard._pem_envelope_classes(prompt_n5, 103.0) == ([], False)

    # N6 & N7: budget expiry inside _flush_batch on <=2,048-char prompt
    prompt_n6 = "-----BEGIN NOTE-----\n-----END NOTE-----\n"
    flush_calls = 0
    orig_classes_of = secret_guard._classes_of
    batch_classes_calls = 0

    def spy_classes_of(text: str) -> list[str]:
        nonlocal batch_classes_calls
        if "NOTE" in text:
            batch_classes_calls += 1
        return orig_classes_of(text)

    def mono_n6() -> float:
        nonlocal flush_calls
        caller = sys._getframe(1).f_code.co_name
        if caller == "_flush_batch":
            flush_calls += 1
            return 104.0
        return 100.0

    monkeypatch.setattr(secret_guard, "_classes_of", spy_classes_of)
    monkeypatch.setattr(secret_guard.time, "monotonic", mono_n6)
    assert secret_guard._pem_envelope_classes(prompt_n6, 103.0) == ([], False)
    assert flush_calls == 1
    assert batch_classes_calls == 0

    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt_n6}, capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert "decision" not in parsed
    assert "not fully scanned" in parsed["systemMessage"]


def test_1678_r1_chunk_streaming_and_pem_carry_pins_n9_n10_n11_n12(capsys, monkeypatch):
    # N9: dirty=False carry from sliced long line before PEM block must NOT glue across PEM
    long_line = ("word " * 450) + "pass" + "word:"
    assert len(long_line) > _CHUNK_CHARS
    pem_note = "-----BEGIN NOTE-----\nplain words\n-----END NOTE-----\n"
    secret_val = "p@ss" + "w0rdX"
    prompt_n9 = long_line + "\n" + pem_note + "  " + secret_val + "\n"
    assert _chunk_classes("  " + secret_val) == []
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt_n9}, capsys)
    assert rc == 0
    assert out == ""

    # N11: streamed plain text cuts at '\n', never mid-line at the marker
    text_n11 = ("word " * 500) + "\npartial_before_begin-----BEGIN A-----\nrest\n"
    units_n11 = list(secret_guard._atomic_units(text_n11))
    assert any(u == "partial_before_begin-----BEGIN A-----\n" for _, u in units_n11)

    # N10 & N12: _atomic_units streams before subsequent markers are pulled
    orig_scan = secret_guard._PEM_MARKER_SCAN_RE

    class _CountingScan:
        def __init__(self, limit: int) -> None:
            self.limit = limit
            self.seen = 0

        def finditer(self, s: str):
            for m in orig_scan.finditer(s):
                self.seen += 1
                if self.seen > self.limit:
                    raise AssertionError(f"pulled marker #{self.seen} before streaming")
                yield m

    text_n10 = ("x" * 3000) + "\n-----BEGIN A-----\n" + ("y" * 3000) + "\n-----BEGIN B-----\n"
    monkeypatch.setattr(secret_guard, "_PEM_MARKER_SCAN_RE", _CountingScan(1))
    first_n10 = next(_iter_chunks(text_n10))
    assert len(first_n10) == _CHUNK_CHARS

    text_n12 = (
        "-----BEGIN NOTES-----\n"
        + ("word " * 1900)
        + "\n-----END NOTES-----\n"
        + ("word " * 500)
        + "\n-----BEGIN X-----\n"
    )
    monkeypatch.setattr(secret_guard, "_PEM_MARKER_SCAN_RE", _CountingScan(2))
    first_n12 = next(_iter_chunks(text_n12))
    assert len(first_n12) <= _CHUNK_CHARS


def test_1678_r3_channel_envelope_bounded_in_process(capsys):
    secret = _sk_ant()
    open_heavy = "<channel " + ("source=x " * 5_000) + ">\n" + ("plain words " * 2_000) + f"\ntoken {secret}\n"
    t0 = time.monotonic()
    rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": open_heavy}, capsys)
    took = time.monotonic() - t0
    assert rc == 0
    assert took < 1.0
    assert json.loads(out)["decision"] == "block"


def test_1678_r4_crafted_marker_layouts_in_process(capsys):
    big = ("A" * 64 + "\n") * 140
    b_pk = "-----BEGIN " + "PRIVATE KEY-----"
    e_pk = "-----END " + "PRIVATE KEY-----"
    long_lbl = ("RSA " * 272).strip() + " PRIVATE KEY"
    layouts = [
        "-----END CERTIFICATE-----BEGIN " + "PRIVATE KEY-----\n" + big + e_pk + "\n",
        "-----BEGIN NOTES -----BEGIN " + "PRIVATE KEY-----\n" + big + e_pk + "\n",
        b_pk + "\n" + big + "-----BEGIN X-----END " + "PRIVATE KEY-----\n",
        f"-----BEGIN {long_lbl}-----\n" + big + f"-----END {long_lbl}-----\n",
    ]
    for prompt in layouts:
        rc, out, err = _run({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, capsys)
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["decision"] == "block"
        assert "PEM private key" in parsed["reason"]
