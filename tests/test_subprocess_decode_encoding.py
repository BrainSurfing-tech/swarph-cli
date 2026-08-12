"""Guards for subprocess text capture decoding as UTF-8, not the ANSI code page.

``text=True`` without ``encoding=`` makes Python decode child output with
``locale.getpreferredencoding(False)``. On Linux that is UTF-8, so these paths
are correct there and the defect is invisible; on Windows it is the ANSI code
page (cp1252 on the affected host) while the muxer emits UTF-8.

That is not merely lossy. cp1252 leaves five bytes undefined and 0x90 is one of
them, and 0x90 falls inside the UTF-8 encoding of box-drawing characters, which
every TUI frame is made of -- so a pane read RAISES rather than smudging.

These tests deliberately name ``cp1252`` rather than relying on the host locale.
A test that depended on the platform default would pass vacuously on Linux CI,
which is exactly how the defect survived: the Linux behaviour is not merely
different, it is the one that masks the bug.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import swarph_cli

# The UTF-8 encoding of U+2510 BOX DRAWINGS LIGHT DOWN AND LEFT is e2 94 90.
# 0x90 is undefined in cp1252. Accented text decodes *soft* under cp1252 and
# merely corrupts; it is the box-drawing byte that turns a smudge into a crash.
SAMPLE = "┐ début — coût 12€ ✓ fin"

_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}
_DECODE_KWARGS = {"text", "universal_newlines"}

# Required literal values. Presence of the keyword is NOT sufficient:
# encoding="cp1252" or a dropped errors= would restore the Windows failure path
# while still "declaring an encoding", so the guard asserts the VALUES.
_REQUIRED = {"encoding": "utf-8", "errors": "replace"}

# Call sites intentionally exempt, mapped to the REASON. Empty by design: an
# exemption must be a decision on the record, not an omission, so adding one
# costs a sentence explaining why that site may differ.
_EXEMPT: dict[tuple[str, int], str] = {}


def test_sample_actually_exercises_the_failure_mode() -> None:
    """Positive control: prove the sample CAN fail before trusting that it passes.

    Without this, the two tests below would still pass against a sample that
    cp1252 happens to handle, and would assert nothing about the real defect.
    """
    raw = SAMPLE.encode("utf-8")
    assert b"\x90" in raw, "sample must contain the cp1252-undefined byte"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")
    assert raw.decode("utf-8") == SAMPLE


def test_text_capture_round_trips_utf8_when_encoding_is_declared() -> None:
    """A child emitting UTF-8 bytes is captured intact when encoding= is passed.

    The child writes to ``stdout.buffer`` so its own text layer cannot influence
    the bytes on the wire; the only variable under test is the parent's decode.
    """
    child = "import sys; sys.stdout.buffer.write({!r})".format(SAMPLE.encode("utf-8"))
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == SAMPLE


def test_malformed_bytes_degrade_instead_of_raising() -> None:
    """errors="replace" keeps a bad byte from crashing a caller mid-decision.

    ``session_bridge`` captures pane content to decide idle/busy/modal. A probe
    that raises cannot return a verdict, so the caller loses its fail-safe --
    a smudge is recoverable, a crash is not.
    """
    child = r"import sys; sys.stdout.buffer.write(b'ok\xff\xfetail')"
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("ok")
    assert proc.stdout.endswith("tail")


def _decoding_call_sites() -> list[tuple[str, int, dict[str, object]]]:
    """Every subprocess call that decodes, as (relpath, lineno, literal kwargs).

    Values are captured, not merely presence. A non-literal (``encoding=SOME_VAR``)
    is recorded as ``None`` so it cannot masquerade as a correct literal -- it is
    unverifiable statically and must be exempted with a reason if intended.
    """
    root = Path(swarph_cli.__file__).parent
    found: list[tuple[str, int, dict[str, object]]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - source in the package must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if name not in _SUBPROCESS_FUNCS:
                continue
            names = {kw.arg for kw in node.keywords if kw.arg}
            if not (names & _DECODE_KWARGS):
                continue
            literals: dict[str, object] = {}
            for kw in node.keywords:
                if kw.arg in _REQUIRED:
                    literals[kw.arg] = (
                        kw.value.value if isinstance(kw.value, ast.Constant) else None
                    )
            rel = path.relative_to(root).as_posix()
            found.append((rel, node.lineno, literals))
    return found


def test_ast_guard_finds_call_sites_at_all() -> None:
    """Positive control for the guard below: a vacuous scan must not read as a pass.

    If the walk silently matched nothing -- a moved package, a renamed import --
    the guard would report success while checking zero sites.
    """
    assert len(_decoding_call_sites()) >= 20


def test_every_decoding_subprocess_call_uses_utf8_and_replace() -> None:
    """Every text-capturing subprocess call must declare the required VALUES.

    Presence of an ``encoding`` keyword is not the property being guarded --
    ``encoding="cp1252"``, or a dropped ``errors="replace"``, would restore the
    Windows reader-thread failure while still "declaring an encoding". So the
    literal values are asserted, and a non-literal is treated as unverified.
    """
    offenders: list[str] = []
    for rel, lineno, literals in _decoding_call_sites():
        if (rel, lineno) in _EXEMPT:
            continue
        for kwarg, want in _REQUIRED.items():
            got = literals.get(kwarg, "<missing>")
            if got != want:
                offenders.append(f"{rel}:{lineno} {kwarg}={got!r} (want {want!r})")
    assert not offenders, (
        "subprocess text capture must decode as utf-8 and degrade rather than "
        "raise: without encoding= Python uses locale.getpreferredencoding(False), "
        "the ANSI code page on Windows, and the resulting UnicodeDecodeError is "
        "raised in subprocess's reader thread -- so run() returns rc=0 with "
        "stdout=None and the caller fails somewhere else entirely.\n  "
        + "\n  ".join(offenders)
    )
