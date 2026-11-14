"""Renders a solver result as one self-contained HTML page.

No external assets: the CSS, the script and the fixture data are inlined, so
the file opens from disk, survives being emailed, and renders the same in a
year. Fixtures are encoded once as JSON (`payload.py`) and drawn by the
page's own script, not repeated as markup per view (#42).

The page exists to answer one question — *which of these three schedules should
we use?* — so it leads with the score, then what each option is winning and
losing, then the calendar itself.
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..display import short_date
from ..frame import build_playoff_weeks
from ..model.calendar import build_calendar, calendars_by_competition
from ..model.loader import World
from ..model.schema import WEEKDAYS, Competition, Match, Season
from ..rounds.commitments import CommitmentSet
from ..rounds.cup_schedule import NEUTRAL_CUP_VENUE, CupRoundPlacement, CupSchedule
from ..rounds.european_schedule import ResolvedLegs
from ..scoring.base import (
    INFEASIBLE_CEILING,
    SOFT_SCALE,
    ConstraintResult,
    Event,
    Score,
    points_for_soft_per_match,
)
from ..scoring.registry import (
    BADGE_LABELS,
    COVERAGE_UNITS,
    badge_label,
    describe,
    rule_name,
)
from ..scoring.soft import PreferredWeekday
from ..slot_planner import search_space_log10
from ..solvers.base import Candidate, SearchStats, SolverResult, severity_of
from . import payload
from .contingency_view import contingency_view, maybe_by_day, robustness_view

TEMPLATE_DIR = Path(__file__).parent / "templates"

REPO_URL = "https://github.com/espenhk/season-scheduler"

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
    json_name: str | None = None,
    generated_at: datetime | None = None,
    commitments: CommitmentSet | None = None,
) -> Path:
    """Render a solver result (or a single scored schedule) as one HTML page.

    `full_diagnostics=True` is for scoring one externally supplied schedule
    rather than choosing between candidates: every negative-scoring rule is
    shown, and a dedicated section names broken hard rules. `warnings`
    surfaces non-fatal issues found loading that schedule.

    `cup_schedules`, and `european_competitions` + `resolved_legs` (pass both
    or neither), arrive already resolved — the caller resolves once rather
    than this doing it per render.

    `json_name` is the machine-readable twin written next to the page, linked
    from the header when given. `generated_at` defaults to now (UTC).

    `commitments` word the contingency table's triggers and mark the days a
    team plays only if it advances.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["short_date"] = short_date
    env.filters["standalone_date"] = _standalone_date
    template = env.get_template("season.html.j2")

    # Every team's marker is filled from its club's colour, dual club or not;
    # dual clubs separately drive the back-to-back-home-day pairing below.
    club_colors = {club.id: club.color for club in world.clubs.values()}
    dual_club_ids = {c.id for c in world.dual_clubs()}
    competition_colors = _competition_colors(world)
    cup_schedules = cup_schedules or []
    european_competitions = european_competitions or []
    resolved_legs = resolved_legs or {}

    # Cup and European entries are the same in every option, so they are built
    # (and later encoded) once; league matches follow them in the `uid` space.
    shared_entries = _cup_entries(world, cup_schedules, competition_colors)
    shared_entries += _european_entries(
        world, european_competitions, resolved_legs, competition_colors
    )
    for uid, entry in enumerate(shared_entries):
        entry["uid"] = uid
    shared_views = _cup_views(cup_schedules, shared_entries) + _european_views(
        european_competitions, resolved_legs, shared_entries
    )
    calendar_legend = _calendar_legend(world, competition_colors)
    teams = payload.team_table(world)
    rule_ids = _rule_ids(result.candidates)
    rule_index = {rule_id: i for i, rule_id in enumerate(rule_ids)}
    hard_ids = {r.constraint_id for c in result.candidates for r in c.score.hard_results()}

    options = [
        _build_option(
            world,
            candidate,
            dual_club_ids,
            shared_entries,
            competition_colors,
            teams,
            rule_index,
            full_diagnostics,
            commitments or CommitmentSet(),
        )
        for candidate in result.candidates
    ]

    for option, (points_text, tag) in zip(options, _score_marks(options), strict=True):
        option["points_text"] = points_text
        option["tag"] = tag
    comparison = _comparison_view(world, result.candidates, options, rule_ids)

    competitions = payload.competition_table(
        shared_entries + [e for option in options for e in option["league_entries"]],
        extra=calendar_legend,
    )
    report_data = {
        **payload.shared_tables(
            world,
            club_colors,
            competitions,
            payload.rule_table(rule_ids, hard_ids, frozenset(_INVERSE_RULES)),
            dual_club_ids,
        ),
        "export": {"season": season.id},
        "tint": {"good": TINT_GOOD, "cost": TINT_COST},
        "preferred": _preferred_days(world, competitions),
        # Dates a team plays only if it advances: one line per team and round.
        "maybe": maybe_by_day(world, season, commitments or CommitmentSet()),
        "shared": [payload.shared_entry_wire(entry) for entry in shared_entries],
        "sharedByCompetition": payload.by_competition_wire(shared_views),
        "options": [option["client"] for option in options],
    }

    html = template.render(
        title=title or _default_title(world, season),
        header=_header_view(
            full_diagnostics, generated_at or datetime.now(UTC), json_name
        ),
        season=season,
        result=result,
        banner_notes=[
            {"text": str(n), "severity": severity_of(n)}
            for n in result.notes
            if severity_of(n) != "info"
        ],
        info_notes=[str(n) for n in result.notes if severity_of(n) == "info"],
        data_caveat=_data_caveat(world),
        run_facts=_run_facts(result),
        options=options,
        comparison=comparison,
        search_stats=_search_stats_view(result.search_stats),
        all_clubs=sorted(world.clubs.values(), key=lambda c: c.name),
        warnings=warnings or [],
        season_exclusions=_season_exclusions(world, season),
        slot_views=_slot_views(world, season, result),
        flag_legend=_flag_legend(options, hard_ids),
        description=_description(world, season, result),
        club_key=_club_key(world),
        band_strong=SCORE_STRONG,
        band_fair=SCORE_FAIR,
        tint_good=TINT_GOOD,
        tint_cost=-TINT_COST,
        palette_css=payload.palette_css(club_colors, competitions),
        report_data_json=payload.json_for_script(report_data),
        client_js=(TEMPLATE_DIR / "report.js").read_text(encoding="utf-8"),
        playoff_views=_playoff_views(world, season),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_drop_indentation(html), encoding="utf-8")
    return output_path


_VERBATIM_BLOCK = re.compile(
    r"(<(script|style)\b.*?</\2\b[^>]*>)", re.DOTALL | re.IGNORECASE
)
_INDENT = re.compile(r"\n[ \t]+")


def _drop_indentation(html: str) -> str:
    """The page without its template indentation, about 3% of the page (#42).

    Each newline stays, so rendered whitespace is unchanged; script and style bodies are left as written.
    """
    pieces = _VERBATIM_BLOCK.split(html)
    # split() yields text, block, tag name, text, ...; keep blocks, drop the tag-name groups.
    return "".join(
        piece if i % 3 == 1 else _INDENT.sub("\n", piece)
        for i, piece in enumerate(pieces)
        if i % 3 != 2
    )


def _league_names(world: World, season: Season) -> str:
    names = [world.competition(c).name for c in season.competitions]
    return " & ".join([", ".join(names[:-1]), names[-1]] if len(names) > 2 else names)


def _default_title(world: World, season: Season) -> str:
    return f"{_league_names(world, season)} {season.year} — proposed fixture list"


def _description(world: World, season: Season, result: SolverResult) -> str:
    """The page's meta description (#39)."""
    options = len(result.candidates)
    return (
        f"{_league_names(world, season)} {season.year}: "
        f"{options} scored fixture list{'' if options == 1 else 's'}, "
        "with the rules each one satisfies or bends, and calendar and per-club views."
    )


def _git_short_commit() -> str | None:
    """The checked-out commit, or `None` outside a git checkout (a tarball, a
    container without git)."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _header_view(full_diagnostics: bool, generated_at: datetime, json_name: str | None) -> dict:
    stamp = f"{_standalone_date(generated_at.date())} {generated_at:%H:%M}"
    if generated_at.tzinfo is not None:
        stamp += f" {generated_at.tzname()}"
    return {
        "scoring_only": full_diagnostics,
        "generated": stamp,
        "commit": _git_short_commit(),
        "repo_url": REPO_URL,
        "json_name": json_name,
    }


def _data_caveat(world: World) -> str | None:
    kinds = world.unverified_data
    if not kinds:
        return None
    listed = " and ".join([", ".join(kinds[:-1]), kinds[-1]] if len(kinds) > 2 else kinds)
    return (
        f"{listed.capitalize()} data is marked verified: false (assembled from web search) — "
        "check data/ before relying on this for anything real."
    )


def _run_facts(result: SolverResult) -> dict:
    seeds = sorted({c.seed for c in result.candidates})
    return {
        "solver": result.solver,
        "seeds": seeds if seeds != [0] else [],
        "iterations": result.iterations,
        "elapsed": f"{result.elapsed_s:.1f}s",
    }


def _build_option(
    world: World,
    candidate: Candidate,
    dual_club_ids: set[str],
    shared_entries: list[dict],
    competition_colors: dict[str, dict],
    teams: dict[str, dict],
    rule_index: dict[str, int],
    full_diagnostics: bool = False,
    commitments: CommitmentSet | None = None,
) -> dict:
    score = candidate.score
    limit = 200 if full_diagnostics else 4
    entries = _match_entries(world, candidate, dual_club_ids, competition_colors)
    for uid, entry in enumerate(entries, start=len(shared_entries)):
        entry["uid"] = uid
    combined_entries = entries + shared_entries
    # Rules with at least one fixture to show, and how many.
    pinned = _pinned_fixture_counts(entries)
    headlines = _headlines(world, candidate)
    club_headlines = _club_headlines(world, candidate, dual_club_ids)
    fairness = _fairness_rows(world, candidate)
    month_calendar, month_calendar_details = _month_calendar_view(combined_entries)
    shown_competitions = {entry["competition_id"] for entry in combined_entries}
    gross = _gross_movement(score)
    return {
        "label": candidate.label,
        "calendar_legend": [
            {**row, "cls": payload.competition_class(row["id"])}
            for row in _calendar_legend(world, competition_colors, shown_competitions)
        ],
        "seed": candidate.seed,
        "full": full_diagnostics,
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "soft_total": score.soft_total,
        "points": score.points,
        "band": _score_band(score.points),
        "scoring": _scoring_view(score, gross),
        "headlines": headlines,
        "hard_violation_detail": (
            [_result_row(r, world, pinned, full=True, gross=gross) for r in score.hard_results() if r.count]
            if full_diagnostics
            else []
        ),
        "problems": [
            _result_row(r, world, pinned, full=full_diagnostics, gross=gross)
            for r in score.biggest_problems(limit=limit)
        ],
        "upsides": [
            _result_row(r, world, pinned, full=full_diagnostics, gross=gross)
            for r in score.biggest_upsides(limit=limit)
        ],
        "breakdown": [_result_row(r, world, pinned, full=True, gross=gross) for r in _ordered_results(score)],
        "soft": _soft_summary(score, gross, world, pinned),
        "fairness": fairness,
        "contingency": contingency_view(
            world, candidate.contingency_plan, candidate.matches, commitments or CommitmentSet()
        ),
        "robustness": robustness_view(candidate.robustness),
        "league_entries": entries,
        "client": payload.option_wire(
            entries=entries,
            teams=teams,
            rule_index=rule_index,
            headlines=[{"value": h["value"], "label": h["label"]} for h in headlines],
            club_headlines=club_headlines,
            league_views=_competition_views(world, entries),
            combined_calendar=_combined_calendar_view(combined_entries),
            combined_list=_combined_list_view(combined_entries),
            month_calendar=month_calendar,
            month_calendar_details=month_calendar_details,
            rule_totals={r.constraint_id: r.total for r in score.results},
            gross=gross,
            score_order=[rule_index[r.constraint_id] for r in _ordered_results(score)],
            club_unpinned=_club_unpinned(world, candidate, rule_index),
            club_ranks=_club_ranks(fairness),
            fairness=fairness,
            limit=limit,
            full=full_diagnostics,
        ),
    }


# -- comparing the options (#35) ------------------------------------------------

_COMPARE_LIST_LIMIT = 100


def _score_marks(options: list[dict]) -> list[tuple[str, str | None]]:
    """Each option's score as its tab shows it, and the tag marking the top score.

    Scores that round to the same whole number get a decimal, so 74 and 74
    read as 74.2 and 73.9 rather than looking identical.
    """
    if len(options) < 2:
        return [(f"{option['points']:.0f}", None) for option in options]
    whole = [round(option["points"]) for option in options]
    decimals = 1 if len(set(whole)) < len(whole) else 0
    tenths = [round(option["points"], 1) for option in options]
    top = max(tenths)
    tied = tenths.count(top) > 1
    tag = "tied top score" if tied else "top score"
    return [
        (f"{option['points']:.{decimals}f}", tag if value == top else None)
        for option, value in zip(options, tenths, strict=True)
    ]


def _best(numbers: list[float | None], higher_is_better: bool) -> list[bool]:
    """Which options hold the best value on a row. A row where every option is
    level has none, so nothing is marked."""
    present = [value for value in numbers if value is not None]
    if len(set(present)) < 2:
        return [False] * len(numbers)
    top = max(present) if higher_is_better else min(present)
    return [value is not None and value == top for value in numbers]


def _signed(value: float) -> str:
    return f"{value:+.1f}".replace("-", "−")


def _fixture_identity(match: Match) -> tuple:
    """A league fixture as the same fixture in every option: its competition,
    leg and team pair, whichever side is at home and whatever date it lands on."""
    return (match.competition_id, match.leg, tuple(sorted((match.home_team, match.away_team))))


def _day_label(day: date) -> str:
    return short_date(day)


def _standalone_date(day: date) -> str:
    """A date away from the fixture views, where nothing implies the year: "Sat 22 Aug 2026"."""
    return f"{short_date(day)} {day.year}"


def _fixture_diff(world: World, baseline: list[Match], other: list[Match]) -> dict:
    """How one option's league fixtures differ from the baseline option's.

    A fixture counts once, under the first of date, home side and kickoff time
    that changed. Fixtures absent from the baseline are counted as unmatched.
    """
    base = {_fixture_identity(match): match for match in baseline}
    counts = {"date": 0, "side": 0, "kickoff": 0, "unmatched": 0}
    items: list[dict] = []
    for match in sorted(other, key=lambda m: (m.date, m.competition_id, m.home_team)):
        was = base.get(_fixture_identity(match))
        if was is None:
            kind, detail = "unmatched", "not in the baseline option"
        elif was.date != match.date:
            kind, detail = "date", f"moved from {_day_label(was.date)}"
        elif was.home_team != match.home_team:
            kind, detail = "side", f"{world.team_label(match.home_team)} now at home"
        elif was.kickoff_time != match.kickoff_time:
            kind, detail = "kickoff", f"kickoff {was.kickoff_time or 'unset'} → {match.kickoff_time or 'unset'}"
        else:
            continue
        counts[kind] += 1
        competition = world.competition(match.competition_id)
        fixture = f"{competition.short_name}: {world.team_label(match.home_team)} v {world.team_label(match.away_team)}"
        items.append({"date": _day_label(match.date), "fixture": fixture, "detail": detail})
    parts = [
        f"{counts[kind]} {text}"
        for kind, text in (
            ("date", "on another date (side or kickoff may differ too)"),
            ("side", "with home and away swapped"),
            ("kickoff", "with only the kickoff time changed"),
            ("unmatched", "not in the baseline"),
        )
        if counts[kind]
    ]
    return {
        "count": len(items),
        "total": len(other),
        "breakdown": (", ".join(parts) + ".").capitalize() if parts else "No fixture differs from the baseline.",
        "fixtures": items[:_COMPARE_LIST_LIMIT],
        "more": max(0, len(items) - _COMPARE_LIST_LIMIT),
    }


# A lead or trail is named in an option's summary only when its gap is at least
# this share of the table's largest gap, so rows that barely separate the
# options stay out of the summary.
_SUMMARY_GAP_SHARE = 0.10
# Per option and per side (leads, trails), so the summary stays a line or two.
_SUMMARY_NAMES = 3


def _gap_text(gap: float) -> str:
    digits = 0 if abs(gap) >= 10 else 1
    return f"{gap:+.{digits}f} pts".replace("-", "−")


def _events(count: int) -> str:
    return "1 event" if count == 1 else f"{count} events"


def _gaps(numbers: list[float | None]) -> list[float | None]:
    """Each option's gap to the best of the others: its lead over the next best
    when positive, its deficit to the best when negative."""
    gaps: list[float | None] = []
    for i, value in enumerate(numbers):
        others = [n for j, n in enumerate(numbers) if j != i and n is not None]
        gaps.append(round(value - max(others), 1) if value is not None and others else None)
    return gaps


def _option_summaries(soft: list[tuple[str, list[float | None]]], hard_counts: list[int]) -> list[str]:
    """One summary per option: the rules it leads on and trails on, by how much.

    `soft` is (rule name, soft points per option); `hard_counts` the hard-rule
    violations per option. Hard-rule gaps are counted, not weighed, and are
    always named when they exist.
    """
    count = len(hard_counts)
    soft_gaps = [(name, _gaps(numbers)) for name, numbers in soft]
    largest = max((abs(g) for _, gaps in soft_gaps for g in gaps if g is not None), default=0.0)
    threshold = _SUMMARY_GAP_SHARE * largest
    hard = _gaps([-c for c in hard_counts])  # fewer violations is a lead
    summaries = []
    for i in range(count):
        leads: list[tuple[tuple[int, float], str]] = []
        trails: list[tuple[tuple[int, float], str]] = []
        if hard[i]:
            if hard[i] > 0:
                leads.append(((0, 0.0), f"Hard-rule violations ({int(hard[i])} fewer)"))
            else:
                trails.append(((0, 0.0), f"Hard-rule violations ({int(-hard[i])} more)"))
        for name, gaps in soft_gaps:
            gap = gaps[i]
            if not gap or abs(gap) < threshold:
                continue
            text = f"{name} ({_gap_text(gap)})"
            if gap > 0:
                leads.append(((1, -gap), text))
            else:
                trails.append(((1, gap), text))
        lead_names = [text for _, text in sorted(leads)][:_SUMMARY_NAMES]
        trail_names = [text for _, text in sorted(trails)][:_SUMMARY_NAMES]
        pieces = []
        if lead_names:
            pieces.append("Leads on " + ", ".join(lead_names))
        if trail_names:
            pieces.append("trails on " + ", ".join(trail_names))
        if not pieces:
            summaries.append("No row sets it apart.")
            continue
        sentence = "; ".join(pieces)
        summaries.append(sentence[0].upper() + sentence[1:] + ".")
    return summaries


def _comparison_view(
    world: World, candidates: list[Candidate], options: list[dict], rule_ids: list[str]
) -> dict | None:
    """The side-by-side table of the options, or None with one option.

    A soft rule row shows the soft points that rule adds to each option, so its
    best value is the one that adds most (or costs least). Hard-rule violations
    are the exception: fewer is better. Rules that add nothing in any option
    are listed once, not as rows.
    """
    if len(candidates) < 2:
        return None
    count = len(candidates)
    results = [{r.constraint_id: r for r in c.score.results} for c in candidates]
    kinds: dict[str, str] = {}
    for per_option in results:
        for rule_id, result in per_option.items():
            kinds.setdefault(rule_id, result.kind)

    def contribution(i: int, rule_id: str) -> float | None:
        result = results[i].get(rule_id)
        return None if result is None else round(result.total, 1)

    def row(label, numbers, texts, subs=None, higher_is_better=True) -> dict:
        best = _best(numbers, higher_is_better)
        cells = [
            {"text": texts[i], "sub": subs[i] if subs else "", "best": best[i]} for i in range(count)
        ]
        return {"label": label, "cells": cells, "best": best}

    score_row = row(
        "Score out of 100",
        [round(option["points"], 1) for option in options],
        [option["points_text"] for option in options],
    )

    diffs = [_fixture_diff(world, candidates[0].matches, c.matches) for c in candidates[1:]]
    fixture_row = {
        "label": f"Fixtures that differ from {options[0]['label']}",
        "cells": [{"text": "baseline", "sub": "", "best": False}]
        + [{"text": f"{diff['count']} of {diff['total']}", "sub": "", "best": False} for diff in diffs],
        "best": [False] * count,
    }

    headline_rules: list[str] = []
    headline_labels: dict[str, str] = {}
    for option in options:
        for headline in option["headlines"]:
            if headline["rule"] not in headline_rules:
                headline_rules.append(headline["rule"])
                text = headline["label"]
                headline_labels[headline["rule"]] = text[:1].upper() + text[1:]
    headline_rows = []
    for rule_id in headline_rules:
        shown = [{h["rule"]: h["value"] for h in option["headlines"]}.get(rule_id, "—") for option in options]
        headline_rows.append(
            row(headline_labels[rule_id], [contribution(i, rule_id) for i in range(count)], shown)
        )

    hard_row = row(
        "Hard-rule violations",
        [option["hard_violations"] for option in options],
        [str(option["hard_violations"]) for option in options],
        higher_is_better=False,
    )

    soft_rows = []
    quiet: list[str] = []
    for rule_id in (rule for rule in kinds if kinds[rule] == "soft"):
        numbers = [contribution(i, rule_id) for i in range(count)]
        if all(n in (None, 0.0) for n in numbers):
            quiet.append(rule_name(rule_id))
            continue
        events = [results[i][rule_id].count if rule_id in results[i] else 0 for i in range(count)]
        soft_rows.append(
            (
                max(abs(n) for n in numbers if n is not None),
                row(
                    rule_name(rule_id),
                    numbers,
                    [_signed(n) if n is not None else "—" for n in numbers],
                    subs=[_events(n) if n else "" for n in events],
                ),
                numbers,
            )
        )
    soft_rows.sort(key=lambda entry: (-entry[0], entry[1]["label"]))
    quiet.sort()

    sections = [{"title": "Overall", "rows": [score_row, fixture_row]}]
    if headline_rows:
        sections.append({"title": "Headline numbers", "rows": headline_rows})
    sections.append({"title": "Hard rules", "rows": [hard_row]})
    sections.append({"title": "Soft points by rule, largest movement first", "rows": [e[1] for e in soft_rows]})

    summaries = _option_summaries(
        [(entry[1]["label"], entry[2]) for entry in soft_rows],
        [option["hard_violations"] for option in options],
    )
    columns = []
    for i, option in enumerate(options):
        tag = option["tag"]
        columns.append(
            {
                "index": i,
                "label": option["label"],
                "seed": candidates[i].seed or None,
                "tag": tag,
                "tag_short": None if tag is None else ("top" if tag == "top score" else "tied"),
                "summary": summaries[i],
            }
        )
    return {
        "columns": columns,
        "sections": sections,
        "quiet": quiet,
        "baseline": options[0]["label"],
        "diffs": [{"label": option["label"], **diff} for option, diff in zip(options[1:], diffs, strict=True)],
    }


def _playoff_views(world: World, season: Season) -> list[dict]:
    """The frame's play-off reservations, one entry per play-off round, for
    the "Play-off weeks" overview (#15)."""
    leagues = [world.competition(c) for c in season.competitions]
    grouped: dict[tuple[str, str], list] = {}
    for week in build_playoff_weeks(leagues):
        grouped.setdefault((week.competition_id, week.play_off_id), []).append(week)
    views = []
    for (competition_id, _), weeks in grouped.items():
        first = weeks[0]
        views.append(
            {
                "competition": world.competition(competition_id).name,
                "label": first.label,
                "reserves_league": first.reserves_league,
                "dates": [
                    {"date": w.date, "iso_week": w.iso_week[1]} for w in weeks
                ],
            }
        )
    return sorted(views, key=lambda v: v["dates"][0]["date"])


def _slot_views(world: World, season: Season, result: SolverResult) -> list[dict]:
    """One entry per league for the "Round slots" overview (#6): the slot each
    round sits in and how far the slot choice narrowed the search."""
    if not result.slot_plans:
        return []
    calendars = calendars_by_competition(
        build_calendar(world, season),
        [world.competition(competition_id) for competition_id in result.slot_plans],
    )
    views = []
    for competition_id, plan in result.slot_plans.items():
        competition = world.competition(competition_id)
        league_calendar = calendars[competition_id]
        views.append(
            {
                "name": competition.name,
                "rounds": plan.rounds,
                "candidates": len(plan.candidates),
                "overlapping": plan.overlapping,
                "combinations": plan.combinations,
                "dates_log10": search_space_log10(plan, competition, league_calendar),
                "rows": [
                    {
                        "round": round_index,
                        "kind": slot.kind,
                        "start": slot.start,
                        "end": slot.end,
                        "default_day": slot.default_day,
                        "clashes": [world.team_label(t) for t in slot.clashing_teams],
                    }
                    for round_index, slot in enumerate(plan.chosen, start=1)
                ],
            }
        )
    return views


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
            "name": rule_name(constraint_id),
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


def _pinned_fixture_counts(entries: list[dict]) -> dict[str, int]:
    """How many fixtures each rule is drawn on; a rule absent here has nothing
    to show in the calendar."""
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        for rule_id in {f["rule_id"] for f in entry["flags"] if not f["quiet"]}:
            counts[rule_id] += 1
    return counts


def _gross_movement(score: Score) -> float:
    """Total soft movement, the denominator of every rule's share (#34)."""
    return sum(abs(r.total) for r in score.soft_results())


def _scoring_view(score: Score, gross: float) -> dict:
    """The numbers behind the 0-100 score, so the page can show its working."""
    return {
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "raw_soft": score.soft_total,
        "num_matches": score.num_matches,
        "soft_per_match": score.soft_per_match,
        "soft_scale": SOFT_SCALE,
        "saturation": score.soft_saturation,
        "ceiling": INFEASIBLE_CEILING,
        "points": score.points,
        "gross": gross,
        # Worked examples for the explanation, from the same curve as `points`.
        "example_zero": points_for_soft_per_match(0.0),
        "example_per_match": 5.5,
        "example_points": points_for_soft_per_match(5.5),
    }


def _coverage(result: ConstraintResult) -> dict | None:
    """How many of a rule's opportunities it used, when the rule reports them (#34)."""
    if result.achieved is None or not result.opportunities:
        return None
    return {
        "achieved": result.achieved,
        "opportunities": result.opportunities,
        "unit": COVERAGE_UNITS.get(result.constraint_id, "opportunities"),
        "pct": 100.0 * result.achieved / result.opportunities,
    }


def _soft_summary(score: Score, gross: float, world: World, pinned: dict[str, int]) -> dict:
    """Every soft rule grouped by effect, and how the gains and costs net out."""
    soft = score.soft_results()
    rows = [_result_row(r, world, pinned, full=True, gross=gross) for r in soft]
    helped = sum(r.total for r in soft if r.total > 0)
    hurt = sum(r.total for r in soft if r.total < 0)
    moved = helped - hurt
    return {
        "count": len(soft),
        "helping": sorted((row for row in rows if row["total"] > 0), key=lambda row: -row["total"]),
        "hurting": sorted((row for row in rows if row["total"] < 0), key=lambda row: row["total"]),
        "neutral": [row["name"] for row in rows if row["total"] == 0],
        "helped": helped,
        "hurt": hurt,
        "net": score.soft_total,
        "gain_pct": 100.0 * helped / moved if moved else 0.0,
    }


def _result_row(
    result: ConstraintResult,
    world: World,
    pinned: dict[str, int],
    full: bool = False,
    gross: float = 0.0,
) -> dict:
    # A rule's fixtures are listed by the page from the fixture flags, so only
    # the events that point at no fixture are spelled out here.
    if pinned.get(result.constraint_id):
        result = replace(result, events=[e for e in result.events if not e.match_keys])
    # Soft rules are weighed as a share of the season's total movement, so rules
    # sit on one scale; hard rules are counted, not weighed.
    share = 100.0 * abs(result.total) / gross if result.kind == "soft" and gross else None
    return {
        "id": result.constraint_id,
        "name": rule_name(result.constraint_id),
        "kind": result.kind,
        "total": result.total,
        "share": share,
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
        "pinned": pinned.get(result.constraint_id, 0),
        "coverage": _coverage(result),
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


def _calendar_legend(
    world: World, competition_colors: dict[str, dict], only: set[str] | None = None
) -> list[dict]:
    """Competition name -> dot colour, for the month calendar's colour key
    (#77). A dot's `title` only surfaces on desktop hover, no help on the
    phone this view targets, so the legend is how a reader learns what a
    colour means. `only` keeps the competitions that have a date in view."""
    return sorted(
        (
            {
                "id": competition.id,
                "name": competition.name,
                "short": competition.short_name,
                "color": competition_colors[competition.id]["fg"],
                "bg": competition_colors[competition.id]["bg"],
            }
            for competition in world.competitions.values()
            if competition.id in competition_colors and (only is None or competition.id in only)
        ),
        key=lambda row: row["name"],
    )


def _headlines(world: World, candidate: Candidate) -> list[dict]:
    """The three numbers a reader checks first."""
    score = candidate.score
    matches = candidate.matches
    headlines: list[dict] = []
    # Each headline names its rule: the comparison table reads it to rank the headline.

    weekday = score.result("preferred_weekday")
    if weekday is not None and matches:
        headlines.append(
            {
                "value": f"{100.0 * weekday.count / len(matches):.0f}%",
                "label": "on the preferred weekday",
                "rule": "preferred_weekday",
            }
        )

    home_pairs = score.result("consecutive_home_days")
    if home_pairs is not None:
        headlines.append(
            {"value": str(home_pairs.count), "label": "back-to-back home days", "rule": "consecutive_home_days"}
        )

    away_pairs = score.result("consecutive_away_days")
    if away_pairs is not None:
        headlines.append(
            {
                "value": str(away_pairs.count),
                "label": "paired away days within travel range",
                "rule": "consecutive_away_days",
            }
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


def _club_unpinned(
    world: World, candidate: Candidate, rule_index: dict[str, int]
) -> dict[str, list[list]]:
    """Per club, the events that point at no fixture, as `[rule, total, count]`.

    A club's scorecard figures are otherwise summed from the fixture flags on
    the page; these are the season-level events no fixture carries. Attributed
    as `_event_groups` attributes pinned events, so the two halves agree.
    """
    totals: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for result in candidate.score.results:
        if result.constraint_id in _INVERSE_RULES:
            continue
        rule = rule_index[result.constraint_id]
        club_events, team_events = _classify_events(world, result.events)
        by_club = len(club_events) >= len(team_events)
        for event in club_events if by_club else team_events:
            if event.match_keys:
                continue
            clubs = {world.team(team_id).club_id for team_id in event.team_ids}
            for club_id in clubs:
                entry = totals[club_id][rule]
                entry[0] += event.delta
                entry[1] += 1
    return {
        club_id: [[rule, round(total, 2), count] for rule, (total, count) in sorted(by_rule.items())]
        for club_id, by_rule in totals.items()
    }


def _match_entries(
    world: World,
    candidate: Candidate,
    dual_club_ids: set[str],
    competition_colors: dict[str, dict] | None = None,
) -> list[dict]:
    """One flat, chronologically sorted list of every match this option plays,
    with everything the per-competition and combined views need.

    Built once per option so the views agree by construction, with no second
    pass that could disagree with the score.
    """
    matches = candidate.matches
    competition_colors = competition_colors or {}
    flags = _match_flags(world, candidate)

    entries: list[dict] = []
    for match in sorted(matches, key=lambda m: (m.date, m.competition_id, m.home_team)):
        competition = world.competition(match.competition_id)
        comp_color = competition_colors.get(match.competition_id, _DEFAULT_COMP_COLOR)
        entries.append(
            {
                "date": match.date,
                "kickoff_time": match.kickoff_time,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "venue_id": match.venue,
                "home": world.team_label(match.home_team),
                "flags": flags.get(match.key, []),
                "leg": match.leg,
                "round_index": match.round_index,
                "competition_id": match.competition_id,
                "competition_name": competition.name,
                "competition_short": competition.short_name,
                "comp_fg": comp_color["fg"],
                "comp_bg": comp_color["bg"],
            }
        )
    return entries


# Net points a fixture needs for its card edge to turn green / amber.
TINT_GOOD = 5.0
TINT_COST = -3.0

# Score bands for the gauge beside the total (#39): at or above STRONG is strong,
# at or above FAIR is fair, below is weak.
SCORE_STRONG = 80
SCORE_FAIR = 60


def _score_band(points: float) -> str:
    return "strong" if points >= SCORE_STRONG else "fair" if points >= SCORE_FAIR else "weak"


def _club_key(world: World) -> list[dict]:
    """Every club's short name beside its full name, for the legend's abbreviation key (#39)."""
    return [
        {"short": club.short_name, "name": club.name, "cls": payload.club_class(club.id)}
        for club in sorted(world.clubs.values(), key=lambda c: c.name)
    ]


def _preferred_days(world: World, competition_ids) -> dict[str, int]:
    """Each league's preferred weekday as the browser numbers days (0 = Sunday)."""
    return {
        competition.id: (WEEKDAYS.index(competition.preferred_weekday) + 1) % 7
        for competition in world.competitions.values()
        if competition.id in competition_ids
    }


# Rules whose reward is the common case: their fixtures stay quiet and the
# report badges the exceptions instead (#29).
_INVERSE_RULES = {"preferred_weekday"}


def _rule_ids(candidates: list[Candidate]) -> list[str]:
    """Every rule scored in any candidate, registry order first."""
    seen = {r.constraint_id for c in candidates for r in c.score.results}
    known = [rule_id for rule_id in BADGE_LABELS if rule_id in seen]
    return known + sorted(seen - set(known))


def _match_flags(world: World, candidate: Candidate) -> dict[str, list[dict]]:
    """Each match's scoring flags, from the score's events.

    An event pinned to several matches is split equally between them, so a
    rule's flags add up to its total less its unpinned events. Rules in
    `_INVERSE_RULES` mark their rewarded matches `quiet` and flag the others,
    at no points, instead.
    """
    by_key = {m.key: m for m in candidate.matches}
    flags: dict[str, list[dict]] = defaultdict(list)
    for result in candidate.score.results:
        inverse = result.constraint_id in _INVERSE_RULES
        group_of = _event_groups(world, result)
        for event in result.events:
            for key in event.match_keys:
                flags[key].append(
                    {
                        "rule_id": result.constraint_id,
                        "delta": event.delta / len(event.match_keys),
                        "detail": "" if inverse else event.detail,
                        "shared_by": 1 if inverse else len(event.match_keys),
                        "quiet": inverse,
                        "group": "" if inverse else group_of(event, by_key[key]),
                    }
                )

    for rule_id in _INVERSE_RULES:
        result = candidate.score.result(rule_id)
        if result is None or not result.events:
            continue
        # What an on-day match of each competition earned, so an off-day
        # fixture can say what it misses.
        reward: dict[str, float] = {}
        for event in result.events:
            for key in event.match_keys:
                reward[by_key[key].competition_id] = event.delta / len(event.match_keys)
        for match in candidate.matches:
            competition = world.competition(match.competition_id)
            preferred = getattr(competition, "preferred_weekday", None)
            if preferred is None or match.date.weekday() == WEEKDAYS.index(preferred):
                continue
            missed = reward.get(
                match.competition_id, competition.weights.get(rule_id, PreferredWeekday.weight)
            )
            flags[match.key].append(
                {
                    "rule_id": rule_id,
                    "delta": 0.0,
                    "detail": (
                        f"{match.date.strftime('%A')}; {competition.name} prefers "
                        f"{preferred.capitalize()}" + (f" (misses +{missed:g})" if missed else "")
                    ),
                    "shared_by": 1,
                    "quiet": False,
                    "group": competition.name,
                }
            )
    return flags


def _event_groups(world: World, result: ConstraintResult):
    """`group(event, match)` -> the team (or dual club) the event is about, as
    the page's lists of a rule's fixtures head them; "" when it has none. Uses
    `_entity_counts`'s choice of team or club, applied to the pinned events."""
    club_events, team_events = _classify_events(world, result.events)
    by_club = len(club_events) >= len(team_events)
    in_bucket = {id(e) for e in (club_events if by_club else team_events)}

    def group(event: Event, match: Match) -> str:
        if id(event) not in in_bucket:
            return ""
        if by_club:
            return world.club(world.team(event.team_ids[0]).club_id).name
        playing = [t for t in event.team_ids if t in (match.home_team, match.away_team)]
        return world.team_label((playing or event.team_ids)[0])

    return group


def _flag_legend(options: list[dict], hard_ids: set[str]) -> list[dict]:
    """The badges that appear in at least one option, once each per polarity."""
    shown: dict[str, set[str]] = defaultdict(set)
    for option in options:
        for entry in option["league_entries"]:
            for flag in entry["flags"]:
                if flag["quiet"]:
                    continue
                if flag["rule_id"] in hard_ids:
                    shown[flag["rule_id"]].add("broken")
                else:
                    shown[flag["rule_id"]].add("good" if flag["delta"] > 0 else "cost")
    order = {rule_id: i for i, rule_id in enumerate(BADGE_LABELS)}
    return [
        {
            "id": rule_id,
            "label": badge_label(rule_id),
            "description": describe(rule_id),
            "polarities": sorted(polarities),
        }
        for rule_id, polarities in sorted(shown.items(), key=lambda kv: order.get(kv[0], 99))
    ]


def _competition_views(world: World, entries: list[dict]) -> list[dict]:
    by_competition: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        by_competition[entry["competition_id"]].append(entry)

    views: list[dict] = []
    for competition_id, comp_entries in sorted(by_competition.items()):
        competition = world.competition(competition_id)
        rounds: dict[int, list[dict]] = defaultdict(list)
        # Within a round: by date, then kickoff, then home team, as a reader scans it.
        for entry in sorted(comp_entries, key=_reading_order):
            rounds[entry["round_index"]].append(entry)

        views.append(
            {
                "id": competition_id,
                "kind": "league",
                "name": competition.name,
                "meta": (
                    f"{len(comp_entries)} matches over {len(rounds)} rounds · "
                    f"prefers {competition.preferred_weekday}"
                ),
                "rounds": [
                    {
                        "title": f"Round {round_index + 1}",
                        "text": f"{_round_date_label(round_entries)} · leg {round_entries[0]['leg']}",
                        "matches": round_entries,
                    }
                    for round_index, round_entries in sorted(rounds.items())
                ],
            }
        )
    return views


def _reading_order(entry: dict) -> tuple:
    """Date, then kickoff (an untimed entry after the timed ones), then home team."""
    return (
        entry["date"],
        entry.get("kickoff_time") or "99:99",
        entry.get("home", entry.get("team", "")),
    )


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
        key=lambda e: (
            e["date"],
            e.get("kickoff_time") or "99:99",
            e["competition_id"],
            e.get("home", e.get("team", "")),
        ),
    )


def _month_calendar_view(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """A month-grid calendar (#77): one cell per day, with at most one dot per
    competition that has something on it.

    Returns `(months, day_details)` — the grid itself, one entry per month
    spanned, each a list of Monday-first weeks; and every date with an entry,
    for the page to show when a day is tapped. The details are keyed by date
    rather than inlined, so a day appearing as both an in-month cell and the
    next month's padding isn't encoded twice.
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
        months.append(_month_grid(cursor))
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)

    day_details = [
        {
            "date_iso": day.isoformat(),
            "label": f"{day:%A} {day.day} {day:%B %Y}",
            "competition_ids": _day_competitions(day_entries),
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


def _month_grid(month_start: date) -> dict:
    """One calendar month's Monday-first weeks as ISO dates, with `None` for
    the days of neighbouring months: every week is a full row, but a date
    appears only once in the grid (as its own month's cell)."""
    weeks = [
        [day.isoformat() if day.month == month_start.month else None for day in week]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)
    ]
    return {"label": month_start.strftime("%B %Y"), "key": month_start.strftime("%Y-%m"), "weeks": weeks}


def _day_competitions(day_entries: list[dict]) -> list[str]:
    """One competition id per competition present on the day (#77), however
    many of its matches fall on it, ordered by competition name."""
    names = {entry["competition_id"]: entry["competition_name"] for entry in day_entries}
    return sorted(names, key=lambda cid: names[cid])


def _round_fixtures(entries: list[dict], competition_id: str, round_index: int) -> list[dict]:
    """The entries of one cup or European round, sorted the way a calendar
    reads — by date, then team."""
    return sorted(
        (
            entry
            for entry in entries
            if entry["competition_id"] == competition_id and entry["round_index"] == round_index
        ),
        key=lambda entry: (entry["date"], entry["team"]),
    )


def _cup_views(cup_schedules: list[CupSchedule], cup_entries: list[dict]) -> list[dict]:
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
                "kind": "cup",
                "name": schedule.competition_name,
                "meta": (
                    f"{team_count} teams entered · opponent drawn closer to each round, so dates "
                    "are per-team, not fixtures · home/away assumed per NM rules (away for the "
                    "first three rounds, then alternating; final always neutral)"
                ),
                "rounds": [
                    _cup_round(placement, _round_fixtures(cup_entries, schedule.competition_id, i))
                    for i, placement in enumerate(schedule.rounds)
                ],
            }
        )
    return views


def _cup_round(placement: CupRoundPlacement, fixtures: list[dict]) -> dict:
    if placement.spread_days == 0:
        dates = _standalone_date(placement.earliest_date)
    else:
        dates = f"{short_date(placement.earliest_date)} – {_standalone_date(placement.latest_date)}"
    venue_summary = _cup_venue_summary(placement.venue_type)
    return {
        "note": placement.note,
        "title": placement.round_name,
        "text": f"{dates} · {venue_summary}",
        "fixtures": fixtures,
    }


def _cup_entries(
    world: World,
    cup_schedules: list[CupSchedule],
    competition_colors: dict[str, dict],
) -> list[dict]:
    """Cup rounds at one row per entered team per round, so the combined views
    can merge them with league entries.
    """
    entries: list[dict] = []
    for schedule in cup_schedules:
        comp_color = competition_colors.get(schedule.competition_id, _DEFAULT_COMP_COLOR)
        for round_index, placement in enumerate(schedule.rounds):
            for team_id, team_date in placement.dates.items():
                entries.append(
                    {
                        "date": team_date,
                        "competition_id": schedule.competition_id,
                        "competition_name": schedule.competition_name,
                        "competition_short": schedule.competition_short_name,
                        "comp_fg": comp_color["fg"],
                        "comp_bg": comp_color["bg"],
                        "round_index": round_index,
                        "round_name": placement.round_name,
                        "team_id": team_id,
                        "team": world.team_short_label(team_id),
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
    european_competitions: list[Competition],
    resolved_legs: ResolvedLegs,
    european_entries: list[dict],
) -> list[dict]:
    """European qualifying rounds, in the shape `_cup_views` uses for cups.

    The difference: UEFA draws pairings well ahead, so an opponent is often
    known and shown directly, "TBD" only where the tie still hangs on another
    result. Home/away likewise comes straight from `EuropeanTie.home_leg`
    rather than being inferred as a cup's `venue_type` is.
    """
    views: list[dict] = []
    for competition in sorted(european_competitions, key=lambda c: c.id):
        count = len(competition.european_rounds)
        if competition.is_main_tournament:
            meta = (
                f'{count} matchday(s)/round(s) tracked · every reachable team shown with opponent "TBD" '
                "— pairings aren't drawn this far ahead"
            )
        else:
            meta = (
                f"{count} qualifying round(s) tracked · opponent and home/away shown where UEFA has "
                'confirmed them, "TBD" where the tie is still conditional on another result'
            )
        rounds = []
        for i, round_ in enumerate(competition.european_rounds):
            dates = _european_round_date_label(resolved_legs[(competition.id, round_.id)])
            rounds.append(
                {
                    "note": round_.note,
                    "title": round_.name,
                    "text": dates,
                    "fixtures": _round_fixtures(european_entries, competition.id, i),
                }
            )
        views.append(
            {
                "id": competition.id,
                "kind": "european",
                "name": competition.name,
                "meta": meta,
                "rounds": rounds,
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


def _european_entries(
    world: World,
    european_competitions: list[Competition],
    resolved_legs: ResolvedLegs,
    competition_colors: dict[str, dict],
) -> list[dict]:
    """European rounds at one row per entered team per leg, so the combined
    views can merge them with league and cup entries. Tagged `is_european` so
    the page picks the opponent-aware card.
    """
    entries: list[dict] = []
    for competition in european_competitions:
        comp_color = competition_colors.get(competition.id, _DEFAULT_COMP_COLOR)
        for round_index, round_ in enumerate(competition.european_rounds):
            first_date, second_date = resolved_legs[(competition.id, round_.id)]
            for tie in round_.ties:
                for leg_label, leg_date in _round_legs(first_date, second_date):
                    venue_type = _european_venue_type(tie.home_leg, leg_label)
                    round_name = f"{round_.name} ({leg_label} leg)" if leg_label else round_.name
                    entries.append(
                        {
                            "is_european": True,
                            "date": leg_date,
                            "competition_id": competition.id,
                            "competition_name": competition.name,
                            "competition_short": competition.short_name,
                            "comp_fg": comp_color["fg"],
                            "comp_bg": comp_color["bg"],
                            "round_index": round_index,
                            "round_name": round_name,
                            "team_id": tie.team,
                            "team": world.team_short_label(tie.team),
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
        return _standalone_date(first_date)
    return f"{short_date(first_date)} – {_standalone_date(second_date)}"


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
        entries = [(world.club(cid).name, raw_counts.get(cid, 0), cid) for cid in dual_club_ids]
        return _fairness_row(result.constraint_id, entries, by="club")
    else:
        if len(participant_teams) < 2:
            return None
        counts = {team_id: 0 for team_id in participant_teams}
        for event in team_events:
            for team_id in event.team_ids:
                if team_id in counts:
                    counts[team_id] += 1
        entries = [(world.team_label(tid), n, world.team(tid).club_id) for tid, n in counts.items()]

    return _fairness_row(result.constraint_id, entries, by="team")


def _fairness_row(constraint_id: str, entries: list[tuple[str, int, str]], by: str) -> dict:
    """`by` says what each entry is: a "team" or a "club" (dual clubs)."""
    entries = sorted(entries, key=lambda e: -e[1])
    values = [n for _, n, _ in entries]
    lo, hi = min(values), max(values)
    mean = sum(values) / len(values)
    return {
        "id": constraint_id,
        "name": rule_name(constraint_id),
        "by": by,
        "description": describe(constraint_id),
        "entries": [{"label": label, "count": n, "club": club} for label, n, club in entries],
        "min": lo,
        "max": hi,
        # Flagged when best-to-worst spread is at least as wide as the average
        # count, and at least 2, so one stray event on an otherwise-flat rule
        # doesn't trip it (#23).
        "flagged": hi > 0 and (hi - lo) >= max(2, round(mean)),
    }


def _club_ranks(fairness_rows: list[dict]) -> dict[str, list[dict]]:
    """Each club's place in every fairness row, read off the row's own entries:
    rank 1 is the most events, ties share a rank. A club with two teams is
    listed once per team, and a club-coupling row once per club."""
    ranks: dict[str, list[dict]] = defaultdict(list)
    for row in fairness_rows:
        entries = row["entries"]
        by_club: dict[str, list] = defaultdict(list)
        for entry in entries:
            rank = 1 + sum(1 for other in entries if other["count"] > entry["count"])
            by_club[entry["club"]].append([entry["label"], entry["count"], rank])
        for club_id, found in by_club.items():
            ranks[club_id].append({"id": row["id"], "by": row["by"], "of": len(entries), "found": found})
    return dict(ranks)


def _round_date_label(entries: list[dict]) -> str:
    days = sorted({e["date"] for e in entries})
    if not days:
        return ""
    if len(days) == 1:
        return short_date(days[0])
    return f"{short_date(days[0])} – {short_date(days[-1])}"


def write_json(result: SolverResult, output_path: Path) -> Path:
    """Machine-readable twin of the HTML, for diffing and test snapshots."""
    stats = result.search_stats
    payload = {
        "solver": result.solver,
        "iterations": result.iterations,
        "elapsed_s": round(result.elapsed_s, 2),
        "notes": [{"text": str(n), "severity": severity_of(n)} for n in result.notes],
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
                **(
                    {"contingency_plan": candidate.contingency_plan.to_dict(candidate.matches)}
                    if candidate.contingency_plan is not None
                    else {}
                ),
                **(
                    {"robustness": candidate.robustness.to_dict()}
                    if candidate.robustness is not None
                    else {}
                ),
            }
            for candidate in result.candidates
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path
