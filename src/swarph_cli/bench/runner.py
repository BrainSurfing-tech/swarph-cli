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

from dataclasses import dataclass, field
from typing import Optional

from .backends import Backend, BackendResult
from .prices import cost_usd
from .quality import score


@dataclass
class ModelSpec:
    id: str
    backend: str = "metered"
    label: str = ""

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


def _dispatch(backend: Backend, model_id: str, prompt: str, system: str) -> BackendResult:
    return backend.generate(model_id, prompt, system)


def preflight(specs: list[ModelSpec], backends: dict[str, Backend]) -> tuple[list[ModelSpec], list[str]]:
    """Credential preflight — BEFORE any dispatch, not discovered via a
    mid-run 401. Checks each spec's resolved backend's declared credential
    requirement (``Backend.missing_creds()``). -> ``(runnable_specs,
    warnings)``: specs with a missing/unwired backend are dropped and a
    clear, actionable warning is emitted naming exactly which model needs
    which credential."""
    runnable: list[ModelSpec] = []
    warnings: list[str] = []
    for spec in specs:
        backend = backends.get(spec.backend)
        if backend is None:
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
        runnable.append(spec)
    return runnable, warnings


def run_pack(
    specs: list[ModelSpec],
    pack: dict,
    backends: dict[str, Backend],
    *,
    task_ids: Optional[list[str]] = None,
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

    for spec in specs:
        backend = backends.get(spec.backend)
        if backend is None:
            raise KeyError(f"no backend wired for {spec.backend!r} (model {spec.id!r})")

        rows: list[TaskRow] = []
        for task in tasks:
            result = _dispatch(backend, spec.id, task["prompt"], system)
            if result.error:
                rows.append(TaskRow(
                    task_id=task["id"], cls=class_of(task), distance=1.0, parse_ok=False,
                    tokens_in=0, tokens_thought=0, tokens_out=0, latency_s=result.latency_s,
                    cost_usd=0.0, estimated=result.estimated, error=result.error,
                ))
                continue
            sc = score(task, result.text)
            rows.append(TaskRow(
                task_id=task["id"], cls=class_of(task), distance=sc["distance"],
                parse_ok=sc["parse_ok"], tokens_in=result.tokens_in,
                tokens_thought=result.tokens_thought, tokens_out=result.tokens_out,
                latency_s=result.latency_s,
                cost_usd=cost_usd(spec.id, result.tokens_in, result.tokens_thought, result.tokens_out),
                estimated=result.estimated,
            ))
        detail[spec.label] = rows

        done = [r for r in rows if r.error is None]
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
            errors=len(rows) - len(done),
        ))

    board.sort(key=lambda b: (b.mean_distance, b.cost_usd))
    return {
        "theme": pack.get("theme"),
        "tasks_total": len(tasks),
        "board": [vars(b) for b in board],
        "detail": {label: [vars(r) for r in rows] for label, rows in detail.items()},
    }
