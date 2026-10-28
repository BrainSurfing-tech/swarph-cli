"""Card #1340: quoted gh is not a gh call, a missing swarph exits 2, and a
refusing doctor does not install the router."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route
from swarph_cli.commands.hooks import (
    _GH_IDENTITY_ROUTER_SH,
    _load_settings,
    init_hooks,
)


def test_quoted_gh_commit_is_allowed_on_an_unmapped_cell(tmp_path, monkeypatch):
    quoted = 'git commit -m "fix; gh routing was wrong"'
    assert not ghid.targets_gh(quoted)
    assert ghid.targets_gh("foo; gh pr list")
    assert ghid.targets_gh('echo "$(gh pr list)"')

    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "meta-muse")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": quoted},
        }))
    assert rc == 0
    assert buf.getvalue().strip() == ""


def test_missing_swarph_exits_2(tmp_path):
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell on PATH")
    script = tmp_path / "gh-identity-router.sh"
    script.write_text(_GH_IDENTITY_ROUTER_SH, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty)
    proc = subprocess.run(
        [shell, str(script)], env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 2, proc.stderr
    assert "not on PATH" in proc.stderr


def test_init_with_a_refusing_doctor_does_not_install_the_router(tmp_path, monkeypatch):
    monkeypatch.setenv(ghid.MAPPING_ENV, str(tmp_path / "absent.json"))
    monkeypatch.setattr(gh_route, "_any_yaml_login", lambda: False)
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
    text = json.dumps(_load_settings(settings_path))
    assert "gh-identity-router" not in text
    assert "cell-resilience" in text
    assert not (hooks_home / "gh-identity-router.sh").exists()
    joined = "\n".join(lines)
    assert "gh-identity-router was not installed" in joined
    assert "DO NOT INSTALL THE HOOK" in joined
