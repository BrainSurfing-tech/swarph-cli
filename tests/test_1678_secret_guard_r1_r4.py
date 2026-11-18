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


# --- #5866 / #5867: gate (no 32-batch early stop) and budget (chunks first) ---


def test_5866_gate_all_earlystop_layouts_and_controls_block_as_child_process():
    body = ("A" * 64 + "\n") * 140
    assert len(body) == 9_100
    b_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
    e_rsa = "-----END " + "RSA PRIVATE KEY-----\n"
    b_ec = "-----BEGIN " + "EC PRIVATE KEY-----\n"
    e_ec = "-----END " + "EC PRIVATE KEY-----\n"
    e_openssh = "-----END " + "OPENSSH PRIVATE KEY-----\n"
    block = b_ec + body + e_ec

    def long_label(i: int) -> str:
        return ("NOTE " * 197).strip() + " " + _b36(i)

    def junk_begin_long(n: int) -> str:
        return "".join(f"-----BEGIN {long_label(i)}-----\n" for i in range(n))

    def junk_end_long(n: int) -> str:
        return "".join(f"-----END {long_label(i)}-----\n" for i in range(n))

    def junk_begin_short(n: int) -> str:
        return "".join(f"-----BEGIN N{_b36(i)}-----\n" for i in range(n))

    def junk_end_short(n: int) -> str:
        return "".join(f"-----END E{_b36(i)}-----\n" for i in range(n))

    layouts = {
        "0_control_block_alone": _WORDS + "\n" + block,
        "1_stray_rsa_begin_40_long_begins_then_block": (
            junk_end_short(45) + b_rsa + junk_begin_long(40) + block
        ),
        "1a_control_no_stray_begin": junk_end_short(45) + junk_begin_long(40) + block,
        "1b_control_10_long_begins": junk_end_short(15) + b_rsa + junk_begin_long(10) + block,
        "1c_control_stray_own_label": junk_end_short(45) + b_ec + junk_begin_long(40) + block,
        "1d_stray_rsa_begin_2000_short_begins_then_block": (
            junk_end_short(2005) + b_rsa + junk_begin_short(2000) + block
        ),
        "2_block_then_40_long_ends_then_stray_rsa_end": (
            junk_begin_short(45) + block + junk_end_long(40) + e_rsa
        ),
        "2a_control_no_stray_end": junk_begin_short(45) + block + junk_end_long(40),
        "2b_control_10_long_ends": junk_begin_short(15) + block + junk_end_long(10) + e_rsa,
        "3_words_between_every_marker_line": (
            junk_end_short(45).replace("\n", "\n" + _WORDS + "\n")
            + b_rsa
            + _WORDS
            + "\n"
            + junk_begin_long(40).replace("\n", "\n" + _WORDS + "\n")
            + block
        ),
        "symmetric_hybrid_both_sides_stray_and_40_long_labels": (
            b_rsa + junk_begin_long(40) + block + junk_end_long(40) + e_openssh
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


def test_5866_budget_300_distinct_4070_char_curl_u_labels_last_line_key_blocks_as_child_process():
    key = _sk_ant(58661)
    unit = "CURL U "
    n = 4070
    prefix_4064 = (unit * ((n - 6) // len(unit) + 1))[: n - 6].rstrip()
    labels = [f"{prefix_4064} {_b36(i)}" for i in range(300)]
    assert all(len(lbl) == 4070 for lbl in labels)

    body = (
        "".join(f"-----BEGIN {lbl}-----\n" for lbl in labels)
        + "".join(f"-----END {lbl}-----\n" for lbl in labels)
    )
    prompt = body + f"token {key}\n"
    assert len(prompt) == 2_451_715

    rc, out, err, took = _run_cli(prompt, timeout=10.0)
    assert rc == 0
    assert took < 5.0, f"budget 300x4070 CURL U last-line key took {took:.3f}s (>= 5.0s)"
    assert err == ""
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "Anthropic" in parsed["reason"]
    _assert_no_secret_echo(key, out, err)


# --- #5885 / #5892: G1 (pre-pass after first chunk) & G2 (alphabetical traps) --


def _b36_4(i: int) -> str:
    out = ""
    while True:
        out = _UPNUM[i % 36] + out
        i //= 36
        if not i:
            return out.rjust(4, "0")


def _alpha_trap_layouts() -> tuple[str, str, str, list[str]]:
    body = ("A" * 64 + "\n") * 140
    b_dsa = "-----BEGIN " + "DSA PRIVATE KEY-----\n"
    e_dsa = "-----END " + "DSA PRIVATE KEY-----\n"
    b_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
    e_rsa = "-----END " + "RSA PRIVATE KEY-----\n"
    long_labels = [("NOTE " * 197).strip() + " " + _b36_4(i) for i in range(40)]
    all_labels = ["DSA PRIVATE KEY"] + long_labels + ["RSA PRIVATE KEY"]
    layout_a = (
        "".join(f"-----END E{_b36_4(i)}-----\n" for i in range(45))
        + b_dsa
        + "".join(f"-----BEGIN {lbl}-----\n" for lbl in long_labels)
        + b_rsa
        + body
        + e_rsa
    )
    layout_b = (
        "".join(f"-----BEGIN N{_b36_4(i)}-----\n" for i in range(45))
        + b_rsa
        + body
        + e_rsa
        + "".join(f"-----END {lbl}-----\n" for lbl in long_labels)
        + e_dsa
    )
    layout_c = (
        "".join(f"-----END E{_b36_4(i)}-----\n" for i in range(45))
        + "".join(f"-----BEGIN {lbl}-----\n" for lbl in long_labels)
        + b_rsa
        + body
        + e_rsa
    )
    return layout_a, layout_b, layout_c, all_labels


def test_5885_g1_order_layouts_and_near_limit_cases_block_under_1s_as_child_process():
    def lines(unit: str, n: int, width: int = 8000) -> str:
        line = (unit * (width // len(unit) + 1))[:width]
        return (line + "\n") * (n // (width + 1))

    def pk_block(body: int) -> str:
        return (
            "-----BEGIN " + "PRIVATE KEY-----\n"
            + ("A" * 64 + "\n") * (body // 65)
            + "-----END " + "PRIVATE KEY-----\n"
        )

    flood_unit = "-eyJ"
    words_unit = "the quick brown fox reads logs from server rack nine "

    cases: list[tuple[str, str]] = []
    for n, expected_len in ((1_200_000, 1_192_149), (1_600_000, 1_592_199), (2_400_000, 2_392_299)):
        fill = lines(flood_unit, n)
        assert len(fill) == expected_len
        cases.append((f"flood_{expected_len}_then_pk9100", fill + pk_block(9100)))
        cases.append((f"pk9100_then_flood_{expected_len}", pk_block(9100) + fill))
    cases.append(("flood_1592199_then_pk2990", lines(flood_unit, 1_600_000) + pk_block(2990)))

    for n, expected_len in (
        (4_000_000, 3_992_499),
        (4_500_000, 4_496_562),
        (5_000_000, 4_992_624),
        (6_000_000, 5_992_749),
    ):
        fill = lines(words_unit, n)
        assert len(fill) == expected_len
        cases.append((f"words_{expected_len}_then_pk9100", fill + pk_block(9100)))
        cases.append((f"pk9100_then_words_{expected_len}", pk_block(9100) + fill))
    fill_words_5m = lines(words_unit, 5_000_000)
    cases.append(("words_4992624_then_pk2990", fill_words_5m + pk_block(2990)))
    cases.append(("pk2990_then_words_4992624", pk_block(2990) + fill_words_5m))

    for name, prompt in cases:
        rc, out, err, took = _run_cli(prompt, timeout=5.0)
        assert rc == 0, name
        assert took < 1.0, f"G1 {name} took {took:.3f}s (>= 1.0s)"
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "PEM private key" in parsed["reason"], name


def test_5885_g2_alphabetical_trap_layouts_block_as_child_process():
    layout_a, layout_b, layout_c, _ = _alpha_trap_layouts()
    for name, prompt in (
        ("A_45_end_stray_dsa_begin_40_long_begins_rsa_block", layout_a),
        ("B_mirror_45_begin_rsa_block_40_long_ends_stray_dsa_end", layout_b),
        ("C_control_no_stray_dsa", layout_c),
    ):
        rc, out, err, took = _run_cli(prompt, timeout=5.0)
        assert rc == 0, name
        assert took < 1.0, f"G2 {name} took {took:.3f}s (>= 1.0s)"
        assert err == "", name
        parsed = json.loads(out)
        assert parsed["decision"] == "block", name
        assert "PEM private key" in parsed["reason"], name


def test_5885_g2_prepass_never_leaves_first_side_label_unclassified(monkeypatch):
    layout_a, layout_b, _, all_labels = _alpha_trap_layouts()
    assert len(all_labels) == 42

    orig_classes_of = secret_guard._classes_of
    seen_texts: list[str] = []

    def spy_classes_of(text: str) -> list[str]:
        seen_texts.append(text)
        return orig_classes_of(text)

    monkeypatch.setattr(secret_guard, "_classes_of", spy_classes_of)

    for name, prompt in (("layout_a", layout_a), ("layout_b", layout_b)):
        seen_texts.clear()
        state: dict = {}
        classes, complete = _pem_envelope_classes(prompt, time.monotonic() + 60.0, _state=state)
        assert (classes, complete) == (["PEM private key"], True), name
        combined_seen = "\n".join(seen_texts)
        for lbl in all_labels:
            assert lbl in combined_seen, f"{name} never sent {lbl!r} to _classes_of"
            assert lbl in state["classified"], f"{name} left {lbl!r} unclassified"


# --- #5926 (plan-review #5919 G1 + R): main's label set runs BEFORE the chunks --


def _prepass_5919_layout(k: int, long: bool, words: int) -> str:
    """drop's prepass_5919.py layout: K distinct labels on BEGIN and END
    lines (4,065 chars each when ``long``), ``words`` chars of 8,000-char
    word lines, then a 9,100-char private-key block LAST."""

    def lab(i: int) -> str:
        return (("NOTE " * 812).strip() + " " + _b36(i)) if long else "N" + _b36(i)

    line = (("the quick brown fox reads logs from server rack nine " * 160)[:8000]) + "\n"
    return (
        "".join(f"-----BEGIN {lab(i)}-----\n" for i in range(k))
        + "".join(f"-----END {lab(i)}-----\n" for i in range(k))
        + line * (words // len(line))
        + "-----BEGIN " + "RSA PRIVATE KEY-----\n" + ("A" * 64 + "\n") * 140
        + "-----END " + "RSA PRIVATE KEY-----\n"
    )


def _deferred_only_layouts() -> dict[str, str]:
    """A 1,099-char private-key label (R4 layout 4a: pair 2,229 > _CHUNK_CHARS,
    so main never classifies it) around a 9,100-char body LAST, behind words
    and, in one layout, three 4,065-char non-key labels. Phase A defers every
    label here, the chunks slice the body, so only Phase B can block."""
    pk_label = ("RSA " * 272).strip() + " PRIVATE KEY"
    assert len(pk_label) == 1_099
    body = ("A" * 64 + "\n") * 140
    block = f"-----BEGIN {pk_label}-----\n" + body + f"-----END {pk_label}-----\n"
    words = (_WORDS + "\n") * 4_000
    junk = [("NOTE " * 812).strip() + " " + _b36(i) for i in range(3)]
    return {
        "4a_block_last_after_200k_words": words + block,
        "4a_block_last_after_three_long_labels_and_200k_words": (
            "".join(f"-----BEGIN {lbl}-----\n" for lbl in junk)
            + "".join(f"-----END {lbl}-----\n" for lbl in junk)
            + words
            + block
        ),
    }


def test_5926_g1_prepass_5919_label_floods_block_last_as_child_process():
    rows = [
        ("200_long_labels_2m_words", 200, True, 2_000_000, 2.0),
        ("300_long_labels_1m_words", 300, True, 1_000_000, 2.0),
        ("600_long_labels_no_words", 600, True, 0, 2.0),
        # main itself needs ~1.6 s here: a block proves the 3 s budget held.
        ("60000_short_labels_no_words", 60_000, False, 0, 5.0),
    ]
    for name, k, long, words, cap in rows:
        prompt = _prepass_5919_layout(k, long, words)
        rc, out, err, took = _run_cli(prompt, timeout=10.0)
        assert rc == 0, name
        assert err == "", name
        assert out.strip(), f"{name}: hook allowed silently (no output)"
        parsed = json.loads(out)
        assert parsed.get("decision") == "block", (name, out[:120])
        assert "PEM private key" in parsed["reason"], name
        assert took < cap, f"{name} took {took:.3f}s (>= {cap}s)"


def test_5926_r_phase_b_resume_blocks_deferred_pk_label_last_as_child_process():
    # Red when the Phase B resume call in _detect_classes is removed.
    for name, prompt in _deferred_only_layouts().items():
        rc, out, err, took = _run_cli(prompt, timeout=10.0)
        assert rc == 0, name
        assert err == "", name
        assert out.strip(), f"{name}: hook allowed silently (no output)"
        parsed = json.loads(out)
        assert parsed.get("decision") == "block", (name, out[:120])
        assert "PEM private key" in parsed["reason"], name
        assert took < 3.0, f"{name} took {took:.3f}s (>= 3.0s)"


def test_5926_phase_a_is_mains_label_set_and_phase_b_matches_one_call(monkeypatch):
    far = time.monotonic() + 600.0
    body = ("A" * 64 + "\n") * 140
    short_pk = ("-----BEGIN " + "EC PRIVATE KEY-----\n" + body + "-----END " + "EC PRIVATE KEY-----\n")
    long_990 = [("NOTE " * 197).strip() + " " + _b36(i) for i in range(40)]
    layouts = dict(_deferred_only_layouts())
    layouts["prepass_5919_5_long_labels_then_block"] = _prepass_5919_layout(5, True, 0)
    layouts["prepass_5919_300_short_labels_then_block"] = _prepass_5919_layout(300, False, 0)
    layouts["alpha_trap_a"], layouts["alpha_trap_b"], _, _ = (*_alpha_trap_layouts(),)
    layouts["earlystop_1_stray_pk_begin_40_990_labels"] = (
        "".join(f"-----END E{_b36(i)}-----\n" for i in range(45))
        + "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        + "".join(f"-----BEGIN {lbl}-----\n" for lbl in long_990)
        + short_pk
    )
    layouts["control_stray_begin_no_block"] = (
        "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        + "".join(f"-----BEGIN {lbl}-----\n" for lbl in long_990[:20])
        + "".join(f"-----END E{_b36(i)}-----\n" for i in range(30))
    )

    orig = secret_guard._classes_of
    seen: list[str] = []
    monkeypatch.setattr(secret_guard, "_classes_of", lambda t: (seen.append(t), orig(t))[1])

    deferred_somewhere = 0
    for name, text in layouts.items():
        ref = _pem_envelope_classes(text, far)
        state: dict = {}
        seen.clear()
        phase_a = _pem_envelope_classes(text, far, _defer_long=True, _state=state)
        assert phase_a[1] is True, name
        # Phase A sends redact only main's label set: nothing over one chunk.
        assert all(len(t) <= _CHUNK_CHARS for t in seen), name
        for lbl in state["deferred"]:
            pair_len = len(f"-----BEGIN {lbl}-----\n-----END {lbl}-----")
            assert _CHUNK_CHARS < pair_len <= secret_guard._MAX_PEM_CHARS, (name, pair_len)
        deferred_somewhere += bool(state["deferred"])
        if phase_a[0]:
            assert phase_a == ref, name
            continue
        phase_b = _pem_envelope_classes(text, far, _state=state) if state["deferred"] else phase_a
        assert phase_b == ref, name
    assert deferred_somewhere >= 3
