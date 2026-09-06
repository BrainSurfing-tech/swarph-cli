from swarph_cli.commands import cell_probe as cp


def test_the_five_states_are_distinct_integers():
    """A monitor branches on rc. Two states sharing a value silently merge
    a defect into a success — the first cut returned 0 for PRESENT and ABSENT."""
    values = [cp.State.PRESENT, cp.State.NO_ENTRY, cp.State.UNREADABLE,
              cp.State.ABSENT, cp.State.CLI_PATH_UNSET]
    assert values == [0, 1, 2, 3, 4]
    assert len(set(values)) == 5


def test_a_verdict_must_name_its_consumer():
    """mcpServers.env feeds the MCP server; a shell `swarph` reads os.environ.
    A verdict that does not say which one it answers for is not usable."""
    v = cp.Verdict(state=cp.State.PRESENT, consumer="mcp-server", detail="ok")
    assert v.consumer == "mcp-server"


import json
import pytest


def _write(tmp_path, doc):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_key_present_reads_present(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"swarph": {"env": {
        "SWARPH_BRAIN_MCP": "http://100.64.189.91:8792/mcp"}}}})
    assert cp.probe_mcp_config(path).state == cp.State.PRESENT


def test_key_missing_reads_absent_not_present(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"swarph": {"env": {
        "MESH_GATEWAY_URL": "http://100.64.189.91:8788"}}}})
    assert cp.probe_mcp_config(path).state == cp.State.ABSENT


def test_no_swarph_entry_is_not_fine(tmp_path):
    """droplet's shape. 'reaches gbrain another way' is an assumption, not health,
    so it gets its own state rather than PRESENT."""
    path = _write(tmp_path, {"mcpServers": {}})
    assert cp.probe_mcp_config(path).state == cp.State.NO_ENTRY


def test_unreadable_file_is_not_an_absent_key(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    assert cp.probe_mcp_config(str(p)).state == cp.State.UNREADABLE
    assert cp.probe_mcp_config(str(tmp_path / "nope.json")).state == cp.State.UNREADABLE


def test_project_override_missing_the_key_reports_absent(tmp_path):
    """THE CASE THAT EARNS THIS PROBE. Top-level reads healthy; the project block is
    what binds for that cwd and lacks the key. Reading only .mcpServers returns the
    reassuring PRESENT. Must be constructed — no real box on the fleet has an
    override, so sampling would never produce it."""
    path = _write(tmp_path, {
        "mcpServers": {"swarph": {"env": {"SWARPH_BRAIN_MCP": "http://x/mcp"}}},
        "projects": {"/home/ubuntu": {"mcpServers": {"swarph": {"env": {}}}}},
    })
    v = cp.probe_mcp_config(path)
    assert v.state == cp.State.ABSENT, "an override missing the key must not read PRESENT"
    assert "project:/home/ubuntu" in v.detail


def test_cli_path_unset_when_env_missing(monkeypatch):
    """The exact state lab-ovh was in AFTER its .claude.json fix: MCP config correct,
    shell invocation still unresolvable."""
    monkeypatch.delenv("SWARPH_BRAIN_MCP", raising=False)
    monkeypatch.delenv("GBRAIN_MCP_URL", raising=False)
    v = cp.probe_cli_path()
    assert v.state == cp.State.CLI_PATH_UNSET
    assert v.consumer == "shell-invocation"


def test_cli_path_present_when_env_set(monkeypatch):
    """CAN-FAIL PAIR for the test above: the same probe must be observed producing
    PRESENT, or its CLI_PATH_UNSET proves nothing about the box."""
    monkeypatch.setenv("SWARPH_BRAIN_MCP", "http://100.64.189.91:8792/mcp")
    v = cp.probe_cli_path()
    assert v.state == cp.State.PRESENT
    assert "8792" in v.detail


def test_cli_and_mcp_are_different_consumers(monkeypatch):
    """They resolve the same endpoint by different routes. Reporting one as the
    other is how a healthy report hid a broken path on 2026-09-05."""
    monkeypatch.setenv("SWARPH_BRAIN_MCP", "http://100.64.189.91:8792/mcp")
    assert cp.probe_cli_path().consumer != cp.probe_mcp_config.__name__
    assert cp.probe_cli_path().consumer == "shell-invocation"


def test_two_path_agreement_reads_present(monkeypatch):
    monkeypatch.setattr(cp, "gateway_memory_list", lambda g, t, timeout=15: ["card-20"])
    v = cp.probe_gbrain_two_path("http://gw:8788", "tok", tool_list=lambda: ["card-20"])
    assert v.state == cp.State.PRESENT


def test_gateway_answers_and_tool_is_silent_reads_absent(monkeypatch):
    """THE SPECIMEN, 2026-09-05: gateway returned 4444 bytes for card-20 while the MCP
    tool returned nothing. One path alone cannot tell this from 'no such memory'."""
    monkeypatch.setattr(cp, "gateway_memory_list", lambda g, t, timeout=15: ["card-20"])
    v = cp.probe_gbrain_two_path("http://gw:8788", "tok", tool_list=lambda: [])
    assert v.state == cp.State.ABSENT
    assert "gateway answered" in v.detail and "tool returned nothing" in v.detail


def test_gateway_unreachable_is_not_a_tool_verdict(monkeypatch):
    """If the reference path is down there is no known answer, so the probe must
    REFUSE rather than blame the tool."""
    def boom(g, t, timeout=15):
        raise OSError("connection refused")
    monkeypatch.setattr(cp, "gateway_memory_list", boom)
    v = cp.probe_gbrain_two_path("http://gw:8788", "tok", tool_list=lambda: [])
    assert v.state == cp.State.UNREADABLE
    assert "no known answer" in v.detail


import hashlib
import subprocess
import sys


def test_two_path_is_registered_and_cli_can_fail_runs_it(capsys):
    """reviewers-pixel on #378: a probe that only exists as a function is the
    silence the card was written to catch. `swarph cell probe --can-fail` must
    run it, and can_fail is the 2026-09-05 specimen (gateway answers, tool silent).
    """
    assert "gbrain-two-path" in cp.PROBES
    v = cp.PROBES["gbrain-two-path"].can_fail()
    assert v.state == cp.State.ABSENT
    assert "tool returned nothing" in v.detail
    rc = cp.run_cell_probe(["--can-fail"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gbrain-two-path" in out
    assert "CAN-FAIL DID NOT FAIL" not in out


def test_two_path_run_without_gateway_is_no_entry_not_silence(monkeypatch):
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    v = cp.PROBES["gbrain-two-path"].run()
    assert v.state == cp.State.NO_ENTRY
    assert v.consumer == "gateway+mcp-tool"


def test_every_registered_probe_has_a_can_fail_that_actually_fails():
    """A probe never observed producing a NON-present verdict proves nothing by its
    silence. The first check published for this card was invalid jq: it failed at
    parse time and printed nothing on every input, so its failure was
    indistinguishable from the condition it detected."""
    assert cp.PROBES, "registry is empty — a monitor with no probes reads as healthy"
    for name, probe in cp.PROBES.items():
        verdict = probe.can_fail()
        assert verdict.state != cp.State.PRESENT, (
            "probe %r can_fail() returned PRESENT — it has never been observed "
            "producing its negative" % name)
        assert probe.consumer, "probe %r does not name its consumer" % name


def test_results_carry_the_sha256_of_what_ran():
    """A green names a version, not a minute: a peer's PASS was recorded against a
    file edited a minute later, in a HOME both cells read."""
    digest = cp.self_sha256()
    expected = hashlib.sha256(
        open(cp.__file__, "rb").read()).hexdigest()[:16]
    assert digest == expected


def test_module_scope_imports_are_stdlib_only():
    """Inherited from cell_selfcheck.py: requiring a working install to diagnose a
    broken install is the same circularity as supervising a thing from inside it.
    Importing the bare file must not pull in swarph_cli."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('cp', %r)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "assert not [k for k in sys.modules if k.startswith('swarph_')], "
        "sorted(k for k in sys.modules if k.startswith('swarph_'))\n"
        "print('OK')\n" % cp.__file__)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout
