"""Repo-wide caller-convention meta-guard (issue #81).

The #80 bug: a hyphenated caller tag (`swarph-compress`) was silently dead at
runtime because it fails swarph_shared's dotted `role.subrole` convention — and it
was masked because the levers/verify tests injected a fake chat and never built a
real SwarphCall. That fix added a per-caller regression test for the two compress
callers. THIS guard generalizes it: it discovers EVERY static caller tag across the
whole `swarph_cli` package by import-introspection and validates each, so the bug
class can't be re-introduced in ANY module without someone remembering to add a
per-caller test.

Design (converged lab+droplet, #81): import-introspection, not AST literal-walking.
The callers live in named `*_CALLER` constants — `caller=SHORTHAND_CALLER` is an
ast.Name, not a literal, so a naive literal-walk would miss exactly the callers #80
hardened. Introspection resolves the value for free and nudges the light convention
that caller tags live in discoverable `*_CALLER` constants.

NOT covered here (separate follow-up, flagged in the PR): the DYNAMIC default-caller
producers (`default_caller` / `_default_repl_caller`) do not guarantee an
[a-z]-leading slug, so a leading-digit/empty OS username yields a non-conforming
`cli.repl.<digit...>` tag — a real robustness bug in the sanitizers, out of scope
for this static meta-guard.
"""
import importlib
import pkgutil

import swarph_cli
from swarph_shared.caller_convention import validate_caller


def _iter_caller_constants():
    """Yield (module, attr, value) for every module-level ``*_CALLER`` str constant
    across the swarph_cli package. Walks the package so a NEW module's caller
    constant is covered automatically. A submodule that can't be imported in this
    env (e.g. an optional dep) is skipped — importability is other tests' concern,
    not this guard's."""
    for mod in pkgutil.walk_packages(swarph_cli.__path__, swarph_cli.__name__ + "."):
        try:
            m = importlib.import_module(mod.name)
        except Exception:
            continue
        for attr in dir(m):
            if attr.endswith("_CALLER"):
                val = getattr(m, attr, None)
                if isinstance(val, str):
                    yield mod.name, attr, val


def test_all_caller_constants_conform():
    """Every ``*_CALLER`` constant in the package satisfies the dotted convention."""
    found = list(_iter_caller_constants())
    # Sanity: the compress callers from #80 must be discovered, else the walk is broken
    # (e.g. swarph_cli resolving to a stale install rather than this tree).
    names = {f"{m.rsplit('.', 1)[-1]}.{a}" for m, a, _ in found}
    assert "levers.SHORTHAND_CALLER" in names and "verify.VERIFY_EXPAND_CALLER" in names, (
        f"meta-guard didn't discover the known compress callers; found: {sorted(names)}")
    for modname, attr, val in found:
        try:
            validate_caller(val)
        except ValueError as e:
            raise AssertionError(
                f"caller constant {modname}.{attr} = {val!r} violates the convention "
                f"(dotted role.subrole) — would crash SwarphCall at runtime: {e}")


def test_default_caller_producers_conform_happy_path(monkeypatch):
    """The default-caller producers emit a conforming tag for a normal username.
    Username is injected so this is deterministic regardless of the CI runner's
    real user (the adversarial leading-digit case is a flagged follow-up, not here)."""
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("LOGNAME", "alice")
    from swarph_cli.main import default_caller
    from swarph_cli.commands.chat import _default_repl_caller
    for fn in (default_caller, _default_repl_caller):
        val = fn()
        try:
            validate_caller(val)
        except ValueError as e:
            raise AssertionError(
                f"{fn.__module__}.{fn.__name__}() -> {val!r} violates the convention: {e}")
