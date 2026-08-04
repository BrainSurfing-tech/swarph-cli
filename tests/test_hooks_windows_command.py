"""The registered hook command must be EXECUTABLE on the platform that wrote it."""
from __future__ import annotations

import pytest

from swarph_cli.commands import hooks


def test_posix_registers_the_script_path(monkeypatch):
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    cmd = hooks._installed_command(hooks.BUILTIN_HOOKS["codegraph-on-grep"], "~/.swarph/hooks")
    assert cmd.endswith("codegraph-on-grep.sh")


def test_windows_registers_the_EXECUTABLE_not_the_sh_path(monkeypatch):
    """>>> A SHEBANG IS A KERNEL CONVENTION. WINDOWS HAS NO EQUIVALENT. <<<

    The installer registered a bare `.sh` path, so CreateProcess failed and EVERY
    matching tool call reported a hook error. It had "succeeded": file written,
    settings JSON valid — nothing checked the only property that matters, that
    the registered command is RUNNABLE ON THE PLATFORM THAT REGISTERED IT.
    Reported from a Windows cell 2026-08-04; the commander saw it in his session.
    """
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks.shutil, "which",
                        lambda n: r"C:\Py\Scripts\swarph.exe" if "swarph" in n else None)
    cmd = hooks._installed_command(hooks.BUILTIN_HOOKS["codegraph-on-grep"], "~/.swarph/hooks")
    assert ".sh" not in cmd, "a .sh path is not executable on Windows"
    assert "swarph.exe" in cmd and "codegraph-hook" in cmd
    assert cmd.startswith('"'), "an exe path with spaces must be quoted"


def test_windows_refuses_a_real_shell_hook_rather_than_writing_a_dead_entry(monkeypatch):
    """>>> REFUSE, DO NOT REGISTER SOMETHING KNOWN TO FAIL. <<< cell-resilience
    carries real shell logic (mkdir, jq fallback) with no direct-executable
    equivalent. Writing it on Windows yields a hook that errors on EVERY tool
    call — worse than an absent hook, because it is noise the operator must
    diagnose and it discredits the whole hook system."""
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    with pytest.raises(ValueError) as exc:
        hooks._installed_command(hooks.BUILTIN_HOOKS["cell-resilience"], "~/.swarph/hooks")
    assert "cannot execute" in str(exc.value)
    assert "WSL2" in str(exc.value)


def test_windows_refuses_when_the_executable_is_not_findable(monkeypatch):
    """Registering a command we cannot locate would fail at runtime instead of
    install time — the same defer-the-failure shape."""
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks.shutil, "which", lambda n: None)
    with pytest.raises(ValueError) as exc:
        hooks._installed_command(hooks.BUILTIN_HOOKS["codegraph-on-grep"], "~/.swarph/hooks")
    assert "refusing to register" in str(exc.value)


def test_every_thin_wrapper_declares_its_verb():
    """A bundle whose body is only `exec swarph <verb>` MUST carry that verb, or
    it silently becomes un-installable on Windows. Pins the SET, so a fifth
    bundle added later is caught here rather than by a Windows operator."""
    for name, b in hooks.BUILTIN_HOOKS.items():
        body = b.script_body
        meaningful = [ln.strip() for ln in body.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")
                      and ln.strip() not in ("exit 0",)]
        is_thin = len(meaningful) <= 2 and any("swarph " in ln for ln in meaningful)
        if is_thin:
            assert b.verb, f"{name} is a thin wrapper but declares no verb"
