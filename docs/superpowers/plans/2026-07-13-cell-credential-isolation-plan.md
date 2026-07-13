# Cell Credential Isolation (#2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give headless one-shot agent spawns a disposable HOME that carries only the provider's own auth, so a spawned `claude -p`/codex/gemini can no longer read the operator's GitHub token or on-disk git/ssh credentials.

**Architecture:** A new focused module in **swarph-shared** (`agent_isolation.py`) generalises grok's proven in-repo isolation (`swarph-cli spawn.py:_grok_env`/`_link_grok_auth`/`_scrub_grok_namespace`) into provider-agnostic helpers. It lands in swarph-shared (not swarph-cli) so every consumer — swarph-cli's providers, claude-service, gpt-service, orchestrator — can import it. swarph-shared is published (commander-authorized 2026-07-13), then swarph-cli's `providers.py:run_provider` becomes the first wired consumer.

**Tech Stack:** Python 3.13, stdlib only (`os`, `pathlib`, `subprocess`), pytest via `venv/bin/python -m pytest`. Publish via twine + `~/.pypirc`.

## Global Constraints

- **Helper home = swarph-shared** (`/home/ubuntu/swarph-shared`, pkg `swarph_shared`). It imports `scrub_env_for_subprocess`/`FORBIDDEN_KEYS_EXPLICIT`/`FORBIDDEN_SUFFIXES` from the same-package `swarph_shared.subprocess_env`.
- **This is critical-path spawn code.** The swarph-shared PR (Tasks 1–3) is additive/low-risk and may merge-on-green + publish (authorized). The **swarph-cli wire-in PR (Task 4) is PR-not-merged** — the commander reviews before it touches live spawns.
- **The credential negative must be PROVEN, not assumed** (Task 3 smoke): show the isolated env cannot reach `~/.config/gh`, not just that HOME changed.
- **Never crash a spawn** — every filesystem step in the home-builder is best-effort (mirrors `_link_grok_auth`): skip-if-correct, replace-stale/dangling, never-clobber-a-real-file, never raise.
- **Version bump:** swarph-shared `0.3.3 → 0.4.0` (minor — additive feature) in BOTH `pyproject.toml` and `src/swarph_shared/__init__.py:__version__`; add `agent_isolation` exports to `__init__.__all__`. No version-pin test exists in swarph-shared (verified) — nothing else to update.
- **swarph-cli dep bump:** `swarph-shared>=0.3.3` → `>=0.4.0` in swarph-cli `pyproject.toml`; reinstall into swarph-cli venv before wiring. No swarph-cli `__version__` change in #2a (internal consumer, no public-CLI surface change) — the `test_version_is_0_27_x` pins stay untouched.
- Stage only named files; never `git add -A`; do not stage `.codegraph/` or `docs/superpowers/plans/2026-06-12-*`.

---

### Task 1: Pure isolation core — env builder, provider auth map, namespace scrub

**Files:**
- Create: `/home/ubuntu/swarph-shared/src/swarph_shared/agent_isolation.py`
- Test: `/home/ubuntu/swarph-shared/tests/test_agent_isolation.py`

**Interfaces:**
- Produces:
  - `PROVIDER_AUTH: dict[str, tuple[str, ...]]` — per-provider auth path(s) *relative to HOME*: `{"claude": (".claude/.credentials.json",), "codex": (".codex/auth.json",), "gemini": (".gemini/oauth_creds.json",), "grok": (".grok/auth.json",)}`.
  - `scrub_provider_namespace(env: dict, provider: str) -> None` — in-place deny of the provider's `*_HOME`/`*_AUTH_PATH`/`*_AUTH_PROVIDER_COMMAND`/`*_CONFIG_DIR` redirect keys (generalises `_scrub_grok_namespace`).
  - `build_isolated_env(source: Mapping[str, str], home: Path, provider: str) -> dict[str, str]` — pure; billing-scrubbed env with `HOME` forced to `str(home)` and the provider namespace scrubbed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_isolation.py
from pathlib import Path

from swarph_shared import agent_isolation as ai


def test_build_isolated_env_forces_home():
    src = {"HOME": "/home/operator", "PATH": "/usr/bin", "FOO": "bar"}
    env = ai.build_isolated_env(src, Path("/tmp/drone-home"), "claude")
    assert env["HOME"] == "/tmp/drone-home", "HOME must be the disposable dir, never the source"
    assert env["PATH"] == "/usr/bin" and env["FOO"] == "bar", "benign vars pass through"


def test_build_isolated_env_scrubs_billing_and_redirect():
    src = {"HOME": "/home/operator", "ANTHROPIC_API_KEY": "sk-x",
           "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/evil"}
    env = ai.build_isolated_env(src, Path("/tmp/h"), "claude")
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env, "a namespace redirect that would bypass forced HOME is scrubbed"


def test_build_isolated_env_does_not_mutate_source():
    src = {"HOME": "/home/operator", "PATH": "/usr/bin"}
    ai.build_isolated_env(src, Path("/tmp/h"), "codex")
    assert src["HOME"] == "/home/operator", "source dict is never mutated"


def test_provider_auth_map_relative_paths():
    assert ai.PROVIDER_AUTH["claude"] == (".claude/.credentials.json",)
    assert ai.PROVIDER_AUTH["codex"] == (".codex/auth.json",)
    assert not any(p.startswith("/") for paths in ai.PROVIDER_AUTH.values() for p in paths)


def test_scrub_provider_namespace_denies_redirect_keeps_rest():
    env = {"GROK_HOME": "/x", "GROK_AUTH_PATH": "/y", "GROK_MODEL": "keep", "XAI_API_KEY": "z"}
    ai.scrub_provider_namespace(env, "grok")
    assert "GROK_HOME" not in env and "GROK_AUTH_PATH" not in env
    assert env.get("GROK_MODEL") == "keep", "non-redirect namespace vars are preserved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest tests/test_agent_isolation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swarph_shared.agent_isolation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_shared/agent_isolation.py
"""Disposable-HOME credential isolation for headless agent spawns (#2a).

Generalises grok's in-repo isolation (swarph-cli spawn.py) to any provider. A
spawned agent receives a HOME that carries ONLY its own CLI auth — never the
operator's ~/.config/gh, ~/.git-credentials, ~/.netrc, ~/.ssh, which are simply
never linked in. Pure helpers (top half) are unit-tested; prepare_isolated_home
is a best-effort seam that never crashes a spawn.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from swarph_shared.subprocess_env import FORBIDDEN_KEYS_EXPLICIT, FORBIDDEN_SUFFIXES

PROVIDER_AUTH: dict[str, tuple[str, ...]] = {
    "claude": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "gemini": (".gemini/oauth_creds.json",),
    "grok": (".grok/auth.json",),
}

_PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_", "ANTHROPIC_"),
    "codex": ("CODEX_", "OPENAI_"),
    "gemini": ("GEMINI_", "GOOGLE_"),
    "grok": ("GROK_", "XAI_"),
}
_REDIRECT_SUFFIXES = ("_HOME", "_AUTH_PATH", "_AUTH_PROVIDER_COMMAND", "_CONFIG_DIR")


def scrub_provider_namespace(env: dict, provider: str) -> None:
    """In-place: drop redirect keys in ``provider``'s namespace (best-effort)."""
    prefixes = _PROVIDER_PREFIXES.get(provider, ())
    if not prefixes:
        return
    for key in list(env):
        if key.startswith(prefixes) and key.endswith(_REDIRECT_SUFFIXES):
            env.pop(key, None)


def build_isolated_env(source: Mapping[str, str], home: Path, provider: str) -> dict[str, str]:
    """Billing-scrubbed env with HOME forced to ``home`` and redirects scrubbed.

    Pure: ``source`` is never mutated. HOME is IMPOSED (never taken from
    ``source``) — that is what cuts the spawned agent's access to on-disk creds.
    """
    env = {
        k: v for k, v in source.items()
        if k not in FORBIDDEN_KEYS_EXPLICIT and not k.endswith(FORBIDDEN_SUFFIXES)
    }
    scrub_provider_namespace(env, provider)
    env["HOME"] = str(home)
    return env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest tests/test_agent_isolation.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-shared
git add src/swarph_shared/agent_isolation.py tests/test_agent_isolation.py
git commit -m "feat(isolation): pure disposable-HOME env core for headless spawns (#2a)"
```

---

### Task 2: Filesystem home-builder — link only the provider's auth, reset git creds

**Files:**
- Modify: `/home/ubuntu/swarph-shared/src/swarph_shared/agent_isolation.py`
- Test: `/home/ubuntu/swarph-shared/tests/test_agent_isolation.py`

**Interfaces:**
- Consumes: `PROVIDER_AUTH` (Task 1).
- Produces:
  - `prepare_isolated_home(provider: str, root: Path, *, operator_home: Path | None = None) -> Path` — creates `root/.{provider}-drone-home`, symlinks the provider's auth from `operator_home` (default `Path.home()`), writes a minimal `.gitconfig` with `[credential]\n\thelper =`. Best-effort; never raises.
  - `_link_auth(link: Path, target: Path) -> None` — idempotent symlink (skip-if-correct, replace-stale/dangling, never-clobber-real-file, never-raise). Mirrors `_link_grok_auth`.

- [ ] **Step 1: Write the failing test**

```python
def test_prepare_isolated_home_links_only_provider_auth(tmp_path):
    op = tmp_path / "operator"
    (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("SECRET-CLAUDE-AUTH")
    (op / ".config" / "gh").mkdir(parents=True)
    (op / ".config" / "gh" / "hosts.yml").write_text("GH-TOKEN")
    (op / ".git-credentials").write_text("https://x:tok@github.com")

    root = tmp_path / "scratch"
    home = ai.prepare_isolated_home("claude", root, operator_home=op)

    assert home == root / ".claude-drone-home"
    assert (home / ".claude" / ".credentials.json").read_text() == "SECRET-CLAUDE-AUTH"
    assert not (home / ".config" / "gh" / "hosts.yml").exists()
    assert not (home / ".git-credentials").exists()


def test_prepare_isolated_home_resets_git_credential_helper(tmp_path):
    op = tmp_path / "operator"; (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("x")
    home = ai.prepare_isolated_home("claude", tmp_path / "s", operator_home=op)
    assert "helper =" in (home / ".gitconfig").read_text(), "credential helper list reset (blocks system helpers)"


def test_link_auth_idempotent_and_replaces_stale(tmp_path):
    target = tmp_path / "real_auth"; target.write_text("auth")
    link = tmp_path / "link"
    ai._link_auth(link, target)
    assert link.resolve() == target
    ai._link_auth(link, target)
    assert link.resolve() == target
    link.unlink(); link.symlink_to(tmp_path / "gone")   # stale
    ai._link_auth(link, target)
    assert link.resolve() == target


def test_link_auth_never_clobbers_real_file(tmp_path):
    real = tmp_path / "link"; real.write_text("do not delete")
    target = tmp_path / "auth"; target.write_text("auth")
    ai._link_auth(real, target)
    assert real.read_text() == "do not delete"


def test_prepare_isolated_home_never_raises_on_missing_auth(tmp_path):
    op = tmp_path / "operator"; op.mkdir()
    home = ai.prepare_isolated_home("claude", tmp_path / "s", operator_home=op)
    assert home.exists(), "missing operator auth is non-fatal — spawn still gets a HOME"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest tests/test_agent_isolation.py -k "prepare_isolated or link_auth" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'prepare_isolated_home'`.

- [ ] **Step 3: Write minimal implementation** (append to `agent_isolation.py`)

```python
_GITCONFIG = "[user]\n\tname = swarph drone\n\temail = drone@swarph.local\n[credential]\n\thelper =\n"


def _link_auth(link: Path, target: Path) -> None:
    """Idempotent best-effort symlink ``link`` -> ``target`` (mirrors _link_grok_auth)."""
    if not target.exists():
        return
    try:
        if link.is_symlink():
            if link.readlink() == target:
                return
            link.unlink()          # stale/foreign/dangling → replace
        elif link.exists():
            return                 # never clobber a real file
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError:
        return                     # never crash a spawn


def prepare_isolated_home(provider: str, root: Path, *, operator_home: Path | None = None) -> Path:
    """Create root/.{provider}-drone-home carrying ONLY this provider's auth."""
    op = operator_home if operator_home is not None else Path.home()
    home = Path(root) / f".{provider}-drone-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
        for rel in PROVIDER_AUTH.get(provider, ()):
            _link_auth(home / rel, op / rel)
        (home / ".gitconfig").write_text(_GITCONFIG, encoding="utf-8")
    except OSError:
        pass                       # best-effort; a partial home is still a valid HOME
    return home
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest tests/test_agent_isolation.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-shared
git add src/swarph_shared/agent_isolation.py tests/test_agent_isolation.py
git commit -m "feat(isolation): filesystem drone-home builder — link only provider auth (#2a)"
```

---

### Task 3: Smoke test (prove the negative) + version bump + publish swarph-shared

**Files:**
- Test: `/home/ubuntu/swarph-shared/tests/test_agent_isolation_smoke.py`
- Modify: `/home/ubuntu/swarph-shared/pyproject.toml` (version), `/home/ubuntu/swarph-shared/src/swarph_shared/__init__.py` (version + exports)

**Interfaces:** Consumes `prepare_isolated_home`, `build_isolated_env`.

- [ ] **Step 1: Write the smoke test** (proves the credential negative with a real subprocess honouring `$HOME`)

```python
# tests/test_agent_isolation_smoke.py
import subprocess
from swarph_shared import agent_isolation as ai


def test_isolated_spawn_cannot_read_operator_gh_creds(tmp_path):
    op = tmp_path / "operator"
    (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("CLAUDE-AUTH-OK")
    (op / ".config" / "gh").mkdir(parents=True)
    (op / ".config" / "gh" / "hosts.yml").write_text("GH-SECRET-TOKEN")

    home = ai.prepare_isolated_home("claude", tmp_path / "scratch", operator_home=op)
    env = ai.build_isolated_env({"PATH": "/usr/bin:/bin"}, home, "claude")

    script = (
        'test -r "$HOME/.claude/.credentials.json" && echo AUTH_OK || echo AUTH_MISSING; '
        'test -r "$HOME/.config/gh/hosts.yml" && echo GH_LEAK || echo GH_ISOLATED'
    )
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "AUTH_OK" in proc.stdout, "the agent's OWN auth is reachable through the disposable HOME"
    assert "GH_ISOLATED" in proc.stdout and "GH_LEAK" not in proc.stdout, \
        "the operator's gh token is UNREACHABLE from the isolated spawn — the credential negative"
```

- [ ] **Step 2: Run smoke test**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest tests/test_agent_isolation_smoke.py -v`
Expected: PASS.

- [ ] **Step 3: Bump version + export**

In `pyproject.toml`: `version = "0.3.3"` → `version = "0.4.0"`.
In `src/swarph_shared/__init__.py`: `__version__ = "0.3.3"` → `"0.4.0"`; add to the imports+`__all__`:
```python
from swarph_shared.agent_isolation import (
    build_isolated_env,
    prepare_isolated_home,
    PROVIDER_AUTH,
)
```
(append `"build_isolated_env"`, `"prepare_isolated_home"`, `"PROVIDER_AUTH"` to `__all__`).

- [ ] **Step 4: Full suite + build + commit**

Run: `cd /home/ubuntu/swarph-shared && venv/bin/python -m pytest -q`  → expected all green (111 + new).
```bash
git add src/swarph_shared/agent_isolation.py src/swarph_shared/__init__.py pyproject.toml tests/test_agent_isolation.py tests/test_agent_isolation_smoke.py
git commit -m "feat(isolation): agent credential-isolation helper + smoke; bump 0.4.0 (#2a)"
```

- [ ] **Step 5: Publish swarph-shared 0.4.0** (commander-authorized 2026-07-13)

```bash
cd /home/ubuntu/swarph-shared
rm -rf dist/ && venv/bin/python -m build && venv/bin/python -m twine upload dist/*
```
Then verify: `pip index versions swarph-shared` (or a `--no-cache-dir` install in a scratch venv) shows `0.4.0`. Per the PyPI-CDN-cache-race lesson, retry the install check if the first fetch 404s.

---

### Task 4: Wire isolation into swarph-cli `providers.py:run_provider` (PR-not-merged)

**Files:**
- Modify: `/home/ubuntu/swarph-cli/pyproject.toml` (dep bump), `/home/ubuntu/swarph-cli/src/swarph_cli/service/providers.py`
- Test: `/home/ubuntu/swarph-cli/tests/test_providers_isolation.py`

**Interfaces:** Consumes `build_isolated_env`, `prepare_isolated_home` from `swarph_shared.agent_isolation`.

- [ ] **Step 1: Bump dep + reinstall**

In swarph-cli `pyproject.toml`: `"swarph-shared>=0.3.3"` → `"swarph-shared>=0.4.0"`.
Run: `cd /home/ubuntu/swarph-cli && venv/bin/pip install --upgrade 'swarph-shared>=0.4.0'`
Verify: `venv/bin/python -c "from swarph_shared.agent_isolation import build_isolated_env; print('ok')"`

- [ ] **Step 2: Write the failing test**

```python
# tests/test_providers_isolation.py
from pathlib import Path
from swarph_cli.service import providers


def test_run_provider_spawns_with_isolated_home(tmp_path, monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0; stdout = "ok"; stderr = ""

    def fake_run(argv, env=None, **kw):
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "leaked-token")
    out = providers.run_provider("claude", "hi", home_root=tmp_path)

    assert out == "ok"
    assert captured["env"]["HOME"] == str(tmp_path / ".claude-drone-home"), "spawn runs under the disposable HOME"
    assert captured["env"]["HOME"] != str(Path.home()), "never the operator HOME"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && venv/bin/python -m pytest tests/test_providers_isolation.py -v`
Expected: FAIL — unexpected kwarg `home_root` / HOME assertion.

- [ ] **Step 4: Wire `run_provider`** — replace its body:

```python
def run_provider(provider: str, prompt: str, timeout: int = _DEFAULT_TIMEOUT,
                 base_env: dict | None = None, home_root=None) -> str:
    """Run the provider's subscription CLI one-shot under an ISOLATED HOME.

    The disposable HOME carries only this provider's own auth, so the spawned
    CLI cannot read the operator's GitHub token or git/ssh credentials (#2a).
    """
    import os
    from pathlib import Path
    from swarph_shared.agent_isolation import build_isolated_env, prepare_isolated_home

    argv = provider_command(provider, prompt)
    root = Path(home_root) if home_root is not None else Path.home() / ".swarph" / "drone-homes"
    home = prepare_isolated_home(provider, root)
    src = os.environ if base_env is None else base_env
    env = build_isolated_env(src, home, provider)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{provider} CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()
```

- [ ] **Step 5: Run tests + full suite**

Run: `cd /home/ubuntu/swarph-cli && venv/bin/python -m pytest tests/test_providers_isolation.py -v && venv/bin/python -m pytest -q`
Expected: PASS; full suite green.

- [ ] **Step 6: Commit (do NOT merge — critical path, commander reviews)**

```bash
cd /home/ubuntu/swarph-cli
git add pyproject.toml src/swarph_cli/service/providers.py tests/test_providers_isolation.py
git commit -m "feat(isolation): run_provider spawns under an isolated HOME (#2a wire-in)"
```

---

## Post-plan notes (not tasks)
- **swarph-shared PR (Tasks 1–3):** additive, low-risk → may merge-on-green + publish 0.4.0 (authorized).
- **swarph-cli PR (Task 4):** critical-path spawn change → **PR-not-merged**; commander reviews before it reaches live spawns.
- **Follow-ups (out of #2a scope, in the spec):** wire the remaining sites (claude-service ×3, gpt-service, orchestrator.py:452, exec_runners/*.sh); #2b interactive cell membrane; #1 reaper; #3 preflight.
