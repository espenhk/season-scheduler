"""Loading and scoring an externally supplied schedule (the `score` command)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from terminliste.external_schedule import ExternalScheduleError, load_external_schedule
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.registry import build_constraints

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["competition", "date", "home_team", "away_team", "venue"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_loads_a_minimal_csv_schedule(tmp_path, world):
    path = tmp_path / "schedule.csv"
    _write_csv(
        path,
        [
            {"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m", "venue": ""},
            {"competition": "eliteserien_2026", "date": "2026-04-12", "home_team": "viking_m", "away_team": "brann_m", "venue": ""},
        ],
    )
    matches, warnings = load_external_schedule(path, world)
    assert len(matches) == 2
    assert matches[0].venue == "brann_stadion"  # defaulted to the home team's venue
    assert {m.leg for m in matches} == {1, 2}


def test_unknown_team_raises_a_clear_error(tmp_path, world):
    path = tmp_path / "schedule.csv"
    _write_csv(
        path,
        [{"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "not_a_team", "away_team": "viking_m", "venue": ""}],
    )
    with pytest.raises(ExternalScheduleError, match="not_a_team"):
        load_external_schedule(path, world)


def test_missing_csv_column_raises_a_clear_error(tmp_path, world):
    path = tmp_path / "schedule.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["competition", "date", "home_team"])
        writer.writeheader()
        writer.writerow({"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m"})
    with pytest.raises(ExternalScheduleError, match="missing required column"):
        load_external_schedule(path, world)


def test_json_schedule_loads_equivalently(tmp_path, world):
    path = tmp_path / "schedule.json"
    path.write_text(
        json.dumps(
            {
                "matches": [
                    {"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m"},
                ]
            }
        ),
        encoding="utf-8",
    )
    matches, _ = load_external_schedule(path, world)
    assert len(matches) == 1
    assert matches[0].home_team == "brann_m"


def test_json_entry_numbers_in_errors_are_1_based_like_csv(tmp_path, world):
    """A bad second entry should be reported as 'entry 2', matching the
    1-based numbering CSV parsing and _validate_entries both use — not the
    0-based index enumerate() hands back by default."""
    path = tmp_path / "schedule.json"
    path.write_text(
        json.dumps(
            [
                {"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m"},
                {"competition": "eliteserien_2026", "date": "not-a-date", "home_team": "brann_m", "away_team": "viking_m"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExternalScheduleError, match="entry 2"):
        load_external_schedule(path, world)


def test_coverage_warning_flags_a_pair_that_never_meets(tmp_path, world):
    path = tmp_path / "schedule.csv"
    # Only one match from the whole Eliteserien: every other pair is missing.
    _write_csv(
        path,
        [{"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m", "venue": ""}],
    )
    _, warnings = load_external_schedule(path, world)
    assert any("never meet" in w for w in warnings)


def test_coverage_warning_text_is_stable_across_hash_seeds(tmp_path, world):
    """The example pair named in a warning must not move with the hash seed.

    `missing` is a set, so an unordered pick would give different text run to
    run. Harmless while the warning is only printed; not harmless once it is
    committed to a baseline report, where it would show up as a spurious diff
    on every refresh.
    """
    path = tmp_path / "schedule.csv"
    _write_csv(
        path,
        [{"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m", "venue": ""}],
    )
    script = (
        "import sys, json;"
        "sys.path.insert(0, %r);"
        "from pathlib import Path;"
        "from terminliste.model.loader import load_world;"
        "from terminliste.external_schedule import load_external_schedule;"
        "w = load_world(Path(%r));"
        "print(json.dumps(load_external_schedule(Path(%r), w)[1]))"
    ) % (str(PROJECT_ROOT), str(PROJECT_ROOT / "data"), str(path))

    outputs = set()
    for seed in ("0", "1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"warning text varies with PYTHONHASHSEED: {outputs}"


def test_leg_inferred_from_meeting_order_not_input_order(tmp_path, world):
    """The second file lists the away-first meeting before the home-first one;
    leg assignment should still follow chronological date, not row order."""
    path = tmp_path / "schedule.csv"
    _write_csv(
        path,
        [
            {"competition": "eliteserien_2026", "date": "2026-09-01", "home_team": "viking_m", "away_team": "brann_m", "venue": ""},
            {"competition": "eliteserien_2026", "date": "2026-04-05", "home_team": "brann_m", "away_team": "viking_m", "venue": ""},
        ],
    )
    matches, _ = load_external_schedule(path, world)
    by_date = {m.date: m for m in matches}
    early = by_date[__import__("datetime").date(2026, 4, 5)]
    late = by_date[__import__("datetime").date(2026, 9, 1)]
    assert early.leg == 1
    assert late.leg == 2


def test_a_broken_real_schedule_scores_hard_violations(world, season):
    """The whole point of this feature: feed in a schedule with an obvious
    flaw and see it surface as a named hard-rule violation."""
    competitions = [world.competition(c) for c in season.competitions]
    constraints = build_constraints(world, season, competitions)

    matches = [
        # Brann's men's and women's teams share Brann Stadion, so both home on
        # the same day is a venue double booking (club_home_clash only fires
        # when a dual club's teams play at two *different* venues).
        _entry_match(world, "eliteserien_2026", "brann_m", "viking_m", "2026-04-08"),
        _entry_match(world, "toppserien_2026", "brann_w", "stabak_w", "2026-04-08"),
        # Viking play again two days later: min_rest_days (needs 3).
        _entry_match(world, "eliteserien_2026", "kristiansund_m", "viking_m", "2026-04-10"),
    ]
    from terminliste.model.travel import HaversineTravelModel

    ctx = EvalContext(world=world, season=season, travel=HaversineTravelModel(world), detail=True)
    score = evaluate(matches, constraints, ctx)

    assert not score.feasible
    violated_ids = {r.constraint_id for r in score.hard_results() if r.count}
    assert "venue_double_booking" in violated_ids
    assert "min_rest_days" in violated_ids


def _entry_match(world, competition_id, home, away, date_str):
    from datetime import datetime

    from terminliste.model.schema import Match

    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    return Match(
        competition_id=competition_id,
        home_team=home,
        away_team=away,
        leg=1,
        round_index=0,
        date=day,
        venue=world.team(home).home_venue,
    )
