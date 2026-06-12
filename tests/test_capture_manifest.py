import json

from swarph_cli.capture import manifest, paths


def test_write_then_read_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.write_manifest(
        "droplet",
        recipe="/cfg/droplet.yaml",
        pin="/state/sessions/droplet.session-id",
        service="claude-tmux@droplet.service",
        lineage="/state/lineage/droplet.jsonl",
        session_id="uuid-1",
    )
    m = manifest.read_manifest("droplet")
    assert m["cell"] == "droplet"
    assert m["service"] == "claude-tmux@droplet.service"
    assert m["head"]["session_id"] == "uuid-1"
    # reserved HEAD fields ship null (deferred checkpoint layer)
    assert m["head"]["jsonl_offset"] is None
    assert m["head"]["sha256"] is None
    assert m["head"]["last_compact_summary_offset"] is None
    assert m["head"]["live_pin_holder"] is None


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert manifest.read_manifest("ghost") is None


def test_clear_live_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.write_manifest(
        "droplet", recipe="r", pin="p", service="s", lineage="l",
        session_id="uuid-1", live_pin_holder="droplet",
    )
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] == "droplet"
    manifest.clear_live_pin("droplet")
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] is None


def test_clear_live_pin_noop_when_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.clear_live_pin("ghost")  # must not raise
