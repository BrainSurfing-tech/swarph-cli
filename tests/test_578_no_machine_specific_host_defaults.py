"""Guard: no machine-specific IP literal may ship inside the package.

WHY THIS IS WRITTEN AGAINST A PROPERTY, NOT A PATTERN
-----------------------------------------------------
Card #546 fixed `GATEWAY = "http://localhost:8788"` by rewriting every site to
`os.environ.get("MESH_GATEWAY_URL", "http://<tailnet-ip>:8788")`. The finder used
to locate the bug was `grep 'GATEWAY[A-Z_]* *= *"http'` — a constant assigned a
literal. After the fix that query matches NOTHING, because the literal moved into
the fallback ARGUMENT. drop-on-meta-edge, reviewing it:

    "IT FOUND THE BUG ONCE AND IS NOW PERMANENTLY BLIND TO IT."

Four more live sites survived in exactly the shape the fix produced. So this guard
does not look for an assignment shape, or for one particular address. It looks for
the PROPERTY that makes any of them wrong: an address that identifies one specific
machine, shipped to every user of the package.

Loopback is allowed: it is wrong on this fleet (the gateway never binds it) but it
is HARMLESS — it can only reach the caller's own box, never a stranger's retired VPS.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "swarph_cli"

# Tailnet CGNAT 100.64/10, plus RFC1918. Anything in these ranges names a machine.
_MACHINE_SPECIFIC = re.compile(
    r"""\b(
          100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}
        | 10\.\d{1,3}\.\d{1,3}\.\d{1,3}
        | 172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}
        | 192\.168\.\d{1,3}\.\d{1,3}
    )\b""",
    re.VERBOSE,
)

_TEXT_SUFFIXES = {".py", ".md", ".default", ".service", ".timer", ".sh", ".toml", ".json"}


def _sweep(root: Path) -> list[str]:
    """Offender lines under `root`.

    The root is an ARGUMENT, not a module global. First draft monkeypatched a
    global instead; the real (already-clean) tree stayed in play, the synthetic
    bad tree was never scanned, and the can-fail test passed vacuously. Passing
    the root explicitly is what makes the can-fail case able to fail at all.
    """
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            found = _MACHINE_SPECIFIC.search(line)
            if found:
                offenders.append(
                    f"{path.name}:{lineno}: {found.group(0)}  |  {line.strip()[:90]}"
                )
    return offenders


def test_no_machine_specific_ip_ships_in_the_package() -> None:
    """The property: a published package names no particular machine."""
    offenders = _sweep(SRC)
    assert not offenders, (
        "Machine-specific address(es) shipped in the package.\n"
        "A default that names one box expires the day that box is retired "
        "(card #578).\n"
        "Use $MESH_GATEWAY_URL and fail loudly when unset — see "
        "swarph_cli.gateway_default.\n\n" + "\n".join(offenders)
    )


def test_the_guard_actually_fires(tmp_path: Path) -> None:
    """CAN-FAIL: prove the sweep is not vacuously green."""
    bad = 'GATEWAY = os.environ.get("X", "http://100.107.' + '222.72:8788")'
    (tmp_path / "bad.py").write_text(bad + "\n")
    assert _sweep(tmp_path), "the sweep missed a machine-specific IP in a synthetic tree"


def test_loopback_is_deliberately_allowed(tmp_path: Path) -> None:
    """127.0.0.1 must NOT trip the guard — wrong here, harmless anywhere."""
    (tmp_path / "ok.py").write_text('GATEWAY = "http://127.0.0.1:8788"  # local dev\n')
    assert not _sweep(tmp_path)


def test_public_urls_are_not_flagged(tmp_path: Path) -> None:
    """Docs links must not be false positives, or the guard gets muted."""
    (tmp_path / "doc.md").write_text("See https://github.com/BrainSurfing-tech/swarph-cli\n")
    assert not _sweep(tmp_path)


def test_the_premise_still_holds() -> None:
    """PIN THE PREMISE: env-driven resolution must still exist in the package.

    If `gateway_default` disappears, this guard becomes machinery with no visible
    reason and the next reader deletes it as cargo cult. Fail here instead, with
    the reason attached.
    """
    helper = SRC / "gateway_default.py"
    assert helper.exists(), (
        "swarph_cli/gateway_default.py is gone — this guard's reason went with it"
    )
    body = helper.read_text(encoding="utf-8")
    assert "MESH_GATEWAY_URL" in body
    assert "require_gateway" in body, "the fail-loud resolver is the point of #578"


def test_the_suite_does_not_depend_on_the_developer_s_own_gateway(monkeypatch) -> None:
    """PIN THE PREMISE for the whole PR.

    This change was first measured GREEN locally and RED in CI, because the
    developer's shell had MESH_GATEWAY_URL set and CI's did not — the identical
    trap #546 hit ("drop's own probe reported clean because HIS shell had
    MESH_GATEWAY_URL set"). 10 tests silently depended on the ambient value.

    This asserts the resolver itself returns "" in a clean environment, so a
    future contributor whose shell happens to export it cannot re-introduce that
    dependency without a red test rather than a red CI run an hour later.
    """
    from swarph_cli.gateway_default import env_gateway

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    assert env_gateway() == "", (
        "env_gateway() must be empty in a clean environment — if this passes only "
        "because your shell exports MESH_GATEWAY_URL, CI will disagree"
    )
