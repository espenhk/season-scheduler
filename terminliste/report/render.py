"""Renders a solver result as one self-contained HTML page.

No external assets: the CSS and the tab-switching script are inlined, so the
file opens from disk, survives being emailed, and renders the same in a year.

The page exists to answer one question — *which of these three schedules should
we use?* — so it leads with the score, then what each option is winning and
losing, then the calendar itself.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..model.loader import World
from ..model.schema import Match, Season
from ..rounds.cup_schedule import CupSchedule
from ..scoring.base import ConstraintResult, Score
from ..scoring.registry import describe
from ..solvers.base import Candidate, SolverResult

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Distinct hues for the clubs that field two teams — the ones whose coupling
# the whole exercise is about. Everyone else stays neutral so the dual clubs
# stand out in the calendar grid.
DUAL_CLUB_COLORS = [
    "#c0392b",
    "#2471a3",
    "#1e8449",
    "#b9770e",
    "#7d3c98",
    "#117a8b",
]


def render_report(
    world: World,
    season: Season,
    result: SolverResult,
    output_path: Path,
    title: str | None = None,
    full_diagnostics: bool = False,
    warnings: list[str] | None = None,
    cup_schedules: list[CupSchedule] | None = None,
) -> Path:
    """Render a solver result (or a single scored schedule) as one HTML page.

    `full_diagnostics=True` is for scoring one externally supplied schedule
    rather than choosing between solver candidates: every negative-scoring
    rule is shown, not just the top few, and a dedicated section calls out
    broken hard rules by name. `warnings` surfaces non-fatal issues found while
    loading that schedule (e.g. a pair that never played) above the score.
    `cup_schedules` are the season's cups already resolved to a date per team
    (see `rounds/cup_schedule.py`) — the caller resolves them once and passes
    the result in, rather than this function resolving them again per render.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("season.html.j2")

    club_colors = {
        club.id: DUAL_CLUB_COLORS[i % len(DUAL_CLUB_COLORS)]
        for i, club in enumerate(sorted(world.dual_clubs(), key=lambda c: c.id))
    }
    cup_schedules = cup_schedules or []

    options = [
        _build_option(world, season, candidate, club_colors, cup_schedules, full_diagnostics)
        for candidate in result.candidates
    ]

    html = template.render(
        title=title or f"{season.year} season schedule",
        season=season,
        result=result,
        options=options,
        club_colors=club_colors,
        dual_clubs=sorted(world.dual_clubs(), key=lambda c: c.name),
        warnings=warnings or [],
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _build_option(
    world: World,
    season: Season,
    candidate: Candidate,
    club_colors,
    cup_schedules: list[CupSchedule],
    full_diagnostics: bool = False,
) -> dict:
    score = candidate.score
    limit = 200 if full_diagnostics else 4
    return {
        "label": candidate.label,
        "seed": candidate.seed,
        "full": full_diagnostics,
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "soft_total": score.soft_total,
        "points": score.points,
        "headlines": _headlines(world, candidate),
        "hard_violation_detail": (
            [_result_row(r, full=True) for r in score.hard_results() if r.count]
            if full_diagnostics
            else []
        ),
        "problems": [_result_row(r, full=full_diagnostics) for r in score.biggest_problems(limit=limit)],
        "upsides": [_result_row(r, full=full_diagnostics) for r in score.biggest_upsides(limit=limit)],
        "breakdown": [_result_row(r, full=True) for r in _ordered_results(score)],
        "competitions": _competition_views(world, candidate, club_colors) + _cup_views(cup_schedules),
    }


def _ordered_results(score: Score) -> list[ConstraintResult]:
    """Hard rules first, then soft ordered by how much they moved the score."""
    hard = sorted(score.hard_results(), key=lambda r: (r.count == 0, r.constraint_id))
    soft = sorted(score.soft_results(), key=lambda r: -abs(r.total))
    return hard + soft


def _result_row(result: ConstraintResult, full: bool = False) -> dict:
    return {
        "id": result.constraint_id,
        "kind": result.kind,
        "total": result.total,
        "count": result.count,
        "description": describe(result.constraint_id),
        # A handful of concrete examples: enough to judge whether the number
        # means anything, not so many that the page becomes a log file.
        "examples": [e.detail for e in result.events[: (8 if full else 3)] if e.detail],
        "more": max(0, len([e for e in result.events if e.detail]) - (8 if full else 3)),
    }


def _headlines(world: World, candidate: Candidate) -> list[dict]:
    """The three numbers a reader checks first."""
    score = candidate.score
    matches = candidate.matches
    headlines: list[dict] = []

    weekday = score.result("preferred_weekday")
    if weekday is not None and matches:
        headlines.append(
            {
                "value": f"{100.0 * weekday.count / len(matches):.0f}%",
                "label": "on the preferred weekday",
            }
        )

    home_pairs = score.result("consecutive_home_days")
    if home_pairs is not None:
        headlines.append(
            {"value": str(home_pairs.count), "label": "back-to-back home days"}
        )

    away_pairs = score.result("consecutive_away_days")
    if away_pairs is not None:
        headlines.append(
            {"value": str(away_pairs.count), "label": "paired away days within travel range"}
        )

    return headlines


def _competition_views(world: World, candidate: Candidate, club_colors) -> list[dict]:
    by_competition: dict[str, list[Match]] = defaultdict(list)
    for match in candidate.matches:
        by_competition[match.competition_id].append(match)

    # Dates on which a dual club has a home match, so the calendar can flag the
    # back-to-back pairs that justify scheduling the leagues together.
    home_by_club: dict[str, set[date]] = defaultdict(set)
    for match in candidate.matches:
        club_id = world.team(match.home_team).club_id
        if club_id in club_colors:
            home_by_club[club_id].add(match.date)

    views: list[dict] = []
    for competition_id, matches in sorted(by_competition.items()):
        competition = world.competition(competition_id)
        rounds: dict[int, list[dict]] = defaultdict(list)
        for match in sorted(matches, key=lambda m: (m.date, m.home_team)):
            club_id = world.team(match.home_team).club_id
            paired = club_id in club_colors and _has_adjacent_home(
                home_by_club[club_id], match.date
            )
            rounds[match.round_index].append(
                {
                    "date": match.date,
                    "weekday": match.date.strftime("%a"),
                    "home": world.team_label(match.home_team),
                    "away": world.team_label(match.away_team),
                    "venue": world.venue(match.venue).name,
                    "color": club_colors.get(club_id, ""),
                    "away_color": club_colors.get(world.team(match.away_team).club_id, ""),
                    "paired": paired,
                    "leg": match.leg,
                }
            )

        views.append(
            {
                "id": competition_id,
                "name": competition.name,
                "preferred_weekday": competition.preferred_weekday,
                "match_count": len(matches),
                "rounds": [
                    {
                        "index": round_index + 1,
                        "leg": entries[0]["leg"],
                        "dates": _round_date_label(entries),
                        "matches": entries,
                    }
                    for round_index, entries in sorted(rounds.items())
                ],
            }
        )
    return views


def _cup_views(cup_schedules: list[CupSchedule]) -> list[dict]:
    """Cup rounds, in the same shape `_competition_views` uses for leagues.

    Pairings are drawn round by round and unknown ahead of time, so there is
    no fixture list here — just each round's resolved date span (a single day
    when every team landed on the same one) and how many of the tracked teams
    are still assumed to be in it.
    """
    views: list[dict] = []
    for schedule in sorted(cup_schedules, key=lambda s: s.competition_id):
        team_count = len(schedule.rounds[0].dates) if schedule.rounds else 0
        views.append(
            {
                "id": schedule.competition_id,
                "name": schedule.competition_name,
                "is_cup": True,
                "match_count": team_count,
                "rounds": [
                    {
                        "index": i + 1,
                        "name": placement.round_name,
                        "dates": (
                            placement.earliest_date.strftime("%d %b %Y")
                            if placement.spread_days == 0
                            else (
                                f"{placement.earliest_date.strftime('%d %b')} – "
                                f"{placement.latest_date.strftime('%d %b %Y')}"
                            )
                        ),
                        "team_count": len(placement.dates),
                        "note": placement.note,
                    }
                    for i, placement in enumerate(schedule.rounds)
                ],
            }
        )
    return views


def _has_adjacent_home(days: set[date], day: date) -> bool:
    from datetime import timedelta

    return (day - timedelta(days=1)) in days or (day + timedelta(days=1)) in days


def _round_date_label(entries: list[dict]) -> str:
    days = sorted({e["date"] for e in entries})
    if not days:
        return ""
    if len(days) == 1:
        return days[0].strftime("%d %b")
    return f"{days[0].strftime('%d %b')} – {days[-1].strftime('%d %b')}"


def write_json(result: SolverResult, output_path: Path) -> Path:
    """Machine-readable twin of the HTML, for diffing and test snapshots."""
    payload = {
        "solver": result.solver,
        "iterations": result.iterations,
        "elapsed_s": round(result.elapsed_s, 2),
        "notes": result.notes,
        "options": [
            {
                "label": candidate.label,
                "seed": candidate.seed,
                "hard_violations": candidate.score.hard_violations,
                "soft_total": round(candidate.score.soft_total, 2),
                "points": round(candidate.score.points, 1),
                "breakdown": [
                    {
                        "constraint": r.constraint_id,
                        "kind": r.kind,
                        "total": round(r.total, 2),
                        "count": r.count,
                    }
                    for r in candidate.score.results
                ],
                "matches": [
                    {
                        "date": m.date.isoformat(),
                        "competition": m.competition_id,
                        "round": m.round_index + 1,
                        "leg": m.leg,
                        "home": m.home_team,
                        "away": m.away_team,
                        "venue": m.venue,
                    }
                    for m in candidate.matches
                ],
            }
            for candidate in result.candidates
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path
