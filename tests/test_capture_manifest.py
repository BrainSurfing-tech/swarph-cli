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


def test_set_live_pin_roundtrip_and_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.set_live_pin("ghost", "tmux-x")  # no manifest → no-op, no raise
    assert manifest.read_manifest("ghost") is None
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1")
    manifest.set_live_pin("droplet", "droplet")
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] == "droplet"


def test_find_pin_holders_sweeps_across_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # the renamed-cell incident: two roles pinning ONE uuid
    manifest.write_manifest("drop-mother", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1",
                            live_pin_holder="drop-mother")
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1")
    manifest.write_manifest("other", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-9",
                            live_pin_holder="other")
    holders, corrupt = manifest.find_pin_holders("uuid-1")
    # clear_key is the on-disk filename stem (a safe role), not the cell field
    assert holders == [("drop-mother", "drop-mother")]
    assert corrupt == []


def test_find_pin_holders_reports_corrupt_manifest_fail_closed(tmp_path, monkeypatch):
    # SECURITY (drop seat-A): a corrupt manifest must be REPORTED, not silently
    # skipped — it could hide a live holder; verify fails closed on it.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    paths.captures_dir().mkdir(parents=True, exist_ok=True)
    (paths.captures_dir() / "broken.json").write_text("{not json")
    holders, corrupt = manifest.find_pin_holders("uuid-1")
    assert holders == []
    assert corrupt == ["broken.json"]
