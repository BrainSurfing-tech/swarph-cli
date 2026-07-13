from pathlib import Path

from swarph_cli.service import providers


def test_run_provider_spawns_with_isolated_home(tmp_path, monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, env=None, **kw):
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "leaked-token")
    out = providers.run_provider("claude", "hi", home_root=tmp_path)

    assert out == "ok"
    assert captured["env"]["HOME"] == str(tmp_path / ".claude-drone-home"), \
        "spawn runs under the disposable HOME"
    assert captured["env"]["HOME"] != str(Path.home()), "never the operator HOME"
    assert "GH_TOKEN" not in captured["env"], "the operator's GH_TOKEN never reaches the spawn"
