from .base import (
    Candidate,
    ProgressUpdate,
    Scheduler,
    SearchStats,
    SolveRequest,
    SolverResult,
    divergence,
    select_diverse,
)
from .greedy import build_initial_schedule, plan_competitions
from .local_search import LocalSearchScheduler

__all__ = [
    "Candidate",
    "LocalSearchScheduler",
    "ProgressUpdate",
    "Scheduler",
    "SearchStats",
    "SolveRequest",
    "SolverResult",
    "build_initial_schedule",
    "divergence",
    "plan_competitions",
    "select_diverse",
]


def get_scheduler(name: str):
    """Resolve a solver by name.

    CP-SAT is imported lazily so the default install stays free of OR-Tools and
    a missing dependency produces a readable message rather than an ImportError
    at startup.
    """
    if name == "local":
        return LocalSearchScheduler()
    if name == "cpsat":
        from .cpsat import CpSatScheduler

        return CpSatScheduler()
    raise ValueError(f"unknown solver {name!r} — expected 'local' or 'cpsat'")
