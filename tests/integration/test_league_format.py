"""Play-off reservations, fixed round dates and day quotas (#15)."""

from __future__ import annotations

from collections import Counter
from datetime import date

import factories as f
import pytest

from terminliste.frame import SeasonFrame, build_playoff_weeks, build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.loader import validate_world
from terminliste.model.schema import DatedNote, FixedRoundDate, LeaguePhase, PlayoffRound
from terminliste.model.travel import ApiTravelModel
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import SlotPlanningError, plan_slots
from terminliste.solvers.base import SolveRequest
from terminliste.solvers.greedy import build_initial_schedule
from terminliste.solvers.local_search import LocalSearchScheduler


def _league(n_teams: int = 4, **extra):
    extra.setdefault("rounds_per_pairing", 1)
    teams = [f.team(f"t{i}", f"c{i}", f"v{i}") for i in range(n_teams)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(n_teams)]
    venues = [f.venue(f"v{i}", lat=59.0 + i * 0.3, lon=10.0 + i * 0.3) for i in range(n_teams)]
    competition = f.competition("league", [t.id for t in teams], **extra)
    return f.world(clubs, venues, [competition]), competition


def _season(**extra):
    extra.setdefault("start", date(2026, 3, 1))
    extra.setdefault("end", date(2026, 5, 31))
    return f.season(competitions=["league"], **extra)


def _playoff(*days: date, reserves_league: bool = True) -> PlayoffRound:
    return PlayoffRound(
        id="po", label="Play-off", dates=list(days), reserves_league=reserves_league
    )


def _plan(world, season, league, frame=None):
    return plan_slots(world, season, [league], build_calendar(world, season), frame)["league"]


# -- play-off reservations -----------------------------------------------------


def test_no_regular_round_is_placed_in_a_playoff_reservation():
    """A reservation inside the regular season removes the slot it falls in,
    wherever it is, not only after the last round."""
    world, base = _league()
    season = _season()
    reserved = _plan(world, season, base).chosen[1].default_day

    _, league = _league(playoff_rounds=[_playoff(reserved)])
    plan = _plan(world, season, league)

    assert len(plan.chosen) == league.rounds
    assert not any(s.contains(reserved) for s in plan.chosen)
    assert not any(s.contains(reserved) for s in plan.candidates)


def test_playoff_dates_from_the_frame_are_reserved_too():
    world, league = _league()
    season = _season()
    reserved = _plan(world, season, league).chosen[1].default_day
    frame = SeasonFrame(
        playoff_weeks=build_playoff_weeks([_league(playoff_rounds=[_playoff(reserved)])[1]])
    )

    plan = _plan(world, season, league, frame)

    assert not any(s.contains(reserved) for s in plan.chosen)


def test_another_divisions_playoff_does_not_reserve_the_league():
    world, base = _league()
    season = _season()
    reserved = _plan(world, season, base).chosen[1].default_day
    _, league = _league(playoff_rounds=[_playoff(reserved, reserves_league=False)])

    assert _plan(world, season, league).chosen == _plan(world, season, base).chosen


def test_frame_lists_playoff_weeks_in_date_order(world, season):
    frame, _ = build_season_frame(world, season)

    dates = {(w.competition_id, w.date) for w in frame.playoff_weeks}
    assert ("eliteserien_2026", date(2026, 12, 21)) in dates
    assert ("toppserien_2026", date(2026, 11, 22)) in dates
    assert [w.date for w in frame.playoff_weeks] == sorted(w.date for w in frame.playoff_weeks)
    assert date(2026, 11, 22) in frame.playoff_dates("toppserien_2026")
    # Another division's play-off does not reserve the league.
    assert date(2026, 10, 31) not in frame.playoff_dates("toppserien_2026")


def test_the_shipped_data_cites_a_format_source(world, season):
    for competition_id in season.competitions:
        competition = world.competition(competition_id)
        assert competition.format_source.startswith("http")
        assert competition.playoff_rounds
        assert sum(p.rounds for p in competition.phases) == competition.rounds


def test_a_pinned_date_inside_a_reservation_is_rejected():
    world, league = _league(
        playoff_rounds=[_playoff(date(2026, 3, 15))],
        fixed_round_dates=[FixedRoundDate(round=1, date=date(2026, 3, 15))],
    )

    with pytest.raises(SlotPlanningError, match="play-off reservation"):
        _plan(world, _season(), league)


# -- fixed round dates ----------------------------------------------------------


def test_fixed_round_dates_pick_the_slot_for_that_round():
    world, base = _league()
    season = _season()
    target = date(2026, 4, 19)
    assert _plan(world, season, base).chosen[0].default_day != target

    _, league = _league(fixed_round_dates=[FixedRoundDate(round=1, date=target)])
    plan = _plan(world, season, league)

    assert plan.chosen[0].contains(target)
    assert plan.chosen[0].default_day == target
    assert len(plan.chosen) == league.rounds


def test_a_fixed_date_in_no_usable_slot_is_an_error():
    world, league = _league(
        fixed_round_dates=[FixedRoundDate(round=2, date=date(2026, 3, 11))]  # a Wednesday
    )

    with pytest.raises(SlotPlanningError, match="no usable round slot"):
        _plan(world, _season(), league)


def _request(world, season, competition):
    return SolveRequest(
        world=world,
        season=season,
        competitions=[competition],
        constraints=build_constraints(world, season, [competition]),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
        seed=3,
        top_n=1,
        time_budget_s=3.0,
    )


def test_the_whole_fixed_round_is_played_on_its_date():
    target = date(2026, 4, 19)
    world, league = _league(fixed_round_dates=[FixedRoundDate(round=2, date=target)])
    season = _season()

    result = LocalSearchScheduler().solve(_request(world, season, league))

    best = result.candidates[0]
    assert {m.date for m in best.matches if m.round_index == 1} == {target}
    assert "fixed_round_dates" in {r.constraint_id for r in best.score.hard_results()}
    assert not [r for r in best.score.hard_results() if r.count]


# -- day quota -------------------------------------------------------------------

_QUOTA = {"friday": 0, "saturday": 2, "sunday": 5, "monday": 1}


def _week_counts(matches):
    per_round: dict[int, Counter] = {}
    for m in matches:
        per_round.setdefault(m.round_index, Counter())[m.date.strftime("%A").lower()] += 1
    return per_round


def test_planner_refuses_a_quota_the_round_cannot_fit():
    world, league = _league(
        n_teams=16, day_quota={"friday": 0, "saturday": 2, "sunday": 2, "monday": 1}
    )
    season = _season(end=date(2026, 12, 31))

    with pytest.raises(SlotPlanningError):
        _plan(world, season, league)


def test_greedy_schedule_honours_the_day_quota():
    world, league = _league(n_teams=16, rounds_per_pairing=1, day_quota=_QUOTA)
    season = _season(end=date(2026, 12, 31))

    matches, _, _ = build_initial_schedule(world, season, [league], seed=5)

    for round_index, counts in _week_counts(matches).items():
        if round_index in league.day_quota_exempt_rounds:
            continue
        for weekday, limit in _QUOTA.items():
            assert counts[weekday] <= limit, (round_index, weekday, counts)


def test_local_search_never_breaks_the_day_quota():
    quota = {"friday": 0, "saturday": 1, "sunday": 2, "monday": 1}
    world, league = _league(n_teams=8, rounds_per_pairing=1, day_quota=quota)
    season = _season(end=date(2026, 9, 30))

    result = LocalSearchScheduler().solve(_request(world, season, league))

    for candidate in result.candidates:
        for round_index, counts in _week_counts(candidate.matches).items():
            if round_index in league.day_quota_exempt_rounds:
                continue
            for weekday, limit in quota.items():
                assert counts[weekday] <= limit, (round_index, weekday, counts)
        assert "round_day_quota" in {r.constraint_id for r in candidate.score.hard_results()}


def test_cpsat_never_breaks_the_day_quota():
    pytest.importorskip("ortools")
    from terminliste.solvers.cpsat import CpSatScheduler

    quota = {"friday": 0, "saturday": 1, "sunday": 2, "monday": 1}
    world, league = _league(n_teams=8, rounds_per_pairing=1, day_quota=quota)
    season = _season(end=date(2026, 9, 30))

    result = CpSatScheduler().solve(_request(world, season, league))

    assert result.candidates
    for candidate in result.candidates:
        for round_index, counts in _week_counts(candidate.matches).items():
            if round_index in league.day_quota_exempt_rounds:
                continue
            for weekday, limit in quota.items():
                assert counts[weekday] <= limit, (round_index, weekday, counts)


def test_cpsat_plays_the_whole_fixed_round_on_its_date():
    pytest.importorskip("ortools")
    from terminliste.solvers.cpsat import CpSatScheduler

    # A Saturday, so the league's Sunday default can't land on it by chance.
    target = date(2026, 4, 18)
    world, league = _league(fixed_round_dates=[FixedRoundDate(round=2, date=target)])
    season = _season()

    result = CpSatScheduler().solve(_request(world, season, league))

    assert result.candidates
    for candidate in result.candidates:
        assert {m.date for m in candidate.matches if m.round_index == 1} == {target}


def test_a_fixed_round_keeps_its_slot_even_when_the_slot_misses_the_quota():
    """The quota exempts fixed-date rounds, so a slot holding one stays usable."""
    target = date(2026, 4, 18)
    quota = {"friday": 0, "saturday": 2, "sunday": 2, "monday": 0}
    world, league = _league(
        n_teams=8, day_quota=quota, fixed_round_dates=[FixedRoundDate(round=2, date=target)]
    )
    # Without its Sunday, the 17-20 Apr weekend can host only 2 of the 4 matches.
    season = _season(
        end=date(2026, 9, 30), global_blackouts=[DatedNote(date=date(2026, 4, 19), reason="blocked")]
    )

    plan = _plan(world, season, league)

    assert target in plan.chosen[1].days


# -- schema and loader checks ---------------------------------------------------


def test_phases_must_add_up_to_the_leagues_rounds():
    world, _ = _league()
    _, league = _league(phases=[LeaguePhase(kind="regular", rounds=5)])
    world.competitions["league"] = league

    assert any("regular-phase rounds" in e for e in validate_world(world))


def test_the_shipped_world_validates(world):
    assert validate_world(world) == []


def test_fixed_rounds_must_be_in_date_order():
    world, _ = _league()
    _, league = _league(
        fixed_round_dates=[
            FixedRoundDate(round=1, date=date(2026, 4, 19)),
            FixedRoundDate(round=2, date=date(2026, 4, 12)),
        ]
    )
    world.competitions["league"] = league

    assert any("is not after" in e for e in validate_world(world))
