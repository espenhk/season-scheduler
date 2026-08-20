"""The reason this project exists: scheduling two leagues together should beat
scheduling them independently for the clubs that field a team in both.

Runs against the real 2026 data, since the coupling only matters where it
actually applies — the six clubs with both an Eliteserien and a Toppserien
side. `align_dual_clubs` (terminliste/solvers/greedy.py) is the piece doing
the coupling; `align=False` reproduces what scheduling the two leagues with no
knowledge of each other would look like.
"""

from __future__ import annotations

from terminliste.model.calendar import build_calendar
from terminliste.model.travel import ApiTravelModel
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.registry import build_constraints
from terminliste.solvers.greedy import build_initial_schedule


def test_dual_clubs_never_clash_at_two_different_venues(world, season, competitions, travel):
    matches, _, _ = build_initial_schedule(world, season, competitions, seed=42)
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=travel, detail=True)
    score = evaluate(matches, constraints, ctx)

    clash = score.result("club_home_clash")
    assert clash.count == 0


def test_venue_sharing_dual_clubs_never_double_book_their_shared_ground(world, season, competitions, travel):
    """Every dual club in the shipped data shares one stadium between its
    men's and women's team, so this is where the coupling actually has to pay
    off — the same-venue case `club_home_clash` deliberately excludes."""
    matches, _, _ = build_initial_schedule(world, season, competitions, seed=42)
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=travel, detail=True)
    score = evaluate(matches, constraints, ctx)

    assert score.result("venue_double_booking").count == 0


def test_alignment_produces_more_back_to_back_home_days_than_independent_scheduling(
    world, season, competitions, travel
):
    constraints = build_constraints(world, season, competitions)
    calendar = build_calendar(world, season)

    aligned, _, _ = build_initial_schedule(world, season, competitions, seed=42, calendar=calendar, align=True)
    independent, _, _ = build_initial_schedule(
        world, season, competitions, seed=42, calendar=calendar, align=False
    )

    ctx = EvalContext(world=world, season=season, travel=travel, detail=True)
    aligned_score = evaluate(aligned, constraints, ctx)
    independent_score = evaluate(independent, constraints, ctx)

    aligned_pairs = aligned_score.result("consecutive_home_days").count
    independent_pairs = independent_score.result("consecutive_home_days").count

    assert aligned_pairs > independent_pairs
