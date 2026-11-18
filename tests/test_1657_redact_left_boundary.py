"""Card #1657: provider keys glued on the left must still be flagged.

A key right after a letter, digit, underscore or a literal backslash-n /
backslash-t escape was missed because every provider pattern opened with
``\\b`` and a word character before the prefix is not a boundary. Sentinels
are assembled from fragments so no scanner-shaped literal sits in this file.
"""
import random
import string
import time

from swarph_cli import redact
from swarph_cli.commands import secret_guard


def _anthropic(rng):
    return "sk-" + "ant-" + "api03-" + "".join(
        rng.choice(string.ascii_letters + string.digits) for _ in range(95))


SHAPES = [
    ("bare", " ", " "),
    ("letters both sides", "abc", "xyz"),
    ("letters left only", "abc", " "),
    ("letters right only", " ", "xyz"),
    ("digits both sides", "123", "456"),
    ("quotes", '"', '"'),
    ("equals left", "k=", " "),
    ("underscore both sides", "_", "_"),
    ("literal backslash-n", "\\n", " "),
    ("literal backslash-t", "\\t", " "),
    ("real newline", "\n", " "),
    ("literal backslash-n then name", "\\nname=", " "),
]


def test_anthropic_glued_left_shapes_flag_40_of_40():
    rng = random.Random(1657)
    for name, left, right in SHAPES:
        for _ in range(40):
            text = "run this " + left + _anthropic(rng) + right + " please"
            classes, _complete = secret_guard._detect_classes(text)
            assert classes, (name, "guard missed a glued Anthropic key")
            assert redact.redact_text(text) != text, (name, "redact missed")


def _key(kind, rng):
    alnum = string.ascii_letters + string.digits
    if kind == "github":
        return "gh" + "p_" + "".join(rng.choice(alnum) for _ in range(36))
    if kind == "github_pat":
        return "github" + "_pat_" + "".join(rng.choice(alnum) for _ in range(40))
    if kind == "openai":
        return "sk-" + "proj-" + "".join(rng.choice(alnum) for _ in range(48))
    if kind == "xai":
        return "x" + "ai-" + "".join(rng.choice(alnum) for _ in range(40))
    if kind == "gcp":
        return "AI" + "za" + "".join(rng.choice(alnum + "-_") for _ in range(35))
    if kind == "slack":
        return "xox" + "b-" + "".join(rng.choice(alnum + "-") for _ in range(24))
    if kind == "aws":
        return "AK" + "IA" + "".join(
            rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
    if kind == "jwt":
        return ("ey" + "J" + "".join(rng.choice(alnum) for _ in range(20))
                + "." + "ey" + "J" + "".join(rng.choice(alnum) for _ in range(60))
                + "." + "".join(rng.choice(alnum) for _ in range(43)))
    raise AssertionError(kind)


PROVIDERS = ["github", "github_pat", "openai", "xai", "gcp", "slack", "aws", "jwt"]
GLUED_LEFTS = ["abc", "123", "_", "\\n", "\\t"]


def test_other_providers_glued_left_flag():
    rng = random.Random(1658)
    for kind in PROVIDERS:
        for left in GLUED_LEFTS:
            for _ in range(10):
                text = "run this " + left + _key(kind, rng) + " please"
                assert redact.redact_text(text) != text, (kind, left)


def test_false_positive_corpus_has_no_hits():
    rng = random.Random(1659)
    corpus = [
        "the quick brown fox jumps over the lazy dog while reading the server logs",
        "ask me again tomorrow and ask the desk clerk about the task list",
        "variable_name = compute_total(items, tax_rate)  # ordinary identifier text",
        "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # a git SHA (da39 + zeros shape)
        "b6b7c9b7-60f2-422d-8b78-f58ad6d26e6d",  # a UUID
        ("QUJDREVGR0hJSg==" * 30)[:400],  # base64 blob, no provider prefix
        "Bearer token goes in the Authorization header, never in the URL",
        "<system-reminder>\nordinary harness note, nothing secret\n</system-reminder>",
    ]
    # A real git SHA from this repository's own history shape: 40 lowercase hex.
    corpus.append("".join(rng.choice("0123456789abcdef") for _ in range(40)))
    for text in corpus:
        assert redact.redact_text(text) == text, text[:60]
        classes, _complete = secret_guard._detect_classes(text)
        assert not classes, text[:60]


def test_redact_stays_linear_on_a_megabyte_line():
    line = "ordinary log text with no secrets at all " * 25000  # ~1.07M chars
    assert len(line) > 1_000_000
    start = time.perf_counter()
    out = redact.redact_text(line)
    elapsed = time.perf_counter() - start
    assert out == line
    assert elapsed < 5.0, f"redact took {elapsed:.2f}s on a 1M-char line"
