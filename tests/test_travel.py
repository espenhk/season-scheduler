"""Travel time model: symmetry, self-distance, and override precedence."""

from __future__ import annotations

import math

from terminliste.model.travel import HaversineTravelModel, haversine_km


def test_self_distance_is_zero(travel, world):
    venue = next(iter(world.venues))
    assert travel.hours(venue, venue) == 0.0


def test_travel_is_symmetric(travel, world):
    venues = list(world.venues)
    a, b = venues[0], venues[1]
    assert travel.hours(a, b) == travel.hours(b, a)


def test_oslo_bergen_distance_is_roughly_right(world):
    """Great-circle Oslo-Bergen is ~305km; sanity-check the haversine helper
    rather than the curated override, which deliberately replaces this."""
    intility = world.venue("intility_arena")
    brann = world.venue("brann_stadion")
    km = haversine_km(intility, brann)
    assert 280 <= km <= 330


def test_override_takes_precedence_over_computed_distance(world):
    travel = HaversineTravelModel(world)
    # travel_overrides.yml sets Bergen-Oslo to 7.5h by train; the computed
    # great-circle estimate at 60 km/h would give something much smaller.
    overridden = travel.hours("brann_stadion", "intility_arena")
    assert overridden == 7.5


def test_null_override_means_unreachable(world):
    travel = HaversineTravelModel(world)
    hours = travel.hours("romssa_arena", "intility_arena")
    assert math.isinf(hours)


def test_uncovered_pair_falls_back_to_haversine(world):
    travel = HaversineTravelModel(world)
    # Two nearby Oslo venues with no override entry.
    hours = travel.hours("kfum_arena", "intility_arena")
    assert 0 < hours < 2
