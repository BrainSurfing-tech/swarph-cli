from pathlib import Path

from swarph_cli.commands import codex_waker


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "src" / "swarph_cli" / "scripts" / "install_codex_waker_windows.ps1"


def test_windows_waker_installer_is_packaged():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"scripts/*.ps1"' in text
    assert INSTALLER.is_file()


def test_windows_waker_installer_path_is_discoverable(capsys):
    assert codex_waker.run_codex_waker(["--windows-installer-path"]) == 0
    assert Path(capsys.readouterr().out.strip()).samefile(INSTALLER)


def test_windows_waker_installer_uses_hidden_direct_executable_runners():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "wscript.exe" in text
    assert "cmd.exe" not in text
    assert "New-ScheduledTaskPrincipal" in text
    assert "-LogonType Interactive" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "--drain-outbox" in text
    assert "[switch]$EnableDrainer" in text


def test_windows_waker_installer_keeps_tokens_out_of_task_content():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "[Parameter(Mandatory)] [string]$TokenFile" in text
    assert "Get-Content $TokenFile" not in text
    assert "token contents" not in text.lower()
