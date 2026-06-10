"""``swarph init`` — interactive install wizard that scaffolds a validated
cell.yaml at the named registry path (``~/.config/swarph/cells/<name>.yaml``).

The default UX is a guided setup: name → LLM type → role/cwd/tmux → assisted
memory (y/n) → register (y/n) → confirm + write. Flags override any prompt, so
a fully-flagged invocation runs non-interactively (scripting / CI).

The cell dict is validated through ``swarph_shared.cell.parse_cell_dict`` BEFORE
writing — init never emits a cell.yaml that wouldn't parse. See
``docs/superpowers/specs/2026-06-02-swarph-init-design.md``.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from swarph_shared.cell import PEER_NAME_RE, VALID_PROVIDERS, parse_cell_dict
from swarph_cli.cell import CellError, cells_dir

_DEFAULT_GATEWAY = os.environ.get("MESH_GATEWAY_URL", "http://lab-ovh:8788")
_CODEX_SANDBOX_DEFAULT = "workspace-write"
_CODEX_SANDBOX_VALUES = ("workspace-write", "read-only")

# LLM type → (provider, blurb). The menu the wizard shows.
_LLM_CHOICES = [
    ("claude", "Anthropic Claude — claude membrane"),
    ("codex", "OpenAI / GPT — codex membrane (AGENTS.md)"),
    ("antigravity", "Google Gemini — agy membrane (firejail)"),
]


# ---------------------------------------------------------------------------
# Prompt helpers (interactive). All no-op when a flag already supplied the value.
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or (default or "")


def _ask_yn(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        ans = ""
    if not ans:
        return default
    return ans in ("y", "yes")


def _ask_llm() -> str:
    print("\nSelect LLM type:")
    for i, (prov, blurb) in enumerate(_LLM_CHOICES, 1):
        print(f"  {i}) {prov:12} {blurb}")
    while True:
        try:
            ans = input("Choice [1-3]: ").strip()
        except EOFError:
            ans = "1"
        if ans.isdigit() and 1 <= int(ans) <= len(_LLM_CHOICES):
            return _LLM_CHOICES[int(ans) - 1][0]
        if ans in VALID_PROVIDERS:
            return ans
        print("  (enter 1, 2, or 3)")


def _https_normalize(repo: str) -> tuple[str, bool]:
    """Rewrite an SSH GitHub URL to HTTPS (this box auths via gh/HTTPS, not a
    deploy key). Returns (url, rewritten)."""
    if repo.startswith("git@github.com:"):
        return "https://github.com/" + repo[len("git@github.com:"):], True
    return repo, False


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph init",
        description="Interactive wizard: scaffold a cell.yaml at "
                    "~/.config/swarph/cells/<name>.yaml. Flags override prompts.",
    )
    p.add_argument("name", nargs="?", default=None,
                   help="kebab-case mesh peer name (prompted if omitted)")
    p.add_argument("--provider", choices=sorted(VALID_PROVIDERS), default=None,
                   help="spawn provider/membrane (prompted if omitted)")
    p.add_argument("--role", default=None)
    p.add_argument("--cwd", default=None)
    p.add_argument("--tmux", default=None)
    p.add_argument("--cursor", default=None)
    p.add_argument("--sandbox", default=None)
    p.add_argument("--gateway", default=_DEFAULT_GATEWAY)
    p.add_argument("--assisted-memory", dest="assisted_memory", default=None, metavar="REPO",
                   help="enable git-backed memory at REPO (SSH→HTTPS normalized)")
    p.add_argument("--starter", default=None)
    p.add_argument("--symlink-cwd", dest="symlink_cwd", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--non-interactive", "-y", dest="non_interactive", action="store_true",
                   help="never prompt; require all values via flags")
    return p


def run_init(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    # Interactive unless explicitly disabled or stdin isn't a TTY.
    interactive = (not args.non_interactive) and sys.stdin.isatty()

    # ── name ──
    name = args.name
    if not name and interactive:
        name = _ask("Cell / peer name (kebab-case)")
    if not name:
        print("swarph init: name required (positional or prompt)", file=sys.stderr)
        return 2
    if not PEER_NAME_RE.match(name):
        print(f"swarph init: name {name!r} must match {PEER_NAME_RE.pattern} "
              f"(kebab-case, no underscores)", file=sys.stderr)
        return 2

    # ── LLM type / provider ──
    provider = args.provider
    if not provider and interactive:
        provider = _ask_llm()
    if not provider:
        print("swarph init: --provider required (or run interactively)", file=sys.stderr)
        return 2

    # ── role / cwd / tmux / cursor ──
    role = args.role or (_ask("Role", name) if interactive else name)
    cwd_raw = args.cwd or (_ask("Working dir (cwd)", str(Path.cwd())) if interactive else str(Path.cwd()))
    cwd = Path(cwd_raw).expanduser().resolve()
    tmux = args.tmux or (_ask("tmux session", name) if interactive else name)
    # Default MUST match where `swarph mesh sidecar` actually writes its
    # cursor (<state-dir>/cursor.json, state-dir defaulting to
    # ~/swarph_state/<name>/mesh-sidecar) — the watchdog reads THIS pin while
    # the sidecar maintains THAT file. The old /tmp default left the two
    # shipped components pointing at different paths out of the box
    # (science-claude onboarding, 2026-06-10).
    cursor = args.cursor or str(
        Path.home() / "swarph_state" / name / "mesh-sidecar" / "cursor.json"
    )

    # ── sandbox ──
    if provider == "codex":
        sandbox = args.sandbox or _CODEX_SANDBOX_DEFAULT
        if sandbox not in _CODEX_SANDBOX_VALUES:
            print(f"swarph init: codex --sandbox must be one of {_CODEX_SANDBOX_VALUES}, "
                  f"got {sandbox!r}", file=sys.stderr)
            return 2
    else:
        sandbox = args.sandbox  # antigravity default-on if None; claude ignores

    cell: dict = {
        "schema_version": "v1",
        "name": name,
        "role": role,
        "provider": provider,
        "cwd": str(cwd),
        "tmux_session": tmux,
        "cursor_path": cursor,
        "mesh": {"gateway": args.gateway},
    }
    if sandbox is not None:
        cell["sandbox"] = sandbox
    if args.starter:
        cell["starter_prompt_path"] = args.starter

    # ── assisted memory (y/n) ──
    am_repo = args.assisted_memory
    if am_repo is None and interactive and _ask_yn("Use assisted memory (git-backed durable memory)?", False):
        am_repo = _ask("  Memory git repo URL")
    am_note = ""
    if am_repo:
        repo, rewritten = _https_normalize(am_repo)
        cell["assisted_memory"] = {"enabled": True, "repo": repo, "interval_min": 15}
        if rewritten:
            am_note = (f"  (repo normalized SSH→HTTPS: {repo}; ensure gh/HTTPS "
                       f"creds can reach it — a private repo needing SSH will fail otherwise)")

    # ── validate BEFORE writing (parse_cell_dict mutates → deepcopy) ──
    try:
        parse_cell_dict(copy.deepcopy(cell))
    except CellError as exc:
        print(f"swarph init: refusing to write an invalid cell.yaml: {exc}", file=sys.stderr)
        return 2

    dest = cells_dir() / f"{name}.yaml"
    if dest.exists() and not args.force:
        if interactive and _ask_yn(f"{dest} exists — overwrite?", False):
            pass
        else:
            print(f"swarph init: {dest} already exists (use --force)", file=sys.stderr)
            return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(cell, sort_keys=False), encoding="utf-8")
    print(f"swarph init: wrote {dest}{am_note}")

    # ── optional cwd symlink ──
    want_symlink = args.symlink_cwd or (interactive and _ask_yn(
        f"Symlink {cwd}/cell.yaml → registry (so `swarph spawn` works from that dir too)?", False))
    if want_symlink:
        _symlink_cwd(cwd, dest, args.force)

    # Resolved summary (echo the derived cwd/tmux/cursor — those were the snags).
    print("  resolved:")
    print(f"    provider={provider}  role={role}  sandbox={sandbox if sandbox is not None else '(default)'}")
    print(f"    cwd={cwd}")
    print(f"    tmux_session={cell['tmux_session']}  cursor_path={cell['cursor_path']}")
    if "assisted_memory" in cell:
        print(f"    assisted_memory.repo={cell['assisted_memory']['repo']} (enabled)")

    # Registration is NOT done here (R1 token mint-once is a once-only secret —
    # init scaffolding from the operator's context shouldn't capture another
    # cell's token; the cell self-adopts from ITS OWN context per the adoption
    # doc, which is the forge-clean path). Deferred follow-up: a careful
    # single-process register that captures→mode-600→verifies the raw token.
    print(f"\nready: swarph spawn {name}")
    print(f"next:  the cell self-registers + adopts its per-peer token from its "
          f"OWN context (see SWARPH_PEER_TOKEN_ADOPTION.md); then `swarph ratify {name}`.")
    return 0


def _symlink_cwd(cwd: Path, dest: Path, force: bool) -> None:
    link = cwd / "cell.yaml"
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if force:
                link.unlink()
            else:
                print(f"swarph init: {link} exists; skip symlink (use --force)", file=sys.stderr)
                return
        link.symlink_to(dest)
        print(f"swarph init: symlinked {link} → {dest}")
    except OSError as exc:
        print(f"swarph init: symlink failed (non-fatal): {exc}", file=sys.stderr)
