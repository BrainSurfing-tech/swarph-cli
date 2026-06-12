from pathlib import Path
from swarph_cli.capture import paths


def test_paths_honour_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths.lineage_path("droplet") == tmp_path / "swarph" / "lineage" / "droplet.jsonl"
    assert paths.manifest_path("droplet") == tmp_path / "swarph" / "captures" / "droplet.json"
    assert paths.captures_dir() == tmp_path / "swarph" / "captures"


def test_paths_fall_back_to_home_state(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.lineage_path("lab-ovh") == tmp_path / ".local" / "state" / "swarph" / "lineage" / "lab-ovh.jsonl"
