"""#874 — a grep over source counts what the source SAYS as what it DOES.

Every case below is a REAL specimen from 2026-09-16..18, not an invented string.
Each one produced a confident wrong number that was reported as a finding before
being caught.

Run: <venv>/python -m pytest tests/test_874_source_text.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from swarph_cli.source_text import (  # noqa: E402
    CANNOT_VERIFY, code_text, hash_comment_code_text, python_code_text,
)


# ── THE FOUR REAL SPECIMENS ──────────────────────────────────────────────────

def test_specimen_1_selector_in_a_docstring_is_not_a_selector():
    """2026-09-16, lab-ovh: `exec:%` "found" in schedule_selectors.py:17 — a DOCSTRING."""
    src = '"""Dispatcher selectors, e.g. exec:% and dm:%."""\nPREFIXES = ("dm:",)\n'
    code, parsed = python_code_text(src)
    assert parsed is True
    assert "exec:%" not in code, "a docstring mention counted as an implementation"
    assert "dm:" in code, "a string used as a VALUE is evidence and must survive"


def test_specimen_4_a_prompt_string_is_not_an_import():
    """2026-09-18, science-claude: pack_stale_resident measured doctor-swarph against
    swarph_cli's mtime. Its only 'swarph' hits are a docstring, a CORS origin and a
    prompt string — and NONE is an import. Note the module name DOES survive here:
    the point is that `import swarph_cli` does not, which is the thing being asked."""
    src = (
        '"""doctor.swarph.ai — password-gated chat backed by cursor-agent -p."""\n'
        'allow_origins = ["https://doctor.swarph.ai"]\n'
        'SYSTEM = "You are Doctor, on doctor.swarph.ai."\n'
    )
    code, parsed = python_code_text(src)
    assert parsed is True
    assert "import swarph_cli" not in code
    assert "import" not in code, "no import statement exists in this source"


def test_specimen_3_unit_file_comment_prose_is_not_an_invocation():
    """2026-09-18, lab-ovh: five of nine "ambient" hits were unit-file COMMENT TEXT,
    one literally the fragment "status`), not by being typed at."."""
    unit = (
        "# swarph monitor status`), not by being typed at.\n"
        "[Service]\n"
        "ExecStart=/home/ubuntu/.local/bin/swarph monitor start --as gridiron\n"
    )
    code, parsed = hash_comment_code_text(unit)
    assert parsed is True
    assert "not by being typed at" not in code
    assert code.count("swarph monitor") == 1, "the comment was counted as an invocation"


def test_a_trailing_comment_is_dropped_but_the_directive_survives():
    unit = "ExecStart=/bin/true --as lab-ovh   # historical: was --as droplet\n"
    code, _ = hash_comment_code_text(unit)
    assert "--as lab-ovh" in code
    assert "droplet" not in code


# ── THE CAN-FAIL: prove the splitter is doing the work ───────────────────────

def test_without_the_splitter_every_specimen_is_a_false_positive():
    """The naive check each specimen was actually caught by. If these assertions
    ever fail, the specimens have stopped being specimens and this suite is
    testing nothing."""
    assert "exec:%" in '"""selectors e.g. exec:% and dm:%."""'
    assert "swarph" in '"""doctor.swarph.ai — chat."""'
    assert "swarph monitor" in "# swarph monitor status`), not by being typed at."


# ── CANNOT_VERIFY is a THIRD state, never False ──────────────────────────────

def test_untokenizable_python_is_cannot_verify_not_empty_evidence():
    code, parsed = python_code_text("def broken(:\n")
    assert parsed is CANNOT_VERIFY
    assert parsed is not False, "False would read as 'looked, found nothing'"
    assert code == "", "no text, so a substring test cannot accidentally succeed"


def test_an_unknown_format_is_cannot_verify_not_raw_text(tmp_path):
    """Handing back raw text for a format we cannot split would make this module a
    participant in the defect it exists to prevent."""
    p = tmp_path / "thing.rb"
    p.write_text("# a ruby comment mentioning swarph\nputs 1\n", encoding="utf-8")
    code, full, parsed = code_text(str(p))
    assert parsed is CANNOT_VERIFY
    assert code == ""
    assert "swarph" in full, "the caller can still see the raw text deliberately"


@pytest.mark.parametrize("name", ["u.service", "t.timer", "s.sh", "c.yaml", "Dockerfile"])
def test_hash_formats_are_recognised_by_name(tmp_path, name):
    p = tmp_path / name
    p.write_text("# comment\nreal=1\n", encoding="utf-8")
    code, _, parsed = code_text(str(p))
    assert parsed is True
    assert "comment" not in code and "real=1" in code


# ── the known ceiling, pinned so it cannot be mistaken for a bug later ───────

def test_the_hash_ceiling_fails_toward_LESS_text_and_is_pinned():
    """A `#` inside a quoted value loses the tail. KNOWN, documented, and the SAFE
    direction: it can cause a MISS, never a false hit."""
    code, _ = hash_comment_code_text('Environment=MSG="a # b"\n')
    assert code == 'Environment=MSG="a '
    assert "b" not in code
