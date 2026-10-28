"""Card #397: install wires the gh identity router without a hand-edited settings file."""
import json
from pathlib import Path

from swarph_cli.commands.hooks import RECOMMENDED_HOOKS, _load_settings, init_hooks

REPO = Path(__file__).resolve().parents[1]


def test_recommended_install_names_the_identity_router(tmp_path):
    assert "gh-identity-router" in RECOMMENDED_HOOKS
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    assert init_hooks(
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
        out=lambda *_a, **_k: None,
    ) == 0
    settings = _load_settings(settings_path)
    text = json.dumps(settings)
    assert "gh-identity-router" in text
    assert "PreToolUse" in settings["hooks"]
    assert (hooks_home / "gh-identity-router.sh").is_file()


def test_plugin_hooks_config_names_the_identity_router():
    path = REPO / "plugins" / "swarph" / "hooks" / "hooks.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(cfg)
    assert "gh-identity-router" in text
    binding = cfg["hooks"]["PreToolUse"][0]
    assert binding["matcher"] == "Bash"
    command = binding["hooks"][0]["command"]
    assert command == "swarph gh-route hook"
