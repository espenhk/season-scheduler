"""Renders a solver result as one self-contained HTML page.

No external assets: the CSS and the tab-switching script are inlined, so the
file opens from disk, survives being emailed, and renders the same in a year.

The page exists to answer one question — *which of these three schedules should
we use?* — so it leads with the score, then what each option is winning and
losing, then the calendar itself.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..model.loader import World
from ..model.schema import WEEKDAYS, Match, Season
from ..rounds.cup_schedule import NEUTRAL_CUP_VENUE, CupRoundPlacement, CupSchedule
from ..scoring.base import ConstraintResult, Event, Score
from ..scoring.registry import describe
from ..solvers.base import Candidate, SolverResult

TEMPLATE_DIR = Path(__file__).parent / "templates"

# One (text, background) pair per competition, cycled in sorted-id order so
# the assignment is stable within a render — used to colour-code the league
# tag in the combined calendar/list views (issue #61) so a reader can tell
# Eliteserien, Toppserien and each cup apart at a glance. Kept out of the
# data files: it's a report-presentation detail, not something a competition
# needs to declare about itself.
_COMPETITION_PALETTE = [
    {"fg": "#2471a3", "bg": "#eaf2f8"},  # blue
    {"fg": "#8e44ad", "bg": "#f5eef8"},  # purple
    {"fg": "#b9770e", "bg": "#fdf6ea"},  # amber
    {"fg": "#117864", "bg": "#e8f6f3"},  # teal
    {"fg": "#a04000", "bg": "#fdf2e9"},  # rust
]
_DEFAULT_COMP_COLOR = {"fg": "var(--muted)", "bg": "var(--line)"}


def _json_attr(value: object) -> str:
    """JSON, pre-escaped for embedding in a double-quoted HTML attribute.

    `season.html.j2`'s name doesn't end in `.html` (Jinja's `select_autoescape`
    matches on the template name's own extension, and this one's is `.j2`), so
    the environment's autoescaping never actually applies to it — every other
    value this template prints is plain trusted data (club/team/rule names
    from our own YAML), so that's gone unnoticed, but `json.dumps` output is
    full of literal double quotes that would otherwise terminate the
    attribute early. Escaping by hand here, rather than fixing autoescaping
    project-wide, keeps the fix scoped to the one thing that actually needs
    it.
    """
    return html.escape(json.dumps(value), quote=True)


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

    # One colour per club (see `Club.color` in model/schema.py), used to fill
    # every team's marker regardless of whether it belongs to a dual club —
    # the dual clubs additionally drive the back-to-back-home-day pairing
    # feature below, which is a separate concern from colour.
    club_colors = {club.id: club.color for club in world.clubs.values()}
    dual_club_ids = {c.id for c in world.dual_clubs()}
    competition_colors = _competition_colors(world)
    cup_schedules = cup_schedules or []

    options = [
        _build_option(
            world,
            season,
            candidate,
            club_colors,
            dual_club_ids,
            cup_schedules,
            competition_colors,
            full_diagnostics,
        )
        for candidate in result.candidates
    ]

    html = template.render(
        title=title or f"{season.year} season schedule",
        season=season,
        result=result,
        options=options,
        club_colors=club_colors,
        all_clubs=sorted(world.clubs.values(), key=lambda c: c.name),
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
    club_colors: dict[str, str],
    dual_club_ids: set[str],
    cup_schedules: list[CupSchedule],
    competition_colors: dict[str, dict],
    full_diagnostics: bool = False,
) -> dict:
    score = candidate.score
    limit = 200 if full_diagnostics else 4
    entries = _match_entries(world, candidate, club_colors, dual_club_ids, competition_colors)
    headlines = _headlines(world, candidate)
    club_headlines = _club_headlines(world, candidate, dual_club_ids)
    return {
        "label": candidate.label,
        "seed": candidate.seed,
        "full": full_diagnostics,
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "soft_total": score.soft_total,
        "points": score.points,
        "headlines": headlines,
        # JSON twins of the same two headline shapes, embedded as data
        # attributes so the club filter can swap between them client-side
        # without a page reload (issue #61) — see the `.headlines` element in
        # the template and its `applyClubFilter` script.
        "headlines_json": _json_attr(headlines),
        "club_headlines_json": _json_attr(club_headlines),
        "hard_violation_detail": (
            [_result_row(r, world, full=True) for r in score.hard_results() if r.count]
            if full_diagnostics
            else []
        ),
        "problems": [_result_row(r, world, full=full_diagnostics) for r in score.biggest_problems(limit=limit)],
        "upsides": [_result_row(r, world, full=full_diagnostics) for r in score.biggest_upsides(limit=limit)],
        "breakdown": [_result_row(r, world, full=True) for r in _ordered_results(score)],
        "fairness": _fairness_rows(world, candidate),
        "by_competition": _competition_views(world, entries) + _cup_views(world, cup_schedules, club_colors),
        "combined_calendar": _combined_calendar_view(entries),
        "combined_list": entries,
    }


def _ordered_results(score: Score) -> list[ConstraintResult]:
    """Hard rules first, then soft ordered by how much they moved the score."""
    hard = sorted(score.hard_results(), key=lambda r: (r.count == 0, r.constraint_id))
    soft = sorted(score.soft_results(), key=lambda r: -abs(r.total))
    return hard + soft


def _result_row(result: ConstraintResult, world: World, full: bool = False) -> dict:
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
        # Per-team/per-club occurrence counts — what the collapsed "Biggest
        # upsides"/"Biggest problems" entries show instead of the raw example
        # list once expanded (issue #61).
        "occurrences": _entity_counts(world, result),
    }


def _classify_events(world: World, events: list[Event]) -> tuple[list[Event], list[Event]]:
    """Split a rule's team-scoped events into club-coupling events (two teams
    of the same club, e.g. the back-to-back-day pairing rules) and ordinary
    single-team events. Shared by the fairness rows and the per-team
    occurrence summary below, both of which need the same split."""
    club_events: list[Event] = []
    team_events: list[Event] = []
    for event in events:
        if not event.team_ids:
            continue
        clubs = {world.team(t).club_id for t in event.team_ids}
        if len(event.team_ids) > 1 and len(clubs) == 1:
            club_events.append(event)
        else:
            team_events.append(event)
    return club_events, team_events


def _count_by_club(world: World, club_events: list[Event]) -> dict[str, int]:
    """Tally a list of already-classified club-coupling events by club id.
    Shared by every place that needs this (fairness rows, per-club headlines,
    the per-team occurrence summary) so the attribution logic — which event's
    `team_ids[0]` names the club it belongs to — lives in exactly one place.
    """
    counts: dict[str, int] = defaultdict(int)
    for event in club_events:
        counts[world.team(event.team_ids[0]).club_id] += 1
    return counts


def _entity_counts(world: World, result: ConstraintResult) -> list[dict]:
    """How many of this rule's events touch each team (or each dual club, for
    the two club-coupling rules), worst/most-frequent first. Returns `[]` for
    a rule whose events carry no `team_ids` at all (e.g. `preferred_weekday`,
    scored per competition rather than per team).

    Picks whichever of the two event shapes is actually larger, the same way
    `_fairness_row_for` does — a rule could in principle mix both shapes, and
    picking consistently keeps the two report sections from disagreeing about
    which bucket "the" occurrences for that rule come from.
    """
    club_events, team_events = _classify_events(world, result.events)
    if not club_events and not team_events:
        return []
    counts: dict[str, int] = defaultdict(int)
    if len(club_events) >= len(team_events):
        for club_id, n in _count_by_club(world, club_events).items():
            counts[world.club(club_id).name] = n
    else:
        for event in team_events:
            for team_id in event.team_ids:
                counts[world.team_label(team_id)] += 1
    return [
        {"label": label, "count": n}
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _competition_colors(world: World) -> dict[str, dict]:
    """One (text, background) colour pair per competition id, stable within a
    render — see `_COMPETITION_PALETTE` above."""
    return {
        competition_id: _COMPETITION_PALETTE[i % len(_COMPETITION_PALETTE)]
        for i, competition_id in enumerate(sorted(world.competitions))
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


def _club_event_counts(world: World, result: ConstraintResult | None) -> dict[str, int]:
    if result is None:
        return {}
    club_events, _ = _classify_events(world, result.events)
    return _count_by_club(world, club_events)


def _club_headlines(world: World, candidate: Candidate, dual_club_ids: set[str]) -> dict[str, list[dict]]:
    """The same three headline numbers as `_headlines`, narrowed to one club's
    own matches — what the calendar's club filter shows in place of the
    season-wide totals once a club is selected (issue #61). Keyed by club id
    so the template's script can look a selection up directly; consumed as
    JSON (see `club_headlines_json` in `_build_option`), never rendered
    server-side, since which club is picked happens entirely client-side.
    """
    score = candidate.score
    club_matches: dict[str, list[Match]] = defaultdict(list)
    for match in candidate.matches:
        home_club = world.team(match.home_team).club_id
        away_club = world.team(match.away_team).club_id
        club_matches[home_club].append(match)
        if away_club != home_club:
            club_matches[away_club].append(match)

    home_pairs = _club_event_counts(world, score.result("consecutive_home_days"))
    away_pairs = _club_event_counts(world, score.result("consecutive_away_days"))

    headlines: dict[str, list[dict]] = {}
    for club_id, matches in club_matches.items():
        on_day = sum(
            1
            for m in matches
            if m.date.weekday() == WEEKDAYS.index(world.competition(m.competition_id).preferred_weekday)
        )
        rows = [
            {"value": str(len(matches)), "label": "matches"},
            {
                "value": f"{100.0 * on_day / len(matches):.0f}%" if matches else "—",
                "label": "on the preferred weekday",
            },
        ]
        if club_id in dual_club_ids:
            rows.append({"value": str(home_pairs.get(club_id, 0)), "label": "back-to-back home days"})
            rows.append(
                {"value": str(away_pairs.get(club_id, 0)), "label": "paired away days within travel range"}
            )
        headlines[club_id] = rows
    return headlines


def _match_entries(
    world: World,
    candidate: Candidate,
    club_colors: dict[str, str],
    dual_club_ids: set[str],
    competition_colors: dict[str, dict] | None = None,
) -> list[dict]:
    """One flat, chronologically sorted list of every match this option plays,
    with everything both the per-competition view and the combined view need.

    Built once per option so the two views agree by construction — there is
    no second pass over the raw matches that could compute `paired` or a
    club's colour differently.
    """
    matches = candidate.matches
    competition_colors = competition_colors or {}

    # Dates on which a dual club has a home match, so the calendar can flag the
    # back-to-back pairs that justify scheduling the leagues together.
    home_by_club: dict[str, set[date]] = defaultdict(set)
    for match in matches:
        club_id = world.team(match.home_team).club_id
        if club_id in dual_club_ids:
            home_by_club[club_id].add(match.date)

    entries: list[dict] = []
    for match in sorted(matches, key=lambda m: (m.date, m.competition_id, m.home_team)):
        home_club = world.team(match.home_team).club_id
        away_club = world.team(match.away_team).club_id
        paired = home_club in dual_club_ids and _has_adjacent_home(
            home_by_club[home_club], match.date
        )
        competition = world.competition(match.competition_id)
        comp_color = competition_colors.get(match.competition_id, _DEFAULT_COMP_COLOR)
        entries.append(
            {
                "date": match.date,
                "weekday": match.date.strftime("%a"),
                "kickoff_time": match.kickoff_time,
                "home": world.team_label(match.home_team),
                "home_short": world.team_short_label(match.home_team),
                "away": world.team_label(match.away_team),
                "away_short": world.team_short_label(match.away_team),
                "venue": world.venue(match.venue).name,
                "color": club_colors.get(home_club, ""),
                "away_color": club_colors.get(away_club, ""),
                "paired": paired,
                "leg": match.leg,
                "round_index": match.round_index,
                "competition_id": match.competition_id,
                "competition_name": competition.name,
                "comp_fg": comp_color["fg"],
                "comp_bg": comp_color["bg"],
                "home_club": home_club,
                "away_club": away_club,
            }
        )
    return entries


def _competition_views(world: World, entries: list[dict]) -> list[dict]:
    by_competition: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        by_competition[entry["competition_id"]].append(entry)

    views: list[dict] = []
    for competition_id, comp_entries in sorted(by_competition.items()):
        competition = world.competition(competition_id)
        rounds: dict[int, list[dict]] = defaultdict(list)
        for entry in comp_entries:
            rounds[entry["round_index"]].append(entry)

        views.append(
            {
                "id": competition_id,
                "name": competition.name,
                "preferred_weekday": competition.preferred_weekday,
                "match_count": len(comp_entries),
                "rounds": [
                    {
                        "index": round_index + 1,
                        "leg": round_entries[0]["leg"],
                        "dates": _round_date_label(round_entries),
                        "matches": round_entries,
                    }
                    for round_index, round_entries in sorted(rounds.items())
                ],
            }
        )
    return views


def _combined_calendar_view(entries: list[dict]) -> list[dict]:
    """Every league's matches merged into one calendar, grouped by ISO week.

    Round numbers don't line up across competitions (Eliteserien and
    Toppserien run different round counts), so week-of-year is the grouping
    that actually interleaves them the way issue #24 asks for.
    """
    weeks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for entry in entries:
        iso_year, iso_week, _ = entry["date"].isocalendar()
        weeks[(iso_year, iso_week)].append(entry)

    views: list[dict] = []
    for (iso_year, iso_week), week_entries in sorted(weeks.items()):
        week_entries = sorted(
            week_entries, key=lambda e: (e["date"], e["competition_name"], e["home"])
        )
        views.append(
            {
                "label": f"Week {iso_week}, {iso_year}",
                "dates": _round_date_label(week_entries),
                "matches": week_entries,
            }
        )
    return views


def _cup_views(
    world: World, cup_schedules: list[CupSchedule], club_colors: dict[str, str]
) -> list[dict]:
    """Cup rounds, in the same shape `_competition_views` uses for leagues.

    Pairings are drawn round by round and unknown ahead of time, so there is
    no home-vs-away fixture list here — the opponent is always "TBD". What we
    do know per round, per entered team, is its own resolved date and which
    side of the (still-undrawn) tie it is on — see `CupRoundPlacement.venue_type`
    and `_venue_type` in `rounds/cup_schedule.py` for the home/away/neutral rule.
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
                        "venue_summary": _cup_venue_summary(placement.venue_type),
                        "note": placement.note,
                        "fixtures": _cup_fixtures(world, placement, club_colors),
                    }
                    for i, placement in enumerate(schedule.rounds)
                ],
            }
        )
    return views


def _cup_fixtures(world: World, placement: CupRoundPlacement, club_colors: dict[str, str]) -> list[dict]:
    """One entry per entered team for a resolved cup round: its own date and
    which side of the undrawn tie it's on, sorted the way a calendar reads —
    by date, then team."""
    fixtures = []
    for team_id, team_date in placement.dates.items():
        club_id = world.team(team_id).club_id
        fixtures.append(
            {
                "team": world.team_short_label(team_id),
                "team_full": world.team_label(team_id),
                "club_id": club_id,
                "color": club_colors.get(club_id, ""),
                "date": team_date,
                "weekday": team_date.strftime("%a"),
                "venue_type": placement.venue_type,
                "venue_label": _cup_venue_label(world, team_id, placement.venue_type),
            }
        )
    return sorted(fixtures, key=lambda fx: (fx["date"], fx["team"]))


def _cup_venue_label(world: World, team_id: str, venue_type: str) -> str:
    if venue_type == "home":
        return world.home_venue_of(team_id).name
    if venue_type == "neutral":
        return f"{NEUTRAL_CUP_VENUE} (neutral)"
    return "Away (opponent's ground)"


def _cup_venue_summary(venue_type: str) -> str:
    if venue_type == "home":
        return "Home leg · opponent drawn later"
    if venue_type == "neutral":
        return f"Final · neutral ground ({NEUTRAL_CUP_VENUE})"
    return "Away leg · opponent drawn later"


def _fairness_rows(world: World, candidate: Candidate) -> list[dict]:
    """Per-team/per-club view of every soft rule that can play favourites, so
    a schedule that showers one team in rewards (or penalties) another never
    sees is visible before the schedule is accepted — see issue #23.

    Discovered from `candidate.score.soft_results()` rather than a maintained
    list of constraint ids: a soft rule earns a row here the moment its
    events carry `team_ids` (see `scoring/base.py::Event`), so a new rule
    added to `soft.py` shows up automatically as long as it tags its events —
    nothing in this file needs to change for it. Reuses the already-computed,
    already-weighted events on `candidate.score` (built with `ctx.detail=True`
    for every rendered candidate — see `solvers/local_search.py::_with_detail`)
    rather than re-deriving counts from the raw matches, so this can never
    disagree with the score itself.
    """
    score = candidate.score
    participant_teams = sorted({t for m in candidate.matches for t in (m.home_team, m.away_team)})
    dual_club_ids = [c.id for c in world.dual_clubs()]

    rows = [
        _fairness_row_for(world, result, dual_club_ids, participant_teams)
        for result in score.soft_results()
    ]
    return [row for row in rows if row is not None]


def _fairness_row_for(
    world: World,
    result: ConstraintResult,
    dual_club_ids: list[str],
    participant_teams: list[str],
) -> dict | None:
    """One rule's events, shaped into a fairness row.

    An event whose `team_ids` name two teams of the *same* club — the
    back-to-back-home/away-day pairing, the only place that happens — is
    attributed to that club once, since it describes something the club's
    two teams did together rather than either one individually. Everything
    else (a single team's own event, or two teams from different clubs, e.g.
    a discouraged-date penalty hitting both the home and away side) is
    attributed to each named team on its own.

    Returns `None` for a rule whose events carry no `team_ids` at all (not
    team-attributable — e.g. `preferred_weekday`, which is scored per
    competition) or whose relevant entity universe has fewer than two
    members to compare.
    """
    club_events, team_events = _classify_events(world, result.events)

    if not club_events and not team_events:
        return None

    # In practice every soft rule's events are consistently one shape or the
    # other — a rule either couples a club's own teams every time or scores
    # individual teams every time, never a mix — but picking whichever
    # bucket actually has events keeps this correct even if that changes.
    if len(club_events) >= len(team_events):
        if len(dual_club_ids) < 2:
            return None
        raw_counts = _count_by_club(world, club_events)
        entries = [(world.club(cid).name, raw_counts.get(cid, 0)) for cid in dual_club_ids]
    else:
        if len(participant_teams) < 2:
            return None
        counts = {team_id: 0 for team_id in participant_teams}
        for event in team_events:
            for team_id in event.team_ids:
                if team_id in counts:
                    counts[team_id] += 1
        entries = [(world.team_label(tid), n) for tid, n in counts.items()]

    return _fairness_row(result.constraint_id, entries)


def _fairness_row(constraint_id: str, entries: list[tuple[str, int]]) -> dict:
    entries = sorted(entries, key=lambda e: -e[1])
    values = [n for _, n in entries]
    lo, hi = min(values), max(values)
    mean = sum(values) / len(values)
    return {
        "id": constraint_id,
        "description": describe(constraint_id),
        "entries": [{"label": label, "count": n} for label, n in entries],
        "min": lo,
        "max": hi,
        # Flagged when the spread between the best- and worst-treated entity
        # is at least as wide as the average count itself (and at least 2, so
        # a single stray event on an otherwise-flat rule doesn't trip it) —
        # the "not zero, and not double" bar from issue #23.
        "flagged": hi > 0 and (hi - lo) >= max(2, round(mean)),
    }


def _has_adjacent_home(days: set[date], day: date) -> bool:
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
                        "kickoff_time": m.kickoff_time,
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
