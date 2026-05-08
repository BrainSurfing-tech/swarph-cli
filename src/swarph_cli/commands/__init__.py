"""Subcommand handlers for ``swarph``.

Phase 2.5 ships ``import``. Phase 3+ adds ``--ask`` / ``list-peers``;
Phase 5+ adds ``chat`` REPL; Phase 5.5 adds ``onboard`` / ``ratify``;
Phase 5.7 adds ``daemon``.

Each handler is a function that takes a list of argv-style argument
strings (the verb stripped off) and returns an int exit code.
"""

from __future__ import annotations

from swarph_cli.commands.import_session import run_import

__all__ = ["run_import"]
