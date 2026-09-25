"""Card #959: hook installers bake the venv symlink, not the resolved base."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
INSTALLERS = (
    SRC / "swarph_cli/commands/install_wake_hook.py",
    SRC / "swarph_cli/commands/install_codex_hooks.py",
    SRC / "swarph_cli/commands/install_opencode_plugin.py",
    SRC / "swarph_cli/commands/install_postcompact_hook.py",
)


def test_none_of_the_four_installers_resolves_sys_executable():
    for path in INSTALLERS:
        text = path.read_text(encoding="utf-8")
        assert ".resolve()" not in text, path.name
        assert "realpath" not in text, path.name


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot execute a POSIX #!/bin/sh stub as the interpreter")
def test_refuse_names_the_interpreter_when_import_fails(tmp_path):
    from swarph_cli.commands.hook_interpreter import refuse_unless_importable

    stub = tmp_path / "python-stub"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    with pytest.raises(SystemExit) as exc:
        refuse_unless_importable(str(stub))
    assert str(stub) in str(exc.value)


def _venv_python(venv: Path) -> Path:
    posix = venv / "bin" / "python"
    if posix.exists():
        return posix
    return venv / "Scripts" / "python.exe"


def test_wake_hook_bakes_the_venv_interpreter_and_it_imports(tmp_path):
    import os

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = _venv_python(venv)
    installed = subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "-e", str(REPO)],
        capture_output=True, text=True,
    )
    assert installed.returncode == 0, installed.stderr[-2000:]

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [str(py), "-c",
         "from swarph_cli.commands.install_wake_hook import run_install_wake_hook\n"
         "raise SystemExit(run_install_wake_hook(['--harness', 'claude']))"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    baked = command.split()[0].strip("'\"")
    assert Path(baked) == py.absolute()
    assert "bin/python" in baked or baked.endswith("python.exe")
    imported = subprocess.run([baked, "-c", "import swarph_cli"], capture_output=True, text=True)
    assert imported.returncode == 0, imported.stderr
