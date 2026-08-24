"""Renders a solver result as one self-contained HTML page.

No external assets: the CSS and the tab-switching script are inlined, so the
file opens from disk, survives being emailed, and renders the same in a year.

The page exists to answer one question — *which of these three schedules should
we use?* — so it leads with the score, then what each option is winning and
losing, then the calendar itself.
"""

from __future__ import annotations

import calendar
import html
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..model.loader import World
from ..model.schema import WEEKDAYS, Competition, EuropeanRound, Match, Season
from ..rounds.cup_schedule import NEUTRAL_CUP_VENUE, CupRoundPlacement, CupSchedule
from ..rounds.european_schedule import ResolvedLegs
from ..scoring.base import ConstraintResult, Event, Score
from ..scoring.registry import describe
from ..solvers.base import Candidate, SearchStats, SolverResult

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Fallback for a competition with no declared `color` (#77). The loader
# catches this in the shipped data, but a `World` built without it — a test
# factory, say — can still lack one.
_DEFAULT_COMP_COLOR = {"fg": "var(--muted)", "bg": "var(--line)"}


def _tint(hex_color: str, factor: float = 0.85) -> str:
    """Lighten a hex colour toward white, so one declared `Competition.color`
    serves as both the dot/text foreground and a matching tag background."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = (round(c + (255 - c) * factor) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _json_attr(value: object) -> str:
    """JSON, pre-escaped for embedding in a double-quoted HTML attribute.

    `select_autoescape` matches on a template's extension, and `season.html.j2`
    ends in `.j2`, so nothing escapes it automatically. Every other value the
    template prints is trusted YAML text, but `json.dumps` output is full of
    double quotes that would terminate the attribute early.
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
    european_competitions: list[Competition] | None = None,
    resolved_legs: ResolvedLegs | None = None,
) -> Path:
    """Render a solver result (or a single scored schedule) as one HTML page.

    `full_diagnostics=True` is for scoring one externally supplied schedule
    rather than choosing between candidates: every negative-scoring rule is
    shown, and a dedicated section names broken hard rules. `warnings`
    surfaces non-fatal issues found loading that schedule.

    `cup_schedules`, and `european_competitions` + `resolved_legs` (pass both
    or neither), arrive already resolved — the caller resolves once rather
    than this doing it per render.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("season.html.j2")

    # Every team's marker is filled from its club's colour, dual club or not;
    # dual clubs separately drive the back-to-back-home-day pairing below.
    club_colors = {club.id: club.color for club in world.clubs.values()}
    dual_club_ids = {c.id for c in world.dual_clubs()}
    competition_colors = _competition_colors(world)
    cup_schedules = cup_schedules or []
    european_competitions = european_competitions or []
    resolved_legs = resolved_legs or {}

    options = [
        _build_option(
            world,
            season,
            candidate,
            club_colors,
            dual_club_ids,
            cup_schedules,
            european_competitions,
            resolved_legs,
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
        search_stats=_search_stats_view(result.search_stats),
        club_colors=club_colors,
        all_clubs=sorted(world.clubs.values(), key=lambda c: c.name),
        dual_clubs=sorted(world.dual_clubs(), key=lambda c: c.name),
        warnings=warnings or [],
        season_exclusions=_season_exclusions(world, season),
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
    european_competitions: list[Competition],
    resolved_legs: ResolvedLegs,
    competition_colors: dict[str, dict],
    full_diagnostics: bool = False,
) -> dict:
    score = candidate.score
    limit = 200 if full_diagnostics else 4
    entries = _match_entries(world, candidate, club_colors, dual_club_ids, competition_colors)
    cup_entries = _cup_entries(world, cup_schedules, club_colors, competition_colors)
    european_entries = _european_entries(
        world, european_competitions, resolved_legs, club_colors, competition_colors
    )
    combined_entries = entries + cup_entries + european_entries
    headlines = _headlines(world, candidate)
    club_headlines = _club_headlines(world, candidate, dual_club_ids)
    month_calendar, month_calendar_details = _month_calendar_view(combined_entries)
    calendar_legend = _calendar_legend(world, competition_colors)
    return {
        "label": candidate.label,
        "seed": candidate.seed,
        "full": full_diagnostics,
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "soft_total": score.soft_total,
        "points": score.points,
        "headlines": headlines,
        # JSON twins of the headline shapes, embedded as data attributes so
        # the club filter can swap between them without a page reload (#61).
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
        "by_competition": (
            _competition_views(world, entries)
            + _cup_views(world, cup_schedules, club_colors)
            + _european_views(world, european_competitions, resolved_legs, club_colors)
        ),
        "combined_calendar": _combined_calendar_view(combined_entries),
        "combined_list": _combined_list_view(combined_entries),
        "month_calendar": month_calendar,
        "month_calendar_details": month_calendar_details,
        "calendar_legend": calendar_legend,
    }


def _season_exclusions(world: World, season: Season) -> list[dict]:
    """Every date this season blacks out — global and venue-scoped, single
    dates and ranges alike — sorted by start date for the "Season exclusions"
    overview (#33). Merged here rather than in the template, which would
    render four separately-sorted blocks and put every range after every
    single date.
    """
    entries: list[dict] = []
    for blackout in season.global_blackouts:
        entries.append(
            {"start": blackout.date, "end": blackout.date, "reason": blackout.reason, "venue_label": None}
        )
    for blackout_range in season.global_blackout_ranges:
        entries.append(
            {
                "start": blackout_range.start,
                "end": blackout_range.end,
                "reason": blackout_range.reason,
                "venue_label": None,
            }
        )
    for blackout in season.venue_blackouts:
        entries.append(
            {
                "start": blackout.date,
                "end": blackout.date,
                "reason": blackout.reason,
                "venue_label": world.venues[blackout.venue].name
                if blackout.venue in world.venues
                else blackout.venue,
            }
        )
    for blackout_range in season.venue_blackout_ranges:
        entries.append(
            {
                "start": blackout_range.start,
                "end": blackout_range.end,
                "reason": blackout_range.reason,
                "venue_label": world.venues[blackout_range.venue].name
                if blackout_range.venue in world.venues
                else blackout_range.venue,
            }
        )
    entries.sort(key=lambda e: (e["start"], e["end"]))
    return entries


_DIFFICULTY_COPY = {
    "easy": "Easy — most scenarios tried were already viable.",
    "moderate": "Took real search — a large share of scenarios hit a hard rule.",
    "hard": "A hard search — the vast majority of scenarios tried broke a hard rule.",
    "unknown": "",
}


def _search_stats_view(stats: SearchStats) -> dict | None:
    """How hard the search had to work (#34), shaped for the template. `None`
    when nothing was tracked, as on the imported-schedule path, which builds a
    `SolverResult` without running a search."""
    if not stats.investigated:
        return None
    breakdown = [
        {
            "id": constraint_id,
            "description": describe(constraint_id),
            "count": count,
            "pct": 100.0 * count / stats.hard_violation_scenarios if stats.hard_violation_scenarios else 0.0,
        }
        for constraint_id, count in stats.hard_violation_counts.most_common()
    ]
    return {
        "investigated": stats.investigated,
        "feasible": stats.feasible,
        "feasible_pct": 100.0 * stats.feasible_rate,
        "hard_violation_scenarios": stats.hard_violation_scenarios,
        "hard_violation_pct": 100.0 * stats.hard_violation_scenarios / stats.investigated,
        "difficulty": stats.difficulty,
        "difficulty_label": _DIFFICULTY_COPY.get(stats.difficulty, ""),
        "breakdown": breakdown,
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
        # Per-team/per-club occurrence counts, shown instead of the raw
        # example list when a "biggest upsides"/"problems" entry is expanded
        # (#61).
        "occurrences": _entity_counts(world, result),
    }


def _classify_events(world: World, events: list[Event]) -> tuple[list[Event], list[Event]]:
    """Split a rule's team-scoped events into club-coupling ones (two teams of
    the same club, as the back-to-back-day rules produce) and ordinary
    single-team ones."""
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
    """Tally already-classified club-coupling events by club id, so the
    attribution rule — `team_ids[0]` names the club — lives in one place."""
    counts: dict[str, int] = defaultdict(int)
    for event in club_events:
        counts[world.team(event.team_ids[0]).club_id] += 1
    return counts


def _entity_counts(world: World, result: ConstraintResult) -> list[dict]:
    """How many of this rule's events touch each team (or each dual club, for
    the club-coupling rules), most frequent first. `[]` for a rule whose
    events carry no `team_ids`, e.g. `preferred_weekday`.

    Picks whichever event shape is larger, matching `_fairness_row_for`, so
    the two report sections can't disagree about which bucket a mixed-shape
    rule's occurrences come from.
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
    """One (text, background) colour pair per competition id (#77), the
    background a light tint of the same hue so a competition's calendar dot
    and its combined-view tag read as one colour. A competition with no
    `color` is omitted; callers fall back to `_DEFAULT_COMP_COLOR`."""
    return {
        competition.id: {"fg": competition.color, "bg": _tint(competition.color)}
        for competition in world.competitions.values()
        if competition.color
    }


def _calendar_legend(world: World, competition_colors: dict[str, dict]) -> list[dict]:
    """Competition name -> dot colour, for the month calendar's colour key
    (#77). A dot's `title` only surfaces on desktop hover, no help on the
    phone this view targets, so the legend is how a reader learns what a
    colour means."""
    return sorted(
        (
            {
                "name": competition.name,
                "short": competition.short_name,
                "color": competition_colors[competition.id]["fg"],
            }
            for competition in world.competitions.values()
            if competition.id in competition_colors
        ),
        key=lambda row: row["name"],
    )


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
    matches — what the calendar's club filter shows once a club is selected
    (#61). Keyed by club id and consumed as JSON, never rendered server-side,
    since the selection happens client-side.
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
    with everything the per-competition and combined views need.

    Built once per option so the two views agree by construction, with no
    second pass that could compute `paired` or a club's colour differently.
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
                "competition_short": competition.short_name,
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
    """Every league's matches, plus every cup's per-team round dates, merged
    into one calendar, grouped by ISO week.

    Round numbers don't line up across competitions running different round
    counts, so week-of-year is the only grouping that interleaves them (#24).
    The sort key falls back to a cup entry's team name where a league entry
    has `home`, so both shapes interleave by date without a KeyError.
    """
    weeks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for entry in entries:
        iso_year, iso_week, _ = entry["date"].isocalendar()
        weeks[(iso_year, iso_week)].append(entry)

    views: list[dict] = []
    for (iso_year, iso_week), week_entries in sorted(weeks.items()):
        week_entries = sorted(
            week_entries,
            key=lambda e: (e["date"], e["competition_name"], e.get("home", e.get("team", ""))),
        )
        views.append(
            {
                "label": f"Week {iso_week}, {iso_year}",
                "dates": _round_date_label(week_entries),
                "matches": week_entries,
            }
        )
    return views


def _combined_list_view(entries: list[dict]) -> list[dict]:
    """The same merged league + cup entries as `_combined_calendar_view`, as
    one flat chronological list rather than grouped by week."""
    return sorted(
        entries,
        key=lambda e: (e["date"], e["competition_id"], e.get("home", e.get("team", ""))),
    )


def _month_calendar_view(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """A month-grid calendar (#77): one cell per day, with at most one dot per
    competition that has something on it.

    Returns `(months, day_details)` — the grid itself, one entry per month
    spanned, each a list of Monday-first weeks; and every date with an entry,
    for the template to render as hidden blocks a tap reveals. The details are
    keyed by date rather than inlined, so a day appearing as both an in-month
    cell and the next month's padding isn't rendered twice.
    """
    if not entries:
        return [], []

    by_day: dict[date, list[dict]] = defaultdict(list)
    for entry in entries:
        by_day[entry["date"]].append(entry)

    months: list[dict] = []
    cursor = date(min(by_day).year, min(by_day).month, 1)
    last = date(max(by_day).year, max(by_day).month, 1)
    while cursor <= last:
        months.append(_month_grid(cursor, by_day))
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)

    day_details = [
        {
            "date_iso": day.isoformat(),
            "label": day.strftime("%A %d %B %Y"),
            # Earliest kickoff first; an entry without one — every cup and
            # European entry — sorts after the timed ones, not before them.
            "matches": sorted(
                day_entries,
                key=lambda e: (
                    e.get("kickoff_time") or "99:99",
                    e["competition_name"],
                    e.get("home", e.get("team", "")),
                ),
            ),
        }
        for day, day_entries in sorted(by_day.items())
    ]
    return months, day_details


def _month_grid(month_start: date, by_day: dict[date, list[dict]]) -> dict:
    """One calendar month's Monday-first weeks, each day carrying its own
    deduped competition dots. Padding days from neighbouring months are kept
    (`in_month=False`) so every week is a full row."""
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month):
        weeks.append(
            [
                {
                    "date_iso": day.isoformat(),
                    "day_num": day.day,
                    "in_month": day.month == month_start.month,
                    "dots": _day_dots(by_day.get(day, [])),
                }
                for day in week
            ]
        )
    return {"label": month_start.strftime("%B %Y"), "weeks": weeks}


def _day_dots(day_entries: list[dict]) -> list[dict]:
    """One dot per competition present on the day (#77), however many of its
    matches fall on it — name and colour come from the first entry seen, since
    every entry for the same competition carries the same ones. `club_ids` is
    the union of every club playing it that day, which the template puts on
    the dot's `data-clubs` so the report's club filter can reach the
    calendar."""
    names: dict[str, str] = {}
    colors: dict[str, str] = {}
    club_ids: dict[str, set[str]] = defaultdict(set)
    for entry in day_entries:
        competition_id = entry["competition_id"]
        names.setdefault(competition_id, entry["competition_name"])
        colors.setdefault(competition_id, entry["comp_fg"])
        club_ids[competition_id].update(_entry_club_ids(entry))
    return [
        {"name": names[cid], "color": colors[cid], "club_ids": sorted(club_ids[cid])}
        for cid in sorted(names, key=lambda cid: names[cid])
    ]


def _entry_club_ids(entry: dict) -> set[str]:
    """The club id(s) a combined-view entry belongs to, regardless of which
    of the three entry shapes it is: a league match carries `home_club`/
    `away_club`, a cup or European entry carries `club_id`."""
    if "home_club" in entry:
        return {entry["home_club"], entry["away_club"]}
    return {entry["club_id"]}


def _cup_views(
    world: World, cup_schedules: list[CupSchedule], club_colors: dict[str, str]
) -> list[dict]:
    """Cup rounds, in the same shape `_competition_views` uses for leagues.

    Pairings are drawn round by round, so there is no home-vs-away fixture
    list and the opponent is always "TBD". What is known per entered team is
    its own resolved date and which side of the undrawn tie it is on.
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


def _cup_entries(
    world: World,
    cup_schedules: list[CupSchedule],
    club_colors: dict[str, str],
    competition_colors: dict[str, dict],
) -> list[dict]:
    """Cup rounds at `_cup_fixtures`' granularity — one row per entered team
    per round — so the combined views can merge them with league entries.
    Tagged `is_cup` so the template picks the TBD-opponent card.
    """
    entries: list[dict] = []
    for schedule in cup_schedules:
        comp_color = competition_colors.get(schedule.competition_id, _DEFAULT_COMP_COLOR)
        for placement in schedule.rounds:
            for team_id, team_date in placement.dates.items():
                club_id = world.team(team_id).club_id
                entries.append(
                    {
                        "is_cup": True,
                        "date": team_date,
                        "weekday": team_date.strftime("%a"),
                        "competition_id": schedule.competition_id,
                        "competition_name": schedule.competition_name,
                        "competition_short": schedule.competition_short_name,
                        "comp_fg": comp_color["fg"],
                        "comp_bg": comp_color["bg"],
                        "round_name": placement.round_name,
                        "team": world.team_short_label(team_id),
                        "team_full": world.team_label(team_id),
                        "club_id": club_id,
                        "color": club_colors.get(club_id, ""),
                        "venue_type": placement.venue_type,
                        "venue_label": _cup_venue_label(world, team_id, placement.venue_type),
                    }
                )
    return entries


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


def _european_views(
    world: World,
    european_competitions: list[Competition],
    resolved_legs: ResolvedLegs,
    club_colors: dict[str, str],
) -> list[dict]:
    """European qualifying rounds, in the shape `_cup_views` uses for cups.

    The difference: UEFA draws pairings well ahead, so an opponent is often
    known and shown directly, "TBD" only where the tie still hangs on another
    result. Home/away likewise comes straight from `EuropeanTie.home_leg`
    rather than being inferred as a cup's `venue_type` is.
    """
    views: list[dict] = []
    for competition in sorted(european_competitions, key=lambda c: c.id):
        views.append(
            {
                "id": competition.id,
                "name": competition.name,
                "is_european": True,
                "is_main_tournament": competition.is_main_tournament,
                "match_count": len(competition.european_rounds),
                "rounds": [
                    {
                        "index": i + 1,
                        "name": round_.name,
                        "note": round_.note,
                        "dates": _european_round_date_label(
                            resolved_legs[(competition.id, round_.id)]
                        ),
                        "fixtures": _european_fixtures(
                            world,
                            round_,
                            resolved_legs[(competition.id, round_.id)],
                            club_colors,
                        ),
                    }
                    for i, round_ in enumerate(competition.european_rounds)
                ],
            }
        )
    return views


def _round_legs(first_date: date, second_date: date) -> list[tuple[str | None, date]]:
    """The real matches a resolved round's two leg dates represent: two
    labelled entries for a genuine tie, or one unlabelled entry when both
    legs share a date, which means a single match rather than a tie.

    A qualifying round's legs never share a UEFA date, so this only collapses
    a main tournament's adapted single-date rounds — otherwise a league-phase
    matchday would emit "first leg" and "second leg" rows for one match.
    """
    if first_date == second_date:
        return [(None, first_date)]
    return [("first", first_date), ("second", second_date)]


def _european_fixtures(
    world: World,
    round_: EuropeanRound,
    leg_dates: tuple[date, date],
    club_colors: dict[str, str],
) -> list[dict]:
    """One entry per entered team per leg for a resolved European round:
    its own date, opponent (or "TBD"), and home/away where known — the
    European counterpart of `_cup_fixtures`."""
    first_date, second_date = leg_dates
    fixtures: list[dict] = []
    for tie in round_.ties:
        club_id = world.team(tie.team).club_id
        for leg_label, leg_date in _round_legs(first_date, second_date):
            venue_type = _european_venue_type(tie.home_leg, leg_label)
            fixtures.append(
                {
                    "team": world.team_short_label(tie.team),
                    "team_full": world.team_label(tie.team),
                    "club_id": club_id,
                    "color": club_colors.get(club_id, ""),
                    "date": leg_date,
                    "weekday": leg_date.strftime("%a"),
                    "opponent": tie.opponent,
                    "leg_label": f"{leg_label} leg" if leg_label else "",
                    "venue_type": venue_type,
                    "venue_label": _european_venue_label(world, tie.team, venue_type),
                }
            )
    return sorted(fixtures, key=lambda fx: (fx["date"], fx["team"]))


def _european_entries(
    world: World,
    european_competitions: list[Competition],
    resolved_legs: ResolvedLegs,
    club_colors: dict[str, str],
    competition_colors: dict[str, dict],
) -> list[dict]:
    """European rounds at `_european_fixtures`' granularity — one row per
    entered team per leg — so the combined views can merge them with league
    and cup entries. Tagged `is_european` so the template picks the
    opponent-aware card.
    """
    entries: list[dict] = []
    for competition in european_competitions:
        comp_color = competition_colors.get(competition.id, _DEFAULT_COMP_COLOR)
        for round_ in competition.european_rounds:
            first_date, second_date = resolved_legs[(competition.id, round_.id)]
            for tie in round_.ties:
                club_id = world.team(tie.team).club_id
                for leg_label, leg_date in _round_legs(first_date, second_date):
                    venue_type = _european_venue_type(tie.home_leg, leg_label)
                    round_name = f"{round_.name} ({leg_label} leg)" if leg_label else round_.name
                    entries.append(
                        {
                            "is_european": True,
                            "date": leg_date,
                            "weekday": leg_date.strftime("%a"),
                            "competition_id": competition.id,
                            "competition_name": competition.name,
                            "competition_short": competition.short_name,
                            "comp_fg": comp_color["fg"],
                            "comp_bg": comp_color["bg"],
                            "round_name": round_name,
                            "team": world.team_short_label(tie.team),
                            "team_full": world.team_label(tie.team),
                            "club_id": club_id,
                            "color": club_colors.get(club_id, ""),
                            "opponent": tie.opponent,
                            "venue_type": venue_type,
                            "venue_label": _european_venue_label(world, tie.team, venue_type),
                        }
                    )
    return entries


def _european_venue_type(home_leg: str | None, leg_label: str | None) -> str:
    """`"home"`/`"away"` read straight from `EuropeanTie.home_leg`, or
    `"unknown"` when that isn't settled — distinct from a cup round's
    `"neutral"`, which means a final at a fixed neutral ground."""
    if home_leg is None:
        return "unknown"
    return "home" if home_leg == leg_label else "away"


def _european_venue_label(world: World, team_id: str, venue_type: str) -> str:
    if venue_type == "home":
        return world.home_venue_of(team_id).name
    if venue_type == "away":
        return "Away (opponent's ground)"
    return "Home/away not yet confirmed"


def _european_round_date_label(leg_dates: tuple[date, date]) -> str:
    first_date, second_date = leg_dates
    if first_date == second_date:
        return first_date.strftime("%d %b %Y")
    return f"{first_date.strftime('%d %b')} – {second_date.strftime('%d %b %Y')}"


def _fairness_rows(world: World, candidate: Candidate) -> list[dict]:
    """Per-team/per-club view of every soft rule that can play favourites, so
    a schedule showering one team in rewards another never sees is visible
    before it's accepted (#23).

    Discovered from `candidate.score.soft_results()` rather than a maintained
    id list: a rule earns a row the moment its events carry `team_ids`, so a
    new one in `soft.py` shows up here with no change. Reuses the already
    weighted events on `candidate.score` rather than re-deriving counts, so
    this can never disagree with the score itself.
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

    An event naming two teams of the *same* club — only the back-to-back-day
    pairings do — is attributed to that club once, since it describes what the
    two teams did together. Everything else is attributed to each named team
    individually.

    `None` for a rule whose events carry no `team_ids` (e.g.
    `preferred_weekday`, scored per competition) or whose entity universe has
    fewer than two members to compare.
    """
    club_events, team_events = _classify_events(world, result.events)

    if not club_events and not team_events:
        return None

    # Every soft rule's events are consistently one shape today, but picking
    # whichever bucket has events keeps this correct if that changes.
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
        # Flagged when best-to-worst spread is at least as wide as the average
        # count, and at least 2, so one stray event on an otherwise-flat rule
        # doesn't trip it (#23).
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
    stats = result.search_stats
    payload = {
        "solver": result.solver,
        "iterations": result.iterations,
        "elapsed_s": round(result.elapsed_s, 2),
        "notes": result.notes,
        "search_stats": {
            "investigated": stats.investigated,
            "feasible": stats.feasible,
            "hard_violation_scenarios": stats.hard_violation_scenarios,
            "feasible_rate": round(stats.feasible_rate, 4),
            "difficulty": stats.difficulty,
            "hard_violation_counts": dict(stats.hard_violation_counts.most_common()),
        },
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
