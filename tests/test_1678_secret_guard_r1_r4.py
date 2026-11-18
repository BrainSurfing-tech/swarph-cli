"""Card #1678 / Obligation #5819: R1-R4 secret-guard pin tests.

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
    _CHUNK_CHARS,
    _atomic_units,
    _detect_classes,
    _iter_chunks,
    _pem_envelope_classes,
    run_hook,
)

_REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_UPNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_WORDS = "plain words about a server rack and its logs "


def _b36(i: int, width: int = 5) -> str:
    out = ""
    for _ in range(width):
        out = _UPNUM[i % 36] + out
        i //= 36
    return out


def _sk_ant(seed: int = 5819) -> str:
    rng = random.Random(seed)
    return "sk-" + "ant-" + "api03-" + "".join(rng.choice(_ALNUM) for _ in range(95))


def _assert_no_secret_echo(secret: str, *texts: str) -> None:
    blob = "".join(texts)
    for i in range(len(secret) - 3):
        window = secret[i:i + 4]
        assert window not in blob, f"secret window leaked: {window!r}"


def _run_inproc(prompt: str, capsys) -> tuple[int, str, str]:
    rc = run_hook(stdin_text=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}))
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


def _filler(n: int) -> str:
    return (_WORDS * (n // len(_WORDS) + 1))[:n]


# --- R1: stray-marker bookkeeping, batch budget checks, and chunk pins -----


def test_r1_stray_marker_bookkeeping_n1_to_n4_inproc_and_child(capsys):
    b_pk = "-----BEGIN " + "PRIVATE KEY-----"
    e_pk = "-----END " + "PRIVATE KEY-----"
    b_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----"
    e_rsa = "-----END " + "RSA PRIVATE KEY-----"
    big = ("A" * 64 + "\n") * 140
    assert len(big) == 9_100
    words_5 = (_WORDS + "\n") * 5
    words_40 = (_WORDS + "\n") * 40

    layouts = {
        "n1_n2_stray_end_then_block_then_stray_begin": (
            e_pk + "\n" + words_5 + b_pk + "\n" + big + e_pk + "\n" + words_5 + b_pk + "\n"
        ),
        "n3_block_then_stray_begin_rsa": (
            b_pk + "\n" + big + e_pk + "\n" + words_40 + b_rsa + "\n"
        ),
        "n4_stray_end_rsa_then_block": (
            e_rsa + "\n" + words_40 + b_pk + "\n" + big + e_pk + "\n"
        ),
    }

    for name, prompt in layouts.items():
        rc, out, err = _run_inproc(prompt, capsys)
        assert rc == 0, name
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "PEM private key" in parsed["reason"], name

        rc, out, err, took = _run_cli(prompt, timeout=5.0)
        assert rc == 0, name
        assert took < 5.0, (name, took)
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "PEM private key" in parsed["reason"], name


def test_r1_batch_budget_check_n6_single_and_multi_batch(monkeypatch):
    # Single batch: deadline crossed right after marker-scan loop, before _flush_batch().
    prompt_single = "-----BEGIN NOTE-----\n-----END NOTE-----\n"
    orig_classes_of = secret_guard._classes_of
    batch_calls = 0

    def spy_classes_of(text: str) -> list[str]:
        nonlocal batch_calls
        if "NOTE" in text:
            batch_calls += 1
        return orig_classes_of(text)

    def mono_single() -> float:
        if sys._getframe(1).f_code.co_name == "_flush_batch":
            return 104.0
        return 100.0

    monkeypatch.setattr(secret_guard, "_classes_of", spy_classes_of)
    monkeypatch.setattr(secret_guard.time, "monotonic", mono_single)
    assert _pem_envelope_classes(prompt_single, 103.0) == ([], False)
    assert batch_calls == 0

    # Multi-batch (55 distinct NOTE labels -> 2 batches): batch 1 runs, deadline expires before batch 2.
    prompt_multi = "".join(
        f"-----BEGIN NOTE {_b36(i)}-----\n-----END NOTE {_b36(i)}-----\n"
        for i in range(55)
    )
    batch_calls = 0
    flush_checks = 0

    def mono_multi() -> float:
        nonlocal flush_checks
        if sys._getframe(1).f_code.co_name == "_flush_batch":
            flush_checks += 1
            return 100.0 if flush_checks == 1 else 104.0
        return 100.0

    monkeypatch.setattr(secret_guard.time, "monotonic", mono_multi)
    assert _pem_envelope_classes(prompt_multi, 103.0) == ([], False)
    assert flush_checks == 2
    assert batch_calls == 1


def test_r1_marker_loop_and_single_chunk_incomplete_pins_n5_n7(capsys, monkeypatch):
    # N5: marker loop checks deadline at step 256 (even when min(first_begin) >= max(last_end)).
    prompt_n5 = "-----END A-----\n" + ("-----BEGIN A-----\n" * 300)
    pem_calls = 0

    def mono_n5() -> float:
        nonlocal pem_calls
        if sys._getframe(1).f_code.co_name == "_pem_envelope_classes":
            pem_calls += 1
            # 1st call: step 0; 2nd call: step 256 -> expire at step 256.
            return 100.0 if pem_calls == 1 else 104.0
        return 100.0

    monkeypatch.setattr(secret_guard.time, "monotonic", mono_n5)
    assert _pem_envelope_classes(prompt_n5, 103.0) == ([], False)
    assert pem_calls == 2

    # N7: <=2,048-char prompt where _pem_envelope_classes returns complete=False must emit systemMessage.
    prompt_n7 = "-----BEGIN NOTE-----\n-----END NOTE-----\n"
    assert len(prompt_n7) <= _CHUNK_CHARS
    monkeypatch.setattr(
        secret_guard.time,
        "monotonic",
        lambda: 104.0 if sys._getframe(1).f_code.co_name == "_flush_batch" else 100.0,
    )
    assert _detect_classes(prompt_n7) == ([], False)
    rc, out, err = _run_inproc(prompt_n7, capsys)
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert "decision" not in parsed
    assert "not fully scanned" in parsed["systemMessage"]


def test_r1_chunk_streaming_and_pem_carry_pins_n9_n10_n11_n12(capsys, monkeypatch):
    # N9: dirty=False carry from sliced long line before PEM block must not glue across PEM.
    long_line = ("word " * 450) + "pass" + "word:"
    assert len(long_line) > _CHUNK_CHARS
    pem_note = "-----BEGIN NOTE-----\nplain words\n-----END NOTE-----\n"
    short_pw = "p@ss" + "w0rdX"
    prompt_n9 = long_line + "\n" + pem_note + "  " + short_pw + "\n"
    rc, out, err = _run_inproc(prompt_n9, capsys)
    assert rc == 0
    assert out == ""
    chunks_n9 = list(_iter_chunks(prompt_n9))
    assert not any(("pass" + "word:" in c and short_pw in c) for c in chunks_n9)

    # N11: streamed plain text cuts at '\n', never mid-line at a marker.
    text_n11 = ("word " * 500) + "\npartial_before_begin-----BEGIN A-----\nrest\n"
    units_n11 = list(_atomic_units(text_n11))
    assert any(u == "partial_before_begin-----BEGIN A-----\n" for _, u in units_n11)

    # N10 & N12: _atomic_units streams incrementally before later markers are pulled.
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
    assert len(next(_iter_chunks(text_n10))) == _CHUNK_CHARS

    text_n12 = (
        "-----BEGIN NOTES-----\n"
        + ("word " * 1900)
        + "\n-----END NOTES-----\n"
        + ("word " * 500)
        + "\n-----BEGIN X-----\n"
    )
    monkeypatch.setattr(secret_guard, "_PEM_MARKER_SCAN_RE", _CountingScan(2))
    assert len(next(_iter_chunks(text_n12))) <= _CHUNK_CHARS


# --- R2: first-line key beside 143,479 distinct labels blocks under 1 s -----


def test_r2_143479_distinct_labels_3300010_chars_first_line_key_blocks_under_1s():
    key = _sk_ant(58192)
    marker_body = "".join(f"-----BEGIN L{_b36(i)}-----\n" for i in range(143_478)) + "-----END Z-----\n"
    assert len(marker_body) == 3_300_010
    assert marker_body.count("-----\n") == 143_479

    prompt = f"token {key}\n{marker_body}"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R2 143,479-label child process took {took:.3f}s (>= 1.0s)"
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "Anthropic" in parsed["reason"]
    _assert_no_secret_echo(key, out, err)


def test_r2_symmetric_70000_matched_pairs_first_line_key_blocks_under_1s():
    key = _sk_ant(58193)
    pairs = "".join(
        f"-----BEGIN L{_b36(i)}-----\n-----END L{_b36(i)}-----\n"
        for i in range(70_000)
    )
    prompt = f"token {key}\n{pairs}"
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R2 symmetric 70k pairs child process took {took:.3f}s (>= 1.0s)"
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "Anthropic" in parsed["reason"]
    _assert_no_secret_echo(key, out, err)


# --- R3: channel opening tag with 5,000 repeated source= attrs under 1 s -----


def test_r3_channel_tag_repeated_source_attrs_and_unclosed_under_1s():
    key = _sk_ant(58194)
    words_200k = _filler(200_000)
    open_5000 = "<channel " + ("source=x " * 5_000) + ">\n" + words_200k

    # With secret on last line: must block in < 1.0s.
    rc, out, err, took = _run_cli(f"{open_5000}\ntoken {key}\n", timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R3 5,000 source= (with key) took {took:.3f}s (>= 1.0s)"
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "Anthropic" in parsed["reason"]
    _assert_no_secret_echo(key, out, err)

    # Benign no-key variant: must exit 0 cleanly with empty stdout in < 1.0s.
    rc, out, err, took = _run_cli(f"{open_5000}\n", timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R3 5,000 source= (benign) took {took:.3f}s (>= 1.0s)"
    assert out == ""
    assert err == ""

    # Unclosed 50,000 source= without '>'
    unclosed_50k = "<channel " + ("source=x " * 50_000)
    rc, out, err, took = _run_cli(f"{unclosed_50k}\ntoken {key}\n", timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R3 50,000 source= unclosed (with key) took {took:.3f}s (>= 1.0s)"
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    _assert_no_secret_echo(key, out, err)

    rc, out, err, took = _run_cli(unclosed_50k, timeout=5.0)
    assert rc == 0
    assert took < 1.0, f"R3 50,000 source= unclosed (benign) took {took:.3f}s (>= 1.0s)"
    assert out == ""
    assert err == ""


def test_r3_legitimate_channel_harness_turn_still_exempt():
    key = _sk_ant(58195)
    prompt = f'<channel source="plugin:swarph:swarph" from_node="peer" id="1">\n{key}\n</channel>'
    rc, out, err, took = _run_cli(prompt, timeout=5.0)
    assert rc == 0
    assert took < 1.0
    assert out == ""
    assert err == ""


# --- R4: four crafted marker layouts block as child processes ---------------


def test_r4_four_crafted_marker_layouts_block_as_child_process():
    big_9100 = ("A" * 64 + "\n") * 140
    mid_2990 = ("A" * 64 + "\n") * 46
    assert len(big_9100) == 9_100
    assert len(mid_2990) == 2_990

    b_pk = "-----BEGIN " + "PRIVATE KEY-----"
    e_pk = "-----END " + "PRIVATE KEY-----"
    long_label = ("RSA " * 272).strip() + " PRIVATE KEY"
    assert len(long_label) == 1_099

    layouts = {
        "1_end_cert_and_begin_pk_share_five_dashes_9100": (
            "-----END CERTIFICATE-----BEGIN " + "PRIVATE KEY-----\n" + big_9100 + e_pk + "\n"
        ),
        "2_begin_notes_space_and_begin_pk_share_five_dashes_9100": (
            "-----BEGIN NOTES -----BEGIN " + "PRIVATE KEY-----\n" + big_9100 + e_pk + "\n"
        ),
        "3_begin_x_and_end_pk_share_five_dashes_9100": (
            b_pk + "\n" + big_9100 + "-----BEGIN X-----END " + "PRIVATE KEY-----\n"
        ),
        "4a_1099_char_pk_label_body_9100": (
            f"-----BEGIN {long_label}-----\n" + big_9100 + f"-----END {long_label}-----\n"
        ),
        "4b_1099_char_pk_label_body_2990": (
            f"-----BEGIN {long_label}-----\n" + mid_2990 + f"-----END {long_label}-----\n"
        ),
    }

    for name, prompt in layouts.items():
        rc, out, err, took = _run_cli(prompt, timeout=5.0)
        assert rc == 0, name
        assert took < 5.0, (name, took)
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "PEM private key" in parsed["reason"], name
