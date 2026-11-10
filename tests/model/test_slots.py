"""Slot generation: weekends, declared extras and the compatibility count (#6)."""

from __future__ import annotations

from datetime import date

import factories as f

from terminliste.model.calendar import build_calendar, calendars_by_competition
from terminliste.model.schema import ExtraRoundSlot, GlobalBlackoutRange
from terminliste.model.slots import candidate_slots, max_compatible_slots


def _league(**kwargs):
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    competition = f.competition("league", [t.id for t in teams], **kwargs)
    return f.world(clubs, [f.venue("v1")], [competition]), competition


def _slots(season, world, competition, **kwargs):
    calendar = build_calendar(world, season)
    scoped = calendars_by_competition(calendar, [competition])[competition.id]
    return candidate_slots(season, competition, scoped, **kwargs)


def test_weekend_slots_span_friday_to_monday_with_the_preferred_default_day():
    world, league = _league(preferred_weekday="sunday")
    season = f.season(competitions=["league"], start=date(2026, 3, 1), end=date(2026, 3, 31))

    slots = _slots(season, world, league)

    assert [s.start.weekday() for s in slots] == [4] * len(slots)
    assert all((s.end - s.start).days == 3 for s in slots[1:])
    assert [s.default_day.weekday() for s in slots] == [6] * len(slots)
    assert all(s.kind == "weekend" for s in slots)


def test_a_slot_clipped_by_the_window_keeps_only_its_allowed_days():
    world, league = _league(preferred_weekday="sunday")
    season = f.season(competitions=["league"], start=date(2026, 3, 14), end=date(2026, 3, 22))

    first = _slots(season, world, league)[0]

    assert first.start == date(2026, 3, 13)
    assert first.days == (date(2026, 3, 14), date(2026, 3, 15), date(2026, 3, 16))


def test_a_blacked_out_preferred_day_moves_the_default_to_the_nearest_allowed_day():
    world, league = _league(preferred_weekday="sunday")
    season = f.season(
        competitions=["league"],
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        global_blackouts=[{"date": date(2026, 3, 15), "reason": "x"}],
    )
    slot = next(s for s in _slots(season, world, league) if s.start == date(2026, 3, 13))

    assert date(2026, 3, 15) not in slot.days
    assert slot.default_day in (date(2026, 3, 14), date(2026, 3, 16))


def test_a_scoped_break_removes_whole_slots_for_that_league_only():
    world, league = _league(preferred_weekday="sunday")
    break_ = GlobalBlackoutRange(
        start=date(2026, 3, 20), end=date(2026, 3, 31), competitions=["league"], reason="break"
    )
    season = f.season(
        competitions=["league"],
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        global_blackout_ranges=[break_],
    )

    slots = _slots(season, world, league)

    assert not any(s.start >= date(2026, 3, 20) for s in slots)
    assert any(s.start == date(2026, 3, 13) for s in slots)


def test_slots_holding_a_skipped_date_are_dropped():
    world, league = _league()
    season = f.season(competitions=["league"], start=date(2026, 3, 1), end=date(2026, 3, 31))

    slots = _slots(season, world, league, skip_dates=frozenset({date(2026, 3, 14)}))

    assert not any(s.contains(date(2026, 3, 14)) for s in slots)


def test_declared_extra_slots_are_single_days_scoped_to_their_competitions():
    world, league = _league()
    extras = [
        ExtraRoundSlot(date=date(2026, 3, 18), kind="midweek"),
        ExtraRoundSlot(date=date(2026, 3, 25), kind="holiday", competitions=["other"]),
    ]
    season = f.season(
        competitions=["league"],
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        extra_round_slots=extras,
    )

    extra = [s for s in _slots(season, world, league) if s.kind != "weekend"]

    assert len(extra) == 1
    assert extra[0].kind == "midweek"
    assert extra[0].start == extra[0].end == extra[0].default_day == date(2026, 3, 18)


def test_a_pinned_day_becomes_the_slots_default_day():
    world, league = _league(preferred_weekday="sunday")
    season = f.season(competitions=["league"], start=date(2026, 3, 1), end=date(2026, 3, 31))

    slots = _slots(season, world, league, pinned_days=frozenset({date(2026, 3, 14)}))

    assert next(s for s in slots if s.contains(date(2026, 3, 14))).default_day == date(2026, 3, 14)


def test_overlapping_or_close_slots_do_not_count_together():
    world, league = _league(preferred_weekday="sunday", min_rest_days=2)
    extra = ExtraRoundSlot(date=date(2026, 3, 16), kind="holiday")  # Monday inside a weekend
    season = f.season(
        competitions=["league"],
        start=date(2026, 3, 13),
        end=date(2026, 3, 22),
        extra_round_slots=[extra],
    )
    slots = _slots(season, world, league)

    assert len(slots) == 3  # two weekends and the Monday
    assert max_compatible_slots(slots, league.min_gap_days) == 2


def test_2026_midweek_placeholder_slots_are_marked_unverified(season):
    midweek = [e for e in season.extra_round_slots if e.kind == "midweek"]
    assert len(midweek) == 6
    assert all(not e.verified for e in midweek)
    assert all(e.verified for e in season.extra_round_slots if e.kind == "holiday")
