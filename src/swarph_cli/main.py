"""``swarph`` entry-point. v0.0.1 SCAFFOLD — prints status banner + exits.

Live impl ships incrementally per PLAN.md §13:
- Phase 2: ``swarph "hello" --provider gemini`` one-shot mode
- Phase 3: ``--ask <peer>`` mesh-aware mode (depends on MeshClient)
- Phase 5: interactive REPL with slash commands (``/inbox``, ``/reply``)
- Phase 5.5: ``swarph onboard`` + ``swarph ratify`` (per PLAN.md §15)
- Phase 5.7: ``swarph daemon`` foreground drain loop (per PLAN.md §16)
"""

from __future__ import annotations

import argparse
import sys

from swarph_cli import __version__


_BANNER = """\
swarph v{version} (CLI scaffold)

STATUS: SCAFFOLD (Phase 0 — substrate scaffold only).

This binary will become the multi-LLM mesh-aware CLI per PLAN.md §10.
v0.0.1 ships only the entry-point. Substrate library (Protocol + types)
lives in:

    pip install swarph-mesh

Spec:
  https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md

Substrate:
  https://github.com/darw007d/swarph-mesh

Phase rollout (per §13):
  1. Substrate v0     — Gemini adapter + SwarphCall + caller convention
  2. CLI v0           — `swarph "hello"` one-shot mode (this binary)
  3. MeshClient       — drains lab_loop_drain.py replacement
  4. Adapter expansion — DeepSeek, Claude (subscription), OpenAI
  5. CLI REPL         — interactive + --ask <peer>
  5.5 Onboard / ratify — `swarph onboard` + `swarph ratify` (§15)
  5.7 Built-in monitor — `swarph daemon` + REPL drain coroutine (§16)
  6. PyPI publish     — `pip install swarph-cli`
"""


def main(argv: list[str] | None = None) -> int:
    """Entry-point. ``argv`` defaults to ``sys.argv[1:]`` when None;
    pass an explicit list (typically ``[]``) from tests so pytest's
    own argv doesn't bleed into the parser."""
    parser = argparse.ArgumentParser(
        prog="swarph",
        description=(
            "swarph — multi-LLM CLI with mesh-gateway integration. "
            "(v0.0.1 SCAFFOLD)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"swarph-cli {__version__}",
    )
    parser.parse_args(argv)
    print(_BANNER.format(version=__version__), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
