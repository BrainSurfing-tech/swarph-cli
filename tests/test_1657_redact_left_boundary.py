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
# Short prefixes (bare sk-, gh+letter, xai-, xox+letter, AKIA/ASIA) sit
# inside ordinary identifiers, so a preceding LETTER guards them out
# (#1657 rework, G3) - except right after a literal backslash escape.
SHORT_PREFIX = {"github", "xai", "slack", "aws"}


_PATTERNS = {
    "github": redact._GITHUB_RE, "github_pat": redact._GITHUB_RE,
    "openai": redact._OPENAI_RE, "xai": redact._XAI_RE,
    "gcp": (redact._GCP_RE, redact._GCP_FINAL_RE),
    "slack": redact._SLACK_RE,
    "aws": redact._AWS_KEY_RE, "jwt": redact._JWT_RE,
}


def test_other_providers_glued_left_flag():
    # Pattern-level: whole-text redact also has a standalone high-entropy
    # layer that flags long random tokens regardless of provider, which
    # is correct and unchanged - it is not what this card measures.
    rng = random.Random(1658)
    for kind in PROVIDERS:
        for left in GLUED_LEFTS:
            for _ in range(10):
                text = "run this " + left + _key(kind, rng) + " please"
                pats = _PATTERNS[kind]
                pats = pats if isinstance(pats, tuple) else (pats,)
                flagged = any(p.search(text) is not None for p in pats)
                if kind in SHORT_PREFIX and left == "abc":
                    assert not flagged, (kind, left, "letter-glued short prefix")
                else:
                    assert flagged, (kind, left)


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


NEAR_MISS = [
    "task-queue_worker_heartbeat_seconds", "disk-usage_report_2026_11_18",
    "risk-adjusted_returns_2026Q3_summary",
    "git checkout feat/task-1657_redact_left_boundary_fix",
    "/var/log/disk-monitor_20261118_100000.log",
    "https://example.com/files/task-attachments_2026_11_18_final",
    "ask-me_anything_about_the_release_plan",
    "desk-booking_system_migration_plan_v2",
    "mask-rcnn_resnet50_fpn_coco_weights_final",
    "kiosk-display_controller_firmware_v12",
    "the task-0123456789abcdef0123456789abcdef row",
    "pip install flask-sqlalchemy_migrations_helper_pkg",
    "rolling_highs_window_252_trading_days",
    "weighs_and_measures_of_the_whole_fleet",
    "laughs_per_minute_metric_histogram_ms",
    "daily_highs_and_lows_by_ticker_symbol_v2",
    "task-ant-colony-optimization notes", "risk-ant-hill-simulation-run-7",
    "MALAYSIA2026Q3REVENUEUSD", "EURASIA20261118T100000Z",
    "paradoxoxb-compat-layer-2026", "relaxai-inference-endpoint-east-1",
    "a git sha 9f2c1e7ab04d5e6f7a8b9c0d1e2f3a4b5c6d7e8f",
    "uuid 123e4567-e89b-12d3-a456-426614174000",
    "base64 " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
    "ask-/desk-/task- prefixes in prose are fine",
    "see https://example.com/desk-setup/ask-the-team/task-list for more",
]


def test_bare_sk_letter_and_digit_glued_do_not_flag():
    # The bare sk- branch (no proj-/svcacct- marker) is the short prefix
    # drop's G3 guards hardest: a letter OR digit directly before it is
    # an identifier, not a glued key. The proj- form above stays unguarded.
    rng = random.Random(5864)
    body = "".join(rng.choice(string.ascii_letters + string.digits)
                   for _ in range(24))
    for left in ("abc", "123"):
        assert redact._OPENAI_RE.search(f"run {left}sk-{body} now") is None
    for left in (" ", "\\n", "\\t", "_"):
        assert redact._OPENAI_RE.search(f"run {left}sk-{body} now") is not None


def test_near_miss_lines_are_unchanged():
    for line in NEAR_MISS:
        assert redact.redact_text(line) == line, line


def _bare_sk(rng, body):
    return "sk-" + body


def test_bare_sk_shapes_main_flags_still_flag():
    rng = random.Random(5863)
    alnum = string.ascii_letters + string.digits
    def s(n, alphabet=alnum):
        return "".join(rng.choice(alphabet) for _ in range(n))
    shapes = [
        lambda: _bare_sk(rng, s(16)), lambda: _bare_sk(rng, s(19)),
        lambda: _bare_sk(rng, s(24)),
        lambda: _bare_sk(rng, s(24) + "-" + s(8)),
        lambda: _bare_sk(rng, s(12) + "-" + s(12)),
        lambda: _bare_sk(rng, "or-v1-" + "".join(
            rng.choice("0123456789abcdef") for _ in range(64))),
        lambda: _bare_sk(rng, "admin-" + s(40)),
        lambda: _bare_sk(rng, "None-" + s(40)),
    ]
    for make in shapes:
        for _ in range(40):
            text = "the key is " + make() + " today"
            assert redact.redact_text(text) != text


def test_changed_patterns_stay_linear_at_a_megabyte():
    cases = {
        "eyJ no dot": ("eyJ" * 333_334, redact._JWT_RE),
        "eyJabcd no dot": ("eyJabcd" * 142_858, redact._JWT_RE),
        "ghp_ accented end": ("ghp_" * 250_000 + "é", redact._GITHUB_RE),
    }
    for name, (line, pattern) in cases.items():
        assert len(line) >= 1_000_000, name
        start = time.perf_counter()
        pattern.sub("[REDACTED]", line)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, (name, f"{elapsed:.3f}s")


def test_redact_stays_linear_on_a_megabyte_line():
    line = "ordinary log text with no secrets at all " * 25000  # ~1.07M chars
    assert len(line) > 1_000_000
    start = time.perf_counter()
    out = redact.redact_text(line)
    elapsed = time.perf_counter() - start
    assert out == line
    assert elapsed < 5.0, f"redact took {elapsed:.2f}s on a 1M-char line"
