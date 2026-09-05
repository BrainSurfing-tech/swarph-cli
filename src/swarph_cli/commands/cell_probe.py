"""swarph cell probe — FUNCTIONAL known-answer probes for each tool surface (#139).

The functional sibling of cell_selfcheck.py (#133). selfcheck reconciles what the
CONFIG says; this measures what the TOOL ANSWERS. Both are needed: on 2026-09-05 a
key present under a WRONG NAME (SWARPH_BRAIN_GATEWAY vs SWARPH_BRAIN_MCP, wrong port,
right host) passed every config reader on two boxes while recall was dark.

STDLIB-ONLY AT MODULE SCOPE, AND BARE-RUNNABLE — same rule as cell_selfcheck.py, for
the same reason: requiring a working install to diagnose a broken install is the same
circularity as supervising a thing from inside it. Anything needing swarph_cli is
imported INSIDE a probe, and its ImportError becomes a named state.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class State:
    """Five states, five exit codes, deliberately distinct.

    A monitor branches on the exit code. PRESENT and ABSENT sharing 0 would make
    a monitor read "key missing" as success — the exact failure this file exists
    to catch, and the bug in this checker's own first cut (2026-09-05).
    """
    PRESENT = 0          # the surface answered, with real content
    NO_ENTRY = 1         # no such surface configured — NOT "fine"
    UNREADABLE = 2       # config missing/unparseable — never report as ABSENT
    ABSENT = 3           # surface configured, the value it needs is missing
    CLI_PATH_UNSET = 4   # the shell-invocation consumer cannot resolve


_NAMES = {0: "PRESENT", 1: "NO_ENTRY", 2: "UNREADABLE", 3: "ABSENT", 4: "CLI_PATH_UNSET"}


def state_name(state: int) -> str:
    return _NAMES.get(state, "UNKNOWN(%r)" % state)


@dataclass
class Verdict:
    """One probe's answer. `consumer` is mandatory: the same endpoint is reached by
    different routes (MCP-server env vs a shell invocation), and a verdict that does
    not say which route it measured reported healthy on a box where the other was
    broken."""
    state: int
    consumer: str
    detail: str


@dataclass
class Probe:
    """`can_fail` must return a NON-PRESENT verdict when run. A probe never observed
    producing both answers proves nothing by its silence."""
    name: str
    consumer: str
    run: Callable[[], Verdict]
    can_fail: Callable[[], Verdict]


PROBES: dict[str, Probe] = {}


def register(probe: Probe) -> Probe:
    PROBES[probe.name] = probe
    return probe


DEFAULT_CLAUDE_JSON = "~/.claude.json"
ENV_KEY = "SWARPH_BRAIN_MCP"   # the name gateway_default.ENV_BRAIN_MCP actually reads


def read_mcp_env(path: str) -> list[tuple[str, str | None]]:
    """Every swarph MCP entry, top-level AND per-project.

    Walks both because a projects[<cwd>] block OVERRIDES the top-level one for that
    cwd; a reader that takes only `.mcpServers` reports the entry that is not binding.
    Raises OSError/ValueError to the caller — an unreadable file is UNREADABLE, never
    an absent key.
    """
    with open(os.path.expanduser(path)) as fh:
        doc = json.load(fh)
    found: list[tuple[str, str | None]] = []

    def scan(block, where):
        srv = (block or {}).get("swarph")
        if isinstance(srv, dict):
            found.append((where, (srv.get("env") or {}).get(ENV_KEY)))

    scan(doc.get("mcpServers"), "top-level")
    for name, proj in (doc.get("projects") or {}).items():
        scan((proj or {}).get("mcpServers"), "project:" + name)
    return found


def probe_mcp_config(path: str | None = None) -> Verdict:
    path = path or DEFAULT_CLAUDE_JSON
    try:
        found = read_mcp_env(path)
    except (OSError, ValueError) as exc:
        return Verdict(State.UNREADABLE, "mcp-server",
                       "cannot read %s: %s" % (path, exc))
    if not found:
        return Verdict(State.NO_ENTRY, "mcp-server",
                       "no swarph MCP entry in %s — this box reaches gbrain another "
                       "way; that is unmeasured, not healthy" % path)
    missing = [w for w, v in found if not v]
    if missing:
        return Verdict(State.ABSENT, "mcp-server",
                       "%s missing in: %s (of %d entr%s)"
                       % (ENV_KEY, ", ".join(missing), len(found),
                          "y" if len(found) == 1 else "ies"))
    return Verdict(State.PRESENT, "mcp-server",
                   "; ".join("%s -> %s" % (w, v) for w, v in found))


def probe_cli_path() -> Verdict:
    """Ask the resolver the CLI ITSELF calls — never reimplement its env precedence.

    A reimplementation can disagree with the code it is checking, which is this
    card's defect one level up. The swarph_cli import is LAZY on purpose: module
    scope must stay stdlib-only so this file diagnoses a broken install, and an
    unimportable package is a NAMED state rather than a traceback.
    """
    try:
        from .brain_ask import _resolve_endpoint  # noqa: PLC0415 — see docstring
    except Exception as exc:  # noqa: BLE001 — an unimportable package is a verdict
        return Verdict(State.CLI_PATH_UNSET, "shell-invocation",
                       "cannot import swarph_cli.brain_ask: %s" % exc)
    try:
        url = _resolve_endpoint()
    except Exception as exc:  # noqa: BLE001 — GatewayNotConfigured and friends
        return Verdict(State.CLI_PATH_UNSET, "shell-invocation",
                       "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0]))
    return Verdict(State.PRESENT, "shell-invocation", "resolves -> %s" % url)


def gateway_memory_list(gateway: str, token: str, timeout: int = 15) -> list[str]:
    """Slugs from the mesh-gateway's POST /memory. Note the PORT: :8788 is the
    gateway; :8792 is gbrain itself and speaks MCP at /mcp, where POST /memory is
    correctly refused. A route named without its coordinate cost a peer a
    measurement on 2026-09-05."""
    req = urllib.request.Request(
        gateway.rstrip("/") + "/memory",
        data=json.dumps({"op": "list", "arguments": {}}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    doc = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    rows = doc.get("result") or []
    return [r["slug"] for r in rows if isinstance(r, dict) and r.get("slug")]


def probe_gbrain_two_path(gateway: str, token: str,
                          tool_list: Callable[[], object]) -> Verdict:
    """Ask the SAME subject down two paths. A tool that answers alone proves only
    that it answered; the second path is what makes silence legible as failure.
    The subject comes from the gateway's own list output, never a guessed slug."""
    try:
        slugs = gateway_memory_list(gateway, token)
    except Exception as exc:  # noqa: BLE001 — reference path down = no known answer
        return Verdict(State.UNREADABLE, "gateway+mcp-tool",
                       "no known answer available: gateway leg failed (%s)" % exc)
    if not slugs:
        return Verdict(State.UNREADABLE, "gateway+mcp-tool",
                       "no known answer available: gateway returned 0 slugs")
    try:
        got = tool_list()
    except Exception as exc:  # noqa: BLE001
        return Verdict(State.ABSENT, "gateway+mcp-tool",
                       "gateway answered %d slugs; tool raised %s"
                       % (len(slugs), type(exc).__name__))
    if not got:
        return Verdict(State.ABSENT, "gateway+mcp-tool",
                       "gateway answered %d slugs (e.g. %s); tool returned nothing "
                       "— this is 'cannot ask', not 'nothing to return'"
                       % (len(slugs), slugs[0]))
    return Verdict(State.PRESENT, "gateway+mcp-tool",
                   "both paths answered (gateway %d, tool %d)" % (len(slugs), len(list(got))))


def self_sha256() -> str:
    """First 16 hex chars of this file's digest. Every report carries it so a PASS
    names a version rather than a moment."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


def _write_tmp_json(doc: dict) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    return path


def _without_env(keys, fn: Callable[[], Verdict]) -> Verdict:
    """Clear one or more env keys for the duration of fn.

    _resolve_endpoint reads GBRAIN_MCP_URL then SWARPH_BRAIN_MCP. Clearing
    only ENV_KEY leaves a live GBRAIN_MCP_URL as a false PRESENT for can_fail.
    """
    if isinstance(keys, str):
        keys = (keys,)
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        return fn()
    finally:
        for k, old in saved.items():
            if old is not None:
                os.environ[k] = old


register(Probe(
    name="mcp-config", consumer="mcp-server",
    run=probe_mcp_config,
    # CAN-FAIL: a constructed doc whose swarph entry has the wrong key.
    can_fail=lambda: probe_mcp_config(_write_tmp_json(
        {"mcpServers": {"swarph": {"env": {"MESH_GATEWAY_URL": "x"}}}})),
))

register(Probe(
    name="cli-path", consumer="shell-invocation",
    run=probe_cli_path,
    # CAN-FAIL: resolve with both CLI keys cleared. ENV_KEY alone is not
    # enough — GBRAIN_MCP_URL is the resolver's first preference.
    can_fail=lambda: _without_env((ENV_KEY, "GBRAIN_MCP_URL"), probe_cli_path),
))


def run_cell_probe(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="swarph cell probe",
        description="Functional known-answer probes per tool surface (#139)")
    p.add_argument("--only", default=None, help="run one probe by name")
    p.add_argument("--can-fail", action="store_true",
                   help="run each probe's can-fail case instead, and verify it "
                        "produces a NON-present verdict")
    args = p.parse_args(argv)

    probes = PROBES
    if args.only:
        if args.only not in PROBES:
            print("unknown probe %r; have: %s" % (args.only, ", ".join(sorted(PROBES))),
                  file=sys.stderr)
            return 2
        probes = {args.only: PROBES[args.only]}

    worst = State.PRESENT
    print("swarph cell probe  (cell_probe.py sha256 %s)" % self_sha256())
    for name, probe in sorted(probes.items()):
        verdict = probe.can_fail() if args.can_fail else probe.run()
        if args.can_fail and verdict.state == State.PRESENT:
            print("  %-12s [%-8s] CAN-FAIL DID NOT FAIL — probe is unproven"
                  % (name, "BROKEN"))
            worst = max(worst, State.UNREADABLE)
            continue
        print("  %-12s [%-14s] consumer=%-16s %s"
              % (name, state_name(verdict.state), verdict.consumer, verdict.detail))
        if not args.can_fail:
            worst = max(worst, verdict.state)
    return 0 if args.can_fail else worst


# RUNNABLE AS A BARE FILE, ON PURPOSE — same rule as cell_selfcheck.py.
#     python3 src/swarph_cli/commands/cell_probe.py
# A cell whose swarph install is itself part of what is broken still needs this
# baseline, and module scope is stdlib-only so the file runs where the package
# cannot be imported. tests/test_139_tool_probe.py pins that property.
if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(run_cell_probe(sys.argv[1:]))
