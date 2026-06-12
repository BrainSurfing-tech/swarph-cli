"""swarph-cli — the ``swarph`` binary.

Thin client over the ``swarph-mesh`` substrate. v0.0.1 ships the entry-
point + a status banner. Live one-shot mode + REPL ship in Phase 2 / 5
per PLAN.md §13.

The architecture splits CLI from substrate so:

* ``swarph-mesh`` stays a library importable from ``omega-boss``,
  ``Council`` judges, ``lab-orchestrator``, etc. — no CLI surface or
  console-script entry point required.
* ``swarph-cli`` is a tiny argparse + REPL layer on top, ~200 LOC at
  ship-out. Console users get the binary; library callers don't pull
  in the CLI surface.
"""

from __future__ import annotations

__version__ = "0.11.0"

__all__ = ["__version__"]
