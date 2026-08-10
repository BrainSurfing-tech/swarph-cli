"""Codex SessionStart adapter for the shared Swarph context hook."""

from __future__ import annotations

from .hook_output import run_hook_output


def run_codex_hook_output(argv: list[str] | None = None) -> int:
    """Emit the Codex-compatible SessionStart additional-context payload."""
    return run_hook_output(argv)
