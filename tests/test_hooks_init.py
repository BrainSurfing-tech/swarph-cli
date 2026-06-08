"""``swarph hooks init`` (T5) — install the recommended bundled set.

``init_hooks`` installs every name in ``RECOMMENDED_HOOKS`` (currently just
``cell-resilience``) via ``install_hook(resolve_builtin(name), ...)``. It is:

* **complete**: each recommended builtin's bindings land in settings + its
  script is written under ``hooks_home``;
* **idempotent**: re-running installs nothing new (``_merge_hook`` dedups), so
  each binding's ``hooks[]`` stays length 1;
* **summarizing**: emits one summary line via ``out`` at the end.

Exercised both directly (``init_hooks(...)``) and through the CLI seam
(``run_hooks(["init", "--yes"], settings_path=, hooks_home=)``).
"""

from __future__ import annotations

from pathlib import Path

from swarph_cli.commands.hooks import (
    RECOMMENDED_HOOKS,
    _load_settings,
    init_hooks,
    list_hooks,
    resolve_builtin,
    run_hooks,
)


def _binding_command(settings: dict, event: str, matcher: str) -> list:
    entry = next(
        e for e in settings["hooks"][event] if e.get("matcher", "") == matcher
    )
    return entry["hooks"]


def test_recommended_set_includes_cell_resilience():
    assert "cell-resilience" in RECOMMENDED_HOOKS


def test_init_installs_recommended_set(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = init_hooks(
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
        out=lambda *_a, **_k: None,
    )
    assert rc == 0

    bundle = resolve_builtin("cell-resilience")

    # script written under hooks_home
    script = (hooks_home / bundle.script_name).resolve()
    assert script.exists()

    # both bindings present in settings
    settings = _load_settings(settings_path)
    assert len(_binding_command(settings, "StopFailure", "rate_limit")) == 1
    assert len(_binding_command(settings, "Stop", "")) == 1

    # list_hooks confirms installed
    lines: list[str] = []
    list_hooks(settings_path=settings_path, hooks_home=hooks_home, out=lines.append)
    cr_line = [ln for ln in lines if "cell-resilience" in ln][0]
    assert "installed" in cr_line


def test_init_emits_summary_line(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    lines: list[str] = []
    rc = init_hooks(
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
        out=lines.append,
    )
    assert rc == 0
    joined = "\n".join(str(x) for x in lines).lower()
    # at least one line summarizes the recommended-set install
    assert any("recommended" in str(ln).lower() for ln in lines)
    assert "cell-resilience" in joined


def test_init_idempotent(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    for _ in range(2):
        assert (
            init_hooks(
                settings_path=settings_path,
                hooks_home=hooks_home,
                assume_yes=True,
                out=lambda *_a, **_k: None,
            )
            == 0
        )

    settings = _load_settings(settings_path)
    # no duplicate actions — each binding's hooks[] length 1 after two runs
    assert len(_binding_command(settings, "StopFailure", "rate_limit")) == 1
    assert len(_binding_command(settings, "Stop", "")) == 1


def test_init_cli_seam(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_hooks(
        ["init", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 0

    bundle = resolve_builtin("cell-resilience")
    assert (hooks_home / bundle.script_name).resolve().exists()

    settings = _load_settings(settings_path)
    assert "StopFailure" in settings["hooks"]
    assert "Stop" in settings["hooks"]
