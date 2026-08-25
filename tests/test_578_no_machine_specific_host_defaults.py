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

# ".ps1"/".cmd"/".sql" added after seat-A review of PR #318: pyproject's
# package-data ships scripts/*.ps1, payloads/postcompact/*.cmd and gateway/*.sql, and
# none of them was ever opened. scripts/install_codex_waker_windows.ps1 is the Windows
# twin of the systemd/*.default template that actually carried the retired IP — a
# `$Gateway = "http://100.x..."` default there would have shipped unseen.
_TEXT_SUFFIXES = {".py", ".md", ".default", ".service", ".timer", ".sh", ".toml",
                  ".json", ".ps1", ".cmd", ".sql"}


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
        # errors="replace", NOT `except UnicodeDecodeError: continue`. The old form
        # dropped the ENTIRE file on one bad byte: MEASURED in seat-A review of PR
        # #318 with a cp1252 `codex-waker.default` holding the retired IP — missed.
        # The pattern is pure ASCII, so a replacement char can never hide a match,
        # and the sibling sweeps in this repo (test_546:136, test_548:34) already
        # read this way.
        text = path.read_text(encoding="utf-8", errors="replace")
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


# ---------------------------------------------------------------------------
# Closing drop-on-meta-edge's C3 / C5 / C6 — variants that stayed GREEN in
# seat-A review of PR #318. Each observes the SHIPPED value in a subprocess with
# the env removed, which is blind to syntax: or-append, second assignment, or a
# hostname in an argparse default all reach the same observable.
# ---------------------------------------------------------------------------

def _run_probe(program: str, env_extra: dict | None = None) -> str:
    """Run `program` against the SOURCE TREE with the gateway env scrubbed."""
    import os as _os
    import subprocess
    import sys

    env = {k: v for k, v in _os.environ.items()
           if k not in ("MESH_GATEWAY_URL", "SWARPH_GATEWAY", "SWARPH_BRAIN_MCP",
                        "GBRAIN_MCP_URL")}
    env["PYTHONPATH"] = str(SRC.parent)
    env.update(env_extra or {})
    out = subprocess.run([sys.executable, "-c", program],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, f"probe failed: {out.stderr[-600:]}"
    return out.stdout.strip()


def _walk_gateway_defaults(parser) -> list:
    """Every `--gateway` default in a parser AND its subparsers.

    Recursion is the point: `swarph mesh` declares --gateway on six subcommands,
    and a guard that read only the top-level parser would see none of them.
    """
    import argparse

    out = []
    for action in parser._actions:
        if "--gateway" in action.option_strings:
            out.append(action.default)
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                if isinstance(sub, argparse.ArgumentParser):
                    out.extend(_walk_gateway_defaults(sub))
    return out


_DEFAULTS_PROBE = r"""
import argparse, json, os, sys
sys.path.insert(0, {tests!r})
from test_578_no_machine_specific_host_defaults import _walk_gateway_defaults as walk

# IMPORT FIRST, while MESH_GATEWAY_URL is still set (when it is), then remove it.
# A module constant evaluated at import keeps the value; a parser default built per
# invocation does not. That difference is the whole measurement.
from swarph_cli.commands import (mesh, monitor, watchdog, ratify, daemon, onboard,
                                 init, cell_selfcheck)
if os.environ.get("SWARPH_PROBE_DROP_ENV"):
    os.environ.pop("MESH_GATEWAY_URL", None)

vals = {{}}
for name, mod in (("mesh", mesh), ("watchdog", watchdog), ("ratify", ratify),
                  ("daemon", daemon), ("onboard", onboard), ("init", init)):
    for i, v in enumerate(walk(mod._build_parser())):
        vals["%s --gateway[%d]" % (name, i)] = v
_p = argparse.ArgumentParser()
monitor._add_common(_p)
for i, v in enumerate(walk(_p)):
    vals["monitor --gateway[%d]" % i] = v
# NOT a parser: reads the env dict it is HANDED. Its inline os.environ.get is
# deliberately not env_gateway() (cell_selfcheck is stdlib-only), which is what
# gives the agreement test in test_546 its resolving power — poison the shared
# resolver and this one site keeps telling the truth, so they disagree.
vals["cell_selfcheck.resolver_gateway"] = cell_selfcheck.resolver_gateway(os.environ)
print(json.dumps(vals))
"""


def effective_gateway_defaults(env_value: str | None = None,
                               drop_after_import: bool = False) -> dict:
    """What each site would use for `--gateway` when the operator passes nothing.

    Deliberately STRONGER than reading a module constant. Two failures reach the
    same observable here: a host baked into a default (C3's `lab-ovh-1`, which the
    IP sweep cannot see) and a value FROZEN at import from the packaging shell.
    `drop_after_import=True` sets the env, imports, then clears it — so the answer
    does not depend on the developer's own shell in either direction.
    """
    import json

    extra = {}
    if env_value is not None:
        extra["MESH_GATEWAY_URL"] = env_value
    if drop_after_import:
        extra["SWARPH_PROBE_DROP_ENV"] = "1"
    program = _DEFAULTS_PROBE.format(tests=str(Path(__file__).resolve().parent))
    return json.loads(_run_probe(program, extra))


def test_C3_no_gateway_host_is_baked_into_any_parser_default() -> None:
    """C3: a HOSTNAME in the argparse default stayed GREEN through review — the IP
    sweep is blind to `lab-ovh-1`, which is the #546 shape with the IP swapped out.

    Also closes the contamination channel C3 rode in on: the probe sets the env,
    imports, then deletes it, so a constant captured at import shows up as the
    sentinel rather than as "".
    """
    vals = effective_gateway_defaults("http://frozen-at-import.invalid:1",
                                      drop_after_import=True)
    assert len(vals) >= 8, f"the probe found almost no sites — it is broken: {vals}"
    bad = {k: v for k, v in vals.items() if v != ""}
    assert not bad, (
        "a gateway host survives with the environment unset (#578). A non-empty value "
        "here is either a baked-in default or one frozen at import from the shell that "
        f"packaged the release:\n  {bad}"
    )


def test_the_default_probe_can_see_a_baked_host() -> None:
    """CAN-FAIL for the walker: a synthetic parser carrying exactly C3's shape.

    Without this the assertion above could pass by finding nothing — the failure
    mode that made C3 green in the first place.
    """
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    send = sub.add_parser("send")
    send.add_argument("--gateway", default="http://lab-ovh-1:8788")
    assert _walk_gateway_defaults(p) == ["http://lab-ovh-1:8788"]


def test_C5_the_hook_names_UNCONFIGURED_rather_than_dialling_a_host(
        monkeypatch, tmp_path, capsys) -> None:
    """C5: `DEFAULT_GATEWAY = env_gateway() or "http://lab-ovh-1:8788"` was GREEN.

    codegraph_hook has no argparse parser, so the probe above cannot reach it. This
    runs the hook end to end instead and pins the UNCONFIGURED wording, which is the
    only thing that distinguishes the three states the hook must not collapse:
    unconfigured / unreachable / a real negative. With an or-append the hook DIALS
    that host and reports a transport error instead — same exit code, different
    sentence, and this test is what notices.
    """
    import json as _json

    from swarph_cli.commands import codegraph_hook as ch

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "swarph").mkdir(parents=True)
    (tmp_path / ".config" / "swarph" / "probe-cell.peer_token").write_text("tok\n")
    monkeypatch.setenv("SWARPH_SELF", "probe-cell")

    class _Stdin:
        def read(self):
            return _json.dumps({"tool_input": {"command": "grep -n 'def foo' src/x.py"}})

    monkeypatch.setattr("sys.stdin", _Stdin())
    assert ch.run_codegraph_hook([]) == 0, "the hook must never fail a turn"
    out = capsys.readouterr().out
    assert "MESH_GATEWAY_URL is not set" in out, (
        "the hook did not name the UNCONFIGURED gateway — if it dialled a host "
        f"instead, that host is a #578 default:\n{out[:400]}"
    )
    assert "never asked" in out


def test_C6_the_relative_url_refusal_is_wired() -> None:
    """C6: neutering `_require_absolute_gateway_url` left 2625 GREEN.

    A guard nothing exercises is decoration. This calls it directly, so
    disabling it fails here rather than silently.
    """
    import pytest as _pytest

    from swarph_cli.commands import mesh

    with _pytest.raises(RuntimeError, match="MESH_GATEWAY_URL is not set"):
        mesh._require_absolute_gateway_url("/peers")
    mesh._require_absolute_gateway_url("http://gw.test:8788/peers")  # must not raise


def test_resolver_raises_a_catchable_Exception_not_SystemExit() -> None:
    """BLOCKING-3: SystemExit is a BaseException and escapes `except Exception`.

    `mcp_server._memory_navigate` promises "never raises" and `memory.py` is a
    documented CLI fail-safe; both call this resolver indirectly. A SystemExit
    there ends the MCP process mid-tool-call.
    """
    import pytest as _pytest

    from swarph_cli.gateway_default import GatewayNotConfigured, require_gateway

    assert issubclass(GatewayNotConfigured, Exception)
    assert not issubclass(GatewayNotConfigured, SystemExit)
    with _pytest.raises(Exception) as exc:   # noqa: B017 — the point IS Exception
        require_gateway("", env="SWARPH_BRAIN_MCP", what="gbrain MCP")
    assert not isinstance(exc.value, SystemExit)


def test_brain_ask_help_survives_an_unset_env() -> None:
    """BLOCKING-2: the argparse default resolved at parser BUILD time, so --help
    died and the --gateway it told you to pass was never parsed."""
    import os as _os
    import subprocess
    import sys

    env = {k: v for k, v in _os.environ.items()
           if k not in ("SWARPH_BRAIN_MCP", "GBRAIN_MCP_URL")}
    env["PYTHONPATH"] = str(SRC.parent)
    out = subprocess.run(
        [sys.executable, "-c",
         "from swarph_cli.commands.brain_ask import run_brain_ask; run_brain_ask(['--help'])"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, f"--help exited {out.returncode}: {out.stderr[-300:]}"
    assert "usage" in (out.stdout + out.stderr).lower()


def test_watchdog_helpers_degrade_on_an_unconfigured_gateway() -> None:
    """BLOCKING-1: these build Request() OUTSIDE their try, so an empty gateway
    raised ValueError before any I/O — killing the tick before the A2
    process-dead respawn decision. On sys3 both watchdog timers run with
    EnvironmentFile=-/etc/default/swarph-watchdog, and that file does not exist."""
    from swarph_cli.commands import watchdog

    assert watchdog._gateway_unread_count("", "lab-ovh", "tok") is None


def test_the_request_time_refusal_survives_a_deleted_CALL_not_just_a_neutered_body(
        monkeypatch) -> None:
    """C6, one level up: the guard must be WIRED, not merely present.

    `test_C6_the_relative_url_refusal_is_wired` calls the helper directly, so it goes
    red when the helper's BODY is neutered — but stays green if someone deletes the
    `_require_absolute_gateway_url(url)` line from `_post_json`, which is the same
    defect with the same consequence (`ValueError: unknown url type: '/messages'`).
    This exercises the real request path, so the call site is what is under test.
    """
    import pytest as _pytest

    from swarph_cli.commands import mesh

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    with _pytest.raises(RuntimeError, match="MESH_GATEWAY_URL is not set"):
        mesh._post_json("/messages", {}, "tok")
    with _pytest.raises(RuntimeError, match="MESH_GATEWAY_URL is not set"):
        mesh._http_get_json("/peers", "tok")


def test_no_endpoint_is_frozen_at_import_from_the_packaging_shell() -> None:
    """The contamination channel itself, for BOTH env vars.

    Five module constants used to evaluate `env_gateway()` / `os.environ.get(...)` at
    IMPORT time. Nothing asserted on them, so a clean run and a contaminated run
    printed the same number — but they were the one path by which a test could depend
    on a developer's shell, and one of them DID: with SWARPH_BRAIN_MCP exported,
    `test_resolve_endpoint_refuses_rather_than_guessing_a_host` failed, because a test
    can delenv the call-time operands and not a value already captured. Measured in
    seat-A review of PR #318; the gateway half is covered by test_C3 above.
    """
    program = (
        "import os\n"
        "from swarph_cli.commands import brain_ask\n"
        "os.environ.pop('SWARPH_BRAIN_MCP', None)\n"
        "os.environ.pop('GBRAIN_MCP_URL', None)\n"
        "from swarph_cli.gateway_default import GatewayNotConfigured\n"
        "try:\n"
        "    print('RESOLVED:' + brain_ask._resolve_endpoint())\n"
        "except GatewayNotConfigured:\n"
        "    print('REFUSED')\n"
    )
    got = _run_probe(program, {"SWARPH_BRAIN_MCP": "http://frozen-at-import.invalid:1"})
    assert got == "REFUSED", (
        "brain_ask resolved a gbrain endpoint after the environment was cleared — it "
        f"is holding a value captured at import: {got}"
    )


def test_the_sweep_reads_a_non_utf8_file_instead_of_skipping_it(tmp_path: Path) -> None:
    """CAN-FAIL for the encoding fix. `except UnicodeDecodeError: continue` dropped the
    WHOLE file, so one accented byte anywhere hid every address in it — and the
    Windows/systemd templates that carried the retired IP are exactly the files most
    likely to be written in a legacy codepage."""
    bad = (tmp_path / "codex-waker.default")
    bad.write_bytes(("# café gateway\nSWARPH_GATEWAY=http://100.107."
                     "222.72:8788\n").encode("cp1252"))
    assert _sweep(tmp_path), "a cp1252 file hid a machine-specific address"


def test_the_sweep_opens_every_shipped_text_type(tmp_path: Path) -> None:
    """CAN-FAIL for the suffix list, one file per type pyproject ships as package-data.

    `scripts/install_codex_waker_windows.ps1` is the Windows twin of the systemd
    `*.default` template that actually carried the retired IP.
    """
    for name in ("install.ps1", "shim.cmd", "schema.sql"):
        (tmp_path / name).write_text('GW = "http://100.107.' + '222.72:8788"\n')
    found = " ".join(_sweep(tmp_path))
    for name in ("install.ps1", "shim.cmd", "schema.sql"):
        assert name in found, f"the sweep never opened {name}"
