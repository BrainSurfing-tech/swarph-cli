"""The N-way showdown loop -> the CONFUSION VIEW (spec §3), mirroring the
reference ``model_showdown.py``.

For each (model x task): dispatch to the model's backend, extract the answer,
score it. Output is deliberately NOT a bare scalar — a scalar reward-hacks
toward a degenerate always-one-answer strategy (a gun-shy always-SKIP model
topped a weighted-distance leaderboard while missing every real opportunity;
see ``2026-07-24-model-showdown-findings.md`` finding 2/methodological
conclusion 1). So :func:`run_pack` always returns the per-class breakdown
alongside the aggregate, and the CLI always PRINTS it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .backends import Backend, BackendResult, _redact
from .prices import cost_usd, is_known, lookup
from .providers import egress_of_url
from .quality import score


@dataclass
class ModelSpec:
    id: str
    backend: str = "metered"
    label: str = ""
    provider: str = ""
    arm: Optional[object] = None

    def __post_init__(self):
        if not self.label:
            self.label = self.id


def parse_models(arg: str) -> list[ModelSpec]:
    """``--models`` csv -> specs. Each token is ``id[:backend[:label]]``;
    backend defaults to ``metered`` (spec §3/§4, decision #3 — bench does not
    auto-detect a provider-specific lane, the caller states it or takes the
    v1 default)."""
    specs = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        # rule:<module:callable> and typed-http:<url> carry colons in the
        # payload, so they cannot go through the id:backend:label split.
        if tok.startswith("provider:"):
            rest = tok[len("provider:"):]
            name, _, label = rest.partition(":")
            specs.append(ModelSpec(
                id=name, backend="provider", provider=name,
                label=label or f"provider:{name}"))
            continue
        for prefix, backend in (("typed-http:", "typed-http"), ("rule:", "rule")):
            if tok.startswith(prefix):
                specs.append(ModelSpec(id=tok[len(prefix):], backend=backend, label=tok))
                break
        else:
            parts = tok.split(":")
            mid = parts[0]
            backend = parts[1] if len(parts) > 1 and parts[1] else "metered"
            label = parts[2] if len(parts) > 2 and parts[2] else mid
            specs.append(ModelSpec(id=mid, backend=backend, label=label))
    return specs


def class_of(task: dict) -> str:
    """The confusion-matrix bucket a task falls into. Prefers an explicit
    ``task.meta.class`` (as the reference judgment-pack builder emits:
    trap/edge/winner); falls back to the expected value for categorical tasks
    (the natural class label, e.g. BUY/SKIP) and to the task ``type``
    otherwise."""
    meta = task.get("meta") or {}
    if meta.get("class"):
        return str(meta["class"])
    if task.get("type") == "categorical":
        return str(task.get("expected"))
    return str(task.get("type"))


@dataclass
class TaskRow:
    task_id: str
    cls: str
    distance: float
    parse_ok: bool
    tokens_in: int
    tokens_thought: int
    tokens_out: int
    latency_s: float
    cost_usd: float
    estimated: bool
    error: Optional[str] = None
    not_run: Optional[str] = None


@dataclass
class ModelBoardRow:
    label: str
    model_id: str
    backend: str
    ran: int
    tasks_total: int
    mean_distance: float
    parse_fail: int
    total_tokens: int
    cost_usd: float
    mean_latency_s: Optional[float]
    estimated: bool
    per_class: dict = field(default_factory=dict)
    errors: int = 0
    partial: Optional[str] = None
    price_source: str = ""
    egress: str = ""
    base_url: str = ""
    path: str = ""
    kind: str = ""


def _dispatch(backend: Backend, model_id: str, prompt: str, system: str) -> BackendResult:
    return backend.generate(model_id, prompt, system)


def _bound(spec: ModelSpec, backends: dict[str, Backend]):
    if spec.arm is not None:
        return spec.arm
    # provider: is runnable only after the registry built an arm. The dict
    # entry is the selectable class, not a fallback host.
    if spec.backend == "provider":
        return None
    return backends.get(spec.backend)


def _arm_egress(spec: ModelSpec, backend) -> str:
    if getattr(backend, "egress", None):
        return backend.egress
    if spec.backend == "typed-http":
        return egress_of_url(spec.id)
    return "local"


def _priced(spec: ModelSpec, backend) -> bool:
    if getattr(backend, "price", None):
        return True
    return is_known(spec.id)


def preflight(specs: list[ModelSpec], backends: dict[str, Backend], *,
              pack: Optional[dict] = None, allow_egress: Optional[set[str]] = None,
              allow_unpriced: Optional[set[str]] = None,
              max_usd: Optional[float] = None) -> tuple[list[ModelSpec], list[str]]:
    """Credential preflight — BEFORE any dispatch, not discovered via a
    mid-run 401. Checks each spec's resolved backend's declared credential
    requirement (``Backend.missing_creds()``). -> ``(runnable_specs,
    warnings)``: specs with a missing/unwired backend are dropped and a
    clear, actionable warning is emitted naming exactly which model needs
    which credential."""
    runnable: list[ModelSpec] = []
    warnings: list[str] = []
    allow_egress = allow_egress or set()
    allow_unpriced = allow_unpriced or set()
    pack = pack or {}
    pack_kind = pack.get("request_kind")
    for spec in specs:
        backend = _bound(spec, backends)
        if backend is None:
            if spec.backend == "provider":
                warnings.append(
                    f"{spec.label}: provider {spec.provider or spec.id!r} is not in the registry — skipped")
            else:
                warnings.append(f"{spec.label} ({spec.backend}): no backend wired for {spec.backend!r} — skipped")
            continue
        missing = backend.missing_creds() if hasattr(backend, "missing_creds") else []
        if missing:
            warnings.append(
                f"{spec.label} ({spec.backend}) requires {', '.join(missing)} (not set) — "
                f"skipped. export <the credential>=... to enable it, or pass --strict to "
                f"abort the whole run instead of skipping."
            )
            continue
        arm_kind = getattr(backend, "KIND", "semantic")
        if pack_kind and pack_kind != arm_kind:
            warnings.append(f"kind mismatch: pack={pack_kind} arm={arm_kind}")
            continue
        if pack.get("egress") == "on_box_only" and _arm_egress(spec, backend) == "external":
            who = spec.provider or spec.label
            if who not in allow_egress:
                warnings.append(
                    f"pack is on_box_only; arm {who} is external — "
                    f"pass --allow-egress {who} to override")
                continue
        if max_usd is not None and not _priced(spec, backend):
            who = spec.provider or spec.id
            if who not in allow_unpriced:
                warnings.append(
                    f"{spec.label}: unpriced arm refused under --max-usd "
                    f"(pass --allow-unpriced {who})")
                continue
        runnable.append(spec)
    return runnable, warnings


def _call_cost(spec: ModelSpec, backend, result: BackendResult) -> tuple[float, str, bool]:
    price = getattr(backend, "price", None)
    if isinstance(price, dict) and "in_per_mtok" in price:
        usd = (result.tokens_in * float(price["in_per_mtok"])
               + result.tokens_out * float(price.get("out_per_mtok") or 0)) / 1_000_000
        return usd, str(price.get("source") or "registry"), False
    if is_known(spec.id):
        pin, pout = lookup(spec.id)
        usd = (result.tokens_in * pin + (result.tokens_thought + result.tokens_out) * pout) / 1_000_000
        return usd, "prices.py", False
    return 0.0, "unknown", True


def _secrets_of(specs, backends) -> list:
    found = []
    for spec in specs:
        backend = _bound(spec, backends)
        if backend is not None and hasattr(backend, "secrets"):
            found.extend(backend.secrets())
    return found


def run_pack(
    specs: list[ModelSpec],
    pack: dict,
    backends: dict[str, Backend],
    *,
    task_ids: Optional[list[str]] = None,
    max_usd: Optional[float] = None,
    max_usd_per_arm: Optional[float] = None,
    allow_egress: Optional[set[str]] = None,
    ledger_path: Optional[str] = None,
) -> dict:
    """Run every spec against every task in ``pack`` (or a ``task_ids``
    subset). ``backends`` maps backend-name ("metered"/"subscription"/...) ->
    a :class:`~swarph_cli.bench.backends.Backend` instance — the injectable
    seam that keeps every test offline (fake backends are passed in here,
    never constructed inside this function).

    Returns ``{"theme", "tasks_total", "board": [...ranked...], "detail":
    {label: [TaskRow, ...]}}``. ``board`` is ranked by ``(mean_distance,
    cost_usd)`` but EVERY row carries ``per_class`` — the confusion view is
    always present, never optional.
    """
    tasks = pack.get("tasks", [])
    if task_ids is not None:
        wanted = set(task_ids)
        tasks = [t for t in tasks if t.get("id") in wanted]
    system = pack.get("system") or ""

    board: list[ModelBoardRow] = []
    detail: dict[str, list[TaskRow]] = {}
    spent = 0.0
    partial = None
    secrets = _secrets_of(specs, backends)
    allow_egress = allow_egress or set()
    overrides = []

    for spec in specs:
        backend = _bound(spec, backends)
        if backend is None:
            raise KeyError(f"no backend wired for {spec.backend!r} (model {spec.id!r})")
        who = spec.provider or spec.label
        if who in allow_egress:
            overrides.append({
                "provider": who,
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

        rows: list[TaskRow] = []
        arm_spent = 0.0
        for task in tasks:
            if (max_usd is not None and spent >= max_usd) or (
                    max_usd_per_arm is not None and arm_spent >= max_usd_per_arm):
                partial = "spend_cap"
                rows.append(TaskRow(
                    task_id=task["id"], cls=class_of(task), distance=0.0, parse_ok=False,
                    tokens_in=0, tokens_thought=0, tokens_out=0, latency_s=0.0,
                    cost_usd=0.0, estimated=False, error="not_run: spend_cap",
                    not_run="spend_cap",
                ))
                continue
            result = _dispatch(backend, spec.id, task["prompt"], system)
            if result.error:
                result.error = _redact(result.error, secrets)
            if result.error:
                rows.append(TaskRow(
                    task_id=task["id"], cls=class_of(task), distance=1.0, parse_ok=False,
                    tokens_in=0, tokens_thought=0, tokens_out=0, latency_s=result.latency_s,
                    cost_usd=0.0, estimated=result.estimated, error=result.error,
                ))
                continue
            sc = score(task, result.text)
            usd, _source, unknown = _call_cost(spec, backend, result)
            if unknown:
                usd = cost_usd(spec.id, result.tokens_in, result.tokens_thought, result.tokens_out)
            spent += usd
            arm_spent += usd
            rows.append(TaskRow(
                task_id=task["id"], cls=class_of(task), distance=sc["distance"],
                parse_ok=sc["parse_ok"], tokens_in=result.tokens_in,
                tokens_thought=result.tokens_thought, tokens_out=result.tokens_out,
                latency_s=result.latency_s,
                cost_usd=usd,
                estimated=result.estimated or unknown,
            ))
        detail[spec.label] = rows

        done = [r for r in rows if r.error is None and not r.not_run]
        weights = {t["id"]: t.get("weight", 1.0) for t in tasks}
        w_sum = sum(weights.get(r.task_id, 1.0) for r in done) or 1.0
        mean_distance = round(sum(r.distance * weights.get(r.task_id, 1.0) for r in done) / w_sum, 4) \
            if done else 1.0

        per_class: dict[str, dict] = {}
        for r in done:
            c = per_class.setdefault(r.cls, {"n": 0, "hits": 0, "sum_distance": 0.0})
            c["n"] += 1
            c["sum_distance"] += r.distance
            if r.distance <= 0.0:
                c["hits"] += 1
        for c in per_class.values():
            c["hit_rate"] = round(c["hits"] / c["n"], 4) if c["n"] else 0.0
            c["mean_distance"] = round(c["sum_distance"] / c["n"], 4) if c["n"] else 1.0
            del c["sum_distance"]

        board.append(ModelBoardRow(
            label=spec.label, model_id=spec.id, backend=spec.backend,
            ran=len(done), tasks_total=len(tasks),
            mean_distance=mean_distance,
            parse_fail=sum(1 for r in done if not r.parse_ok),
            total_tokens=sum(r.tokens_in + r.tokens_thought + r.tokens_out for r in done),
            cost_usd=round(sum(r.cost_usd for r in done), 8),
            mean_latency_s=round(sum(r.latency_s for r in done) / len(done), 2) if done else None,
            estimated=any(r.estimated for r in done),
            per_class=per_class,
            errors=sum(1 for r in rows if r.error and not r.not_run),
            partial="spend_cap" if any(r.not_run for r in rows) else None,
            price_source=(getattr(backend, "price", None) or {}).get("source", "")
            if isinstance(getattr(backend, "price", None), dict) else "",
            egress=_arm_egress(spec, backend),
            base_url=getattr(backend, "base_url", ""),
            path=getattr(backend, "path", ""),
            kind=getattr(backend, "KIND", ""),
        ))

    board.sort(key=lambda b: (b.mean_distance, b.cost_usd))
    payload = {
        "theme": pack.get("theme"),
        "tasks_total": len(tasks),
        "partial": partial,
        "exit_code": 1 if partial else 0,
        "egress_overrides": overrides,
        "board": [vars(b) for b in board],
        "detail": {label: [vars(r) for r in rows] for label, rows in detail.items()},
    }
    text = _redact(json.dumps(payload), secrets)
    if ledger_path:
        from pathlib import Path
        Path(ledger_path).write_text(text, encoding="utf-8")
    return json.loads(text)
