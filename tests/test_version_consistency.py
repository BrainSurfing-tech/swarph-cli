"""Guard against the 0.13.3 bug class: the __version__ constant in
src/swarph_cli/__init__.py drifting from the pyproject version (0.13.3 shipped
with the constant left at 0.13.2, so `swarph --version` misreported). Reads both
straight from the source tree so it validates what will be built, independent of
whatever is pip-installed in the test env."""
import pathlib
import re


def _read_version(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    assert m, f"version not found via {pattern!r}"
    return m.group(1)


def test_version_constant_matches_pyproject():
    root = pathlib.Path(__file__).resolve().parent.parent
    pyproject_version = _read_version(
        (root / "pyproject.toml").read_text(), r'^version\s*=\s*"([^"]+)"')
    init_version = _read_version(
        (root / "src" / "swarph_cli" / "__init__.py").read_text(),
        r'^__version__\s*=\s*"([^"]+)"')
    assert init_version == pyproject_version, (
        f"src/swarph_cli/__init__.py __version__={init_version!r} but pyproject "
        f"version={pyproject_version!r}. Bump BOTH on every release."
    )
