"""A monitor must not poll an inbox nobody can address.

FOUND BY droplet deploying 0.39.0 on his box, within minutes of the release.

`self_name` falls back to the STATE DIR BASENAME when --as is absent. He ran:

    swarph monitor start --state-dir /var/lib/swarph/droplet-monitor

and got a monitor for the peer `droplet-monitor` -- which does not exist. The
gateway had 0 DMs addressed to it, because nothing can be addressed to an
unregistered peer. So it polled a nonexistent inbox, saw zero DMs forever, and
reported itself RUNNING AND HEALTHY.

That is THE EXACT FAILURE THIS CARD EXISTS TO REMOVE -- silent deafness --
reintroduced through CONFIGURATION rather than through code. A nonexistent
inbox and a quiet mesh emit the same signal: nothing.

Two constraints pull against each other and both must hold:
  * `start` is contractually safe to call unconditionally from a SessionStart
    hook, so a DOWN GATEWAY MUST NOT BLOCK IT. We refuse only on a positive
    answer that the peer is absent.
  * An identity the operator TYPED (--as / $SWARPH_SELF) is deliberate; one
    derived from a directory name is incidental. Only the derived path refuses.

Run: venv/bin/python -m pytest tests/test_monitor_self_name_verification.py -v
"""
import pytest

from swarph_cli.commands import monitor


def _peers(*names):
    return (200, {"peers": [{"name": n} for n in names]})


def test_registered_peer_verifies(monkeypatch):
    monkeypatch.setattr(monitor.mesh, "_http_get_json",
                        lambda u, t, **k: _peers("lab-ovh", "droplet"))
    ok, detail = monitor._verify_self_is_registered("droplet", "http://gw", "tok")
    assert ok and detail == "registered"


def test_unregistered_peer_is_refused(monkeypatch):
    """droplet's exact case: the dir basename is not a peer."""
    monkeypatch.setattr(monitor.mesh, "_http_get_json",
                        lambda u, t, **k: _peers("lab-ovh", "droplet"))
    ok, detail = monitor._verify_self_is_registered(
        "droplet-monitor", "http://gw", "tok")
    assert not ok
    assert "NOT a registered peer" in detail


@pytest.mark.parametrize("response", [
    (0, {}),                      # unreachable
    (500, {"detail": "boom"}),    # gateway error
    (200, {"peers": "nonsense"}), # unexpected shape
    (200, {"peers": []}),         # empty
])
def test_never_blocks_when_the_answer_is_unknown(response, monkeypatch):
    """A hook must not fail because the network did. Refuse only on a POSITIVE no."""
    monkeypatch.setattr(monitor.mesh, "_http_get_json", lambda u, t, **k: response)
    ok, detail = monitor._verify_self_is_registered("whoever", "http://gw", "tok")
    assert ok, "unknown must not be treated as absent"
    assert "UNVERIFIED" in detail


def test_derived_identity_is_the_dangerous_path(tmp_path):
    import argparse
    derived = argparse.Namespace(self_name=None, state_dir=str(tmp_path))
    explicit = argparse.Namespace(self_name="droplet", state_dir=str(tmp_path))
    assert monitor._self_name_was_derived(derived) is True
    assert monitor._self_name_was_derived(explicit) is False


def test_start_refuses_a_derived_unregistered_identity(tmp_path, monkeypatch, capsys):
    """End to end: droplet's command must now fail loudly instead of going deaf."""
    d = tmp_path / "droplet-monitor"
    d.mkdir()
    monkeypatch.setattr(monitor.mesh, "_http_get_json",
                        lambda u, t, **k: _peers("lab-ovh", "droplet"))
    monkeypatch.setattr(monitor.mesh, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.delenv("SWARPH_SELF", raising=False)

    rc = monitor.run_monitor(["start", "--state-dir", str(d)])

    assert rc == 2, "must refuse rather than poll a nonexistent inbox"
    err = capsys.readouterr().err
    assert "droplet-monitor" in err
    assert "silent deafness" in err, "name the failure mode"
    assert "--as" in err, "tell the operator the fix"


def test_start_continues_when_identity_was_explicit(tmp_path, monkeypatch, capsys):
    """--as means the operator meant it; they may be pre-staging a new peer."""
    d = tmp_path / "not-a-peer"
    d.mkdir()
    monkeypatch.setattr(monitor.mesh, "_http_get_json",
                        lambda u, t, **k: _peers("lab-ovh"))
    monkeypatch.setattr(monitor.mesh, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(monitor, "_run_detached", lambda *a, **k: 0, raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)

    rc = monitor.run_monitor(["start", "--state-dir", str(d), "--as", "brand-new",
                              "--once"])
    err = capsys.readouterr().err
    assert "NOT a registered peer" in err, "must still WARN"
    assert rc != 2 or "Continuing" in err
