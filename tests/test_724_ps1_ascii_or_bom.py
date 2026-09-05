"""Card #724 — shipped .ps1 must be PS 5.1-safe (ASCII or UTF-8 BOM).

PowerShell 5.1 decodes BOM-less .ps1 as the ANSI code page (cp1252 on
Western Windows). Non-ASCII UTF-8 bytes become mojibake that break string
terminators and surface as 'Missing closing }' parse walls. A BOM repairs
one file and leaves the trap armed; ASCII-only (or explicit BOM) is the
durable contract for every packaged .ps1.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "swarph_cli" / "scripts"


def _ps1_encoding_ok(raw: bytes) -> bool:
    if raw.startswith(b"\xef\xbb\xbf"):
        return True
    return all(b < 128 for b in raw)


def _shipped_ps1_paths() -> list[Path]:
    return sorted(SCRIPTS.glob("*.ps1"))


def test_every_shipped_ps1_is_ascii_or_utf8_bom():
    paths = _shipped_ps1_paths()
    assert paths, "expected packaged .ps1 under src/swarph_cli/scripts/"
    bad = []
    for path in paths:
        raw = path.read_bytes()
        if not _ps1_encoding_ok(raw):
            non = sorted({b for b in raw if b >= 128})
            bad.append(f"{path.relative_to(ROOT)}: BOM-less with non-ASCII bytes {non[:12]}")
    assert not bad, "PS 5.1-unsafe .ps1:\n  " + "\n  ".join(bad)


def test_ps1_encoding_guard_fails_on_bomless_emdash(tmp_path):
    """CAN-FAIL: one em-dash in a BOM-less file must be rejected."""
    specimen = tmp_path / "bad.ps1"
    specimen.write_bytes("# hold \u2014 deliberate\n".encode("utf-8"))
    assert not _ps1_encoding_ok(specimen.read_bytes())
    assert _ps1_encoding_ok(b"\xef\xbb\xbf# hold \xe2\x80\x94 deliberate\n")
    assert _ps1_encoding_ok(b"# hold -- deliberate\n")
