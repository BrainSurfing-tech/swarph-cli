"""The legacy-conhost remedy must fire for EVERY provider, not just its discovery site."""
from __future__ import annotations

import pytest

from swarph_cli.commands import spawn


class _Cell:
    def __init__(self): self.name = "c"; self.cwd = "/tmp"; self.extra = {}


@pytest.mark.parametrize("provider", sorted(spawn.MEMBRANES))
def test_windows_terminal_relaunch_is_offered_to_every_provider(provider, monkeypatch):
    """>>> PROVIDER DISCRIMINATION, MEASURED. <<<

    The Windows-Terminal relaunch + legacy-conhost warning lived in
    ClaudeMembrane as its "provider-specific extra". It never was: every
    condition tested the TERMINAL (`_relaunch_in_windows_terminal` takes the
    binary and inspects nothing about it; the warning keys on win32 + not-genuine-
    WT). So codex/grok/antigravity cells on Windows got NEITHER the relaunch NOR
    the warning and simply broke — mangled interface, cursor misplaced, session
    exiting. Reported by the commander 2026-08-03 launching a codex cell and
    hitting the exact symptoms Claude had before the fix existed.

    A REMEDY SCOPED TO ITS DISCOVERY SITE IS A REMEDY THAT GETS REDISCOVERED ONCE
    PER PROVIDER.
    """
    seen: list = []
    monkeypatch.setattr(spawn, "_launch_via_tmux", lambda *a, **k: False)
    monkeypatch.setattr(spawn, "_relaunch_in_windows_terminal",
                        lambda binary, argv, cwd: seen.append(binary) or False)
    monkeypatch.setattr(spawn.sys, "platform", "linux")  # skip the warning branch

    spawn.MEMBRANES[provider].pre_launch(
        _Cell(), "/bin/agent", ["agent"], no_banner=True, session_name=None
    )
    assert seen == ["/bin/agent"], (
        f"{provider} never reached the conhost remedy — the Windows fix is "
        f"provider-discriminatory again"
    )


def test_no_membrane_reintroduces_a_private_pre_launch():
    """The override collapsed into the base. If a membrane grows one again it must
    be a DELIBERATE act with a stated reason — this fails loudly so it cannot be
    reintroduced by copy-paste, which is how the original discrimination survived
    the base-hoist."""
    private = [k for k, v in spawn.MEMBRANES.items()
               if "pre_launch" in type(v).__dict__]
    assert private == [], (
        f"membranes with a private pre_launch: {private} — verify it holds "
        f"something that genuinely DIFFERS by provider, not a remedy that applies "
        f"to all of them"
    )
