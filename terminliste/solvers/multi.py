"""Run several solvers on one request and merge their options (#81).

Each backend gets its own diversity: local search restarts from different
seeds, CP-SAT re-solves with the earlier options' fixtures penalised. Both end
in `select_diverse`, so every solver contributes `top_n` options that differ
from each other. All options are scored by the same constraints, which is what
makes them comparable.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import Note, Scheduler, SolveRequest, SolverResult, SolverRun, error

SOLVER_NAMES = ("local", "cpsat")


def parse_solver_names(values: list[str] | None) -> list[str]:
    """Flatten repeated and comma-separated `--solver` values, keeping first-seen order."""
    names: list[str] = []
    for value in values or ["local"]:
        for name in value.split(","):
            name = name.strip()
            if name not in SOLVER_NAMES:
                raise ValueError(f"unknown solver {name!r} — expected one of {', '.join(SOLVER_NAMES)}")
            if name not in names:
                names.append(name)
    return names


def solve_many(
    names: list[str],
    request: SolveRequest,
    get_scheduler: Callable[[str], Scheduler],
    on_start: Callable[[Scheduler], None] | None = None,
) -> SolverResult:
    """Solve with each named backend and merge. A backend that cannot run is
    recorded in `runs` and the notes; the others still go ahead."""
    results: list[tuple[str, SolverResult | None, str]] = []
    for name in names:
        try:
            scheduler = get_scheduler(name)
            if on_start:
                on_start(scheduler)
            results.append((name, scheduler.solve(request), ""))
        except (RuntimeError, ImportError) as exc:
            if len(names) == 1:
                raise
            results.append((name, None, str(exc)))
    return merge_results(results, multi=len(names) > 1)


def merge_results(
    results: list[tuple[str, SolverResult | None, str]], *, multi: bool = True
) -> SolverResult:
    merged = SolverResult(solver=" + ".join(name for name, _, _ in results))
    for name, result, failure in results:
        if result is None:
            merged.runs.append(SolverRun(name, "unavailable", message=failure))
            merged.notes.append(error(f"{name}: skipped — {failure}"))
            continue
        for index, candidate in enumerate(result.candidates, start=1):
            candidate.solver = result.solver or name
            candidate.index = index
            if multi:
                candidate.label = f"{candidate.solver} {index}"
        merged.candidates.extend(result.candidates)
        merged.iterations += result.iterations
        merged.elapsed_s += result.elapsed_s
        merged.search_stats += result.search_stats
        for key, plan in result.slot_plans.items():
            merged.slot_plans.setdefault(key, plan)
        merged.notes.extend(
            Note(f"{result.solver or name}: {note}", getattr(note, "severity", "warning"))
            if multi
            else note
            for note in result.notes
        )
        merged.runs.append(
            SolverRun(
                result.solver or name,
                "ok",
                result.elapsed_s,
                result.iterations,
                len(result.candidates),
            )
        )
    if not multi and results[0][1] is not None:
        merged.solver = results[0][1].solver
        merged.runs = []
    return merged
