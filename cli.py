#!/usr/bin/env python3
"""Terminliste — season scheduling for related football leagues.

    python cli.py validate                       # data integrity, no solving
    python cli.py generate --season 2026          # solve and write a report
    python cli.py score my_schedule.csv --season 2026
                                                   # score a real/proposed schedule
    python cli.py explain schedules/2026.json      # score breakdown in the terminal
    python cli.py refresh-reference-data           # diff data/*.yml against a live API
    python cli.py refresh-travel-times             # fetch real ground travel times

`validate` and `score` need no solver and run in well under a second.
`generate` runs the local-search solver by default (`--solver cpsat` needs
OR-Tools installed). `refresh-reference-data` and `refresh-travel-times` are
the only commands that touch the network, and only to read — neither
rewrites `data/*.yml`; `refresh-travel-times` writes to the gitignored
`data/.refdata_cache/` instead. See `terminliste/refdata/refresh.py` and
`terminliste/refdata/travel_refresh.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT_ROOT / "data"
SCHEDULES_ROOT = PROJECT_ROOT / "schedules"

sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.external_schedule import ExternalScheduleError, load_external_schedule  # noqa: E402
from terminliste.model.loader import DataError, load_world  # noqa: E402
from terminliste.model.travel import ApiTravelModel  # noqa: E402
from terminliste.refdata import refresh_competition, refresh_travel_times  # noqa: E402
from terminliste.report.render import render_report, write_json  # noqa: E402
from terminliste.rounds.cup_schedule import CupSchedulingError, schedule_cups  # noqa: E402
from terminliste.rounds.european_schedule import (  # noqa: E402
    EuropeanCascadeError,
    resolve_all_legs,
    resolve_european_commitments,
)
from terminliste.scoring.base import EvalContext, Score, evaluate  # noqa: E402
from terminliste.scoring.registry import build_constraints  # noqa: E402
from terminliste.solvers import Candidate, ProgressUpdate, SearchStats, SolveRequest, SolverResult, get_scheduler  # noqa: E402

# TheSportsDB's league-name string for each competition this project knows
# about. Unconfirmed against a live call in this sandbox — the network egress
# policy here blocks the API host entirely (see README) — so treat these as
# a documented best guess to verify the first time this runs somewhere that
# can actually reach the API, not as tested constants.
API_LEAGUE_NAMES = {
    "eliteserien_2026": "Norwegian Eliteserien",
    "toppserien_2026": "Norwegian Toppserien",
}


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        world = load_world(DATA_ROOT)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(world.venues)} venues, {len(world.clubs)} clubs, {len(world.teams)} teams, "
        f"{len(world.competitions)} competitions, {len(world.seasons)} season(s)."
    )
    dual = world.dual_clubs()
    print(f"     {len(dual)} dual clubs: {', '.join(c.name for c in dual)}")
    for competition in world.competitions.values():
        if competition.format == "cup":
            print(
                f"     {competition.name}: {competition.team_count} teams entered, "
                f"{competition.rounds} rounds tracked (forced/windowed dates, pairings TBD), "
                f"movable={competition.movable}"
            )
        elif competition.format == "european":
            print(
                f"     {competition.name}: {competition.team_count} teams entered, "
                f"{competition.rounds} rounds tracked, movable={competition.movable}"
            )
        else:
            print(
                f"     {competition.name}: {competition.team_count} teams, "
                f"{competition.rounds} rounds, {competition.total_matches} matches, "
                f"movable={competition.movable}"
            )

    for season in world.seasons.values():
        if season.cup_competitions:
            cup_competitions = [world.competition(c) for c in season.cup_competitions]
            try:
                schedules, warnings = schedule_cups(cup_competitions, season)
            except CupSchedulingError as exc:
                print(f"FAIL: {exc}", file=sys.stderr)
                return 1
            for schedule in schedules:
                print(f"     {schedule.competition_name} resolves cleanly:")
                for placement in schedule.rounds:
                    span = (
                        f"{placement.earliest_date}"
                        if placement.spread_days == 0
                        else f"{placement.earliest_date} – {placement.latest_date}"
                    )
                    print(f"       {placement.round_name}: {span}")
            for warning in warnings:
                print(f"     ⚠ {warning}")

        if season.european_competitions:
            european_competitions = [world.competition(c) for c in season.european_competitions]
            try:
                commitments_by_team, european_warnings = resolve_european_commitments(
                    european_competitions, season
                )
            except EuropeanCascadeError as exc:
                print(f"FAIL: {exc}", file=sys.stderr)
                return 1
            for team_id, commitments in sorted(commitments_by_team.items()):
                print(f"     {world.team_label(team_id)} European commitments:")
                for commitment in commitments:
                    print(f"       {commitment.label}: {commitment.date}")
            for warning in european_warnings:
                print(f"     ⚠ {warning}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    world = _load_world_or_exit()
    season = _season_or_exit(world, args.season)
    competitions = [world.competition(c) for c in season.competitions]
    cup_schedules, cup_warnings = _schedule_cups_or_exit(world, season)
    european_commitments, european_warnings = _resolve_european_or_exit(world, season)
    constraints = build_constraints(
        world, season, competitions, cup_schedules, european_commitments
    )
    travel = ApiTravelModel(world)
    ctx = EvalContext(world=world, season=season, travel=travel)

    scheduler = get_scheduler(args.solver)
    # Live progress (issue #34) — a terminal-only courtesy: piped/redirected
    # output (e.g. a CI log) would otherwise fill up with carriage-return
    # updates that don't mean anything once flattened to lines.
    live = sys.stdout.isatty()
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        cup_schedules=cup_schedules,
        european_commitments=european_commitments,
        constraints=constraints,
        ctx=ctx,
        seed=args.seed,
        top_n=args.top_n,
        time_budget_s=args.time_budget,
        progress=_print_progress if live else None,
    )

    print(f"Solving with {scheduler.name} (budget {args.time_budget:.0f}s, seed {args.seed})...")
    for warning in [*cup_warnings, *european_warnings]:
        print(f"  ⚠ {warning}")
    try:
        result = scheduler.solve(request)
    except RuntimeError as exc:
        if live:
            print()
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if live:
        print()  # leave the last progress line intact rather than overwriting it

    if not result.candidates:
        print("FAIL: solver produced no candidates.", file=sys.stderr)
        for note in result.notes:
            print(f"  - {note}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{season.id}.html"
    json_path = out_dir / f"{season.id}.json"

    european_competitions, resolved_legs = _european_report_data(world, season)
    render_report(
        world,
        season,
        result,
        html_path,
        title=f"{season.year} season schedule",
        warnings=[*cup_warnings, *european_warnings],
        cup_schedules=cup_schedules,
        european_competitions=european_competitions,
        resolved_legs=resolved_legs,
    )
    write_json(result, json_path)

    _print_summary(result)
    print(f"\nWrote {html_path} and {json_path}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    world = _load_world_or_exit()
    season = _season_or_exit(world, args.season)
    competitions = [world.competition(c) for c in season.competitions]
    cup_schedules, cup_warnings = _schedule_cups_or_exit(world, season)
    european_commitments, european_warnings = _resolve_european_or_exit(world, season)
    constraints = build_constraints(
        world, season, competitions, cup_schedules, european_commitments
    )
    travel = ApiTravelModel(world)

    try:
        matches, warnings = load_external_schedule(Path(args.schedule), world)
    except ExternalScheduleError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if not matches:
        print("FAIL: schedule file contained no matches.", file=sys.stderr)
        return 1

    warnings = [*warnings, *cup_warnings, *european_warnings]
    ctx = EvalContext(world=world, season=season, travel=travel, detail=True)
    score = evaluate(matches, constraints, ctx)

    print(f"Scored {len(matches)} matches from {args.schedule}\n")
    if warnings:
        print("Coverage notes:")
        for warning in warnings:
            print(f"  ⚠ {warning}")
        print()

    _print_score(score)

    result = SolverResult(
        candidates=[Candidate(matches=matches, score=score, label="Imported schedule")],
        solver="external",
    )
    out_path = Path(args.out) if args.out else SCHEDULES_ROOT / f"{Path(args.schedule).stem}_scored.html"
    european_competitions, resolved_legs = _european_report_data(world, season)
    render_report(
        world,
        season,
        result,
        out_path,
        title=f"Score report — {Path(args.schedule).name}",
        full_diagnostics=True,
        warnings=warnings,
        cup_schedules=cup_schedules,
        european_competitions=european_competitions,
        resolved_legs=resolved_legs,
    )
    print(f"\nWrote {out_path}")

    return 0 if score.feasible else 2


def cmd_explain(args: argparse.Namespace) -> int:
    path = Path(args.schedule_json)
    if not path.exists():
        print(f"FAIL: no such file: {path}", file=sys.stderr)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))

    options = payload.get("options", [])
    if args.option is not None:
        options = [o for o in options if o.get("label") == args.option]
    if not options:
        print("FAIL: no matching option in that file.", file=sys.stderr)
        return 1

    for option in options:
        print(f"=== {option['label']} (seed {option.get('seed', '?')}) ===")
        print(f"  points:          {option.get('points', '?')} / 100")
        print(f"  hard violations: {option['hard_violations']}")
        print(f"  soft total:      {option['soft_total']}")
        for row in sorted(option["breakdown"], key=lambda r: (r["kind"] != "hard", -abs(r["total"]))):
            if not row["count"]:
                continue
            print(f"    {row['constraint']:<28} {row['total']:>9.1f}  (kind={row['kind']}, n={row['count']})")
        print()
    return 0


def cmd_refresh_reference_data(args: argparse.Namespace) -> int:
    world = _load_world_or_exit()
    season = _season_or_exit(world, args.season)

    any_diffs = False
    for competition_id in season.competitions:
        league_name = API_LEAGUE_NAMES.get(competition_id)
        if league_name is None:
            print(f"skipping {competition_id!r}: no API league name mapped for it")
            continue

        report = refresh_competition(world, competition_id, league_name, force=args.force)
        print(f"\n=== {competition_id} ({league_name}) — source: {report.source} ===")
        if report.error:
            print(f"  note: {report.error}")
        if report.source == "unavailable":
            print("  no data to compare — using whatever is already in data/*.yml")
            continue

        if not report.diffs and not report.unmatched_api_teams and not report.unmatched_local_teams:
            print("  no differences found")
        for diff in report.diffs:
            any_diffs = True
            print(f"  {diff.team_id}: {diff.field} — file has {diff.current!r}, API has {diff.fetched!r}")
        if report.unmatched_api_teams:
            any_diffs = True
            print(f"  API teams not matched to a local team: {', '.join(report.unmatched_api_teams)}")
        if report.unmatched_local_teams:
            any_diffs = True
            print(f"  local teams not matched to an API team: {', '.join(report.unmatched_local_teams)}")

    print(
        "\nThis only reports differences — nothing in data/*.yml has been changed. "
        "Fold in anything you trust by hand."
    )
    return 1 if any_diffs else 0


def cmd_refresh_travel_times(args: argparse.Namespace) -> int:
    world = _load_world_or_exit()
    report = refresh_travel_times(world, force=args.force)

    print(
        f"{report.fetched} fetched, {report.reused} reused from cache, "
        f"{len(report.failed)} failed (of {report.pairs_total} venue pairs)"
    )
    for error in report.failed:
        print(f"  ⚠ {error}")
    if report.failed:
        print(
            "\nFailed pairs keep whatever was cached before (or fall back to the "
            "air-travel estimate at run time). Re-run to retry them."
        )
    return 0


def _print_progress(update: ProgressUpdate) -> None:
    """Live "how's it going" line (issue #34), overwritten in place."""
    pct = 100 * update.iterations / max(1, update.total_iterations)
    stats = update.stats
    feasible_pct = 100 * stats.feasible_rate if stats.investigated else 0.0
    sys.stdout.write(
        f"\r  restart {update.restart + 1}/{update.total_restarts}: "
        f"{update.iterations:,}/{update.total_iterations:,} ({pct:.0f}%) scenarios tried — "
        f"{feasible_pct:.0f}% viable so far   "
    )
    sys.stdout.flush()


def _print_search_stats(stats: SearchStats) -> None:
    if not stats.investigated:
        return
    print(
        f"  search: {stats.investigated:,} scenarios investigated, "
        f"{stats.feasible:,} viable ({100 * stats.feasible_rate:.0f}%), "
        f"{stats.hard_violation_scenarios:,} hit a hard-rule violation — difficulty: {stats.difficulty}"
    )
    if stats.hard_violation_counts:
        top = stats.hard_violation_counts.most_common(3)
        breakdown = ", ".join(f"{name} ({count:,})" for name, count in top)
        print(f"    most common dead end: {breakdown}")


def _print_summary(result: SolverResult) -> None:
    print(f"\n{len(result.candidates)} option(s), {result.iterations} iterations, "
          f"{result.elapsed_s:.1f}s elapsed")
    _print_search_stats(result.search_stats)
    for note in result.notes:
        print(f"  note: {note}")
    for candidate in result.candidates:
        badge = "feasible" if candidate.score.feasible else f"{candidate.score.hard_violations} HARD VIOLATIONS"
        print(f"  {candidate.label}: {candidate.score.points:.0f}/100 ({badge})")


def _print_score(score: Score) -> None:
    if score.feasible:
        print("Feasible — no hard rules broken.\n")
    else:
        print(f"NOT FEASIBLE — {score.hard_violations} hard rule violation(s):\n")
        for result in score.hard_results():
            if not result.count:
                continue
            print(f"  ✗ {result.constraint_id} ({result.count})")
            for event in result.events[:5]:
                print(f"      {event.detail}")
            if len(result.events) > 5:
                print(f"      ...and {len(result.events) - 5} more")
        print()

    print(f"Score: {score.points:.0f} / 100\n")
    print(f"Soft score: {score.soft_total:.1f}\n")
    print("Soft rules, worst first:")
    for result in sorted(score.soft_results(), key=lambda r: r.total):
        marker = "✗" if result.total < 0 else ("·" if result.total == 0 else "✓")
        print(f"  {marker} {result.constraint_id:<28} {result.total:>9.1f}  (n={result.count})")


def _load_world_or_exit():
    try:
        return load_world(DATA_ROOT)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


def _season_or_exit(world, season_id: str):
    try:
        return world.season(season_id)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


def _schedule_cups_or_exit(world, season):
    """Resolve the season's cups to dates, or exit with a clear reason why not."""
    cup_competitions = [world.competition(c) for c in season.cup_competitions]
    try:
        return schedule_cups(cup_competitions, season)
    except CupSchedulingError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


def _resolve_european_or_exit(world, season):
    """Resolve the season's European qualifying cascade, or exit with a
    clear reason why not."""
    european_competitions = [world.competition(c) for c in season.european_competitions]
    try:
        return resolve_european_commitments(european_competitions, season)
    except EuropeanCascadeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


def _european_report_data(world, season):
    """The season's European competitions plus their rounds resolved to real
    leg dates — what `render_report` needs to show them, as opposed to
    `_resolve_european_or_exit`'s flattened, cascade-walked commitment list,
    which is shaped for conflict-checking rather than display."""
    european_competitions = [world.competition(c) for c in season.european_competitions]
    blackouts = set(season.blacked_out_dates)
    resolved_legs, _ = resolve_all_legs(european_competitions, blackouts)
    return european_competitions, resolved_legs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="check data files for referential integrity")
    p_validate.set_defaults(func=cmd_validate)

    p_generate = sub.add_parser("generate", help="solve a season and write an HTML + JSON report")
    p_generate.add_argument("--season", default="2026")
    p_generate.add_argument("--solver", choices=["local", "cpsat"], default="local")
    p_generate.add_argument("--seed", type=int, default=42)
    p_generate.add_argument("--top-n", type=int, default=3)
    p_generate.add_argument(
        "--time-budget",
        type=float,
        default=180.0,
        help=(
            "seconds (default 180 — the 2026 season's European qualifying "
            "commitments need real search headroom over a Europe-free season "
            "to reliably reach a feasible schedule; see README's 'Suggested "
            "next steps')"
        ),
    )
    p_generate.add_argument("--out", default=str(SCHEDULES_ROOT))
    p_generate.set_defaults(func=cmd_generate)

    p_score = sub.add_parser("score", help="score a real or proposed schedule (CSV/JSON) against the rules")
    p_score.add_argument("schedule", help="path to a .csv or .json schedule file")
    p_score.add_argument("--season", default="2026")
    p_score.add_argument("--out", default=None, help="HTML report path")
    p_score.set_defaults(func=cmd_score)

    p_explain = sub.add_parser("explain", help="print a generated schedule's score breakdown")
    p_explain.add_argument("schedule_json")
    p_explain.add_argument("--option", default=None, help="only this option's label, e.g. 'Option 1'")
    p_explain.set_defaults(func=cmd_explain)

    p_refresh = sub.add_parser(
        "refresh-reference-data",
        help="diff data/*.yml's team/venue data against a live API (read-only, needs network)",
    )
    p_refresh.add_argument("--season", default="2026")
    p_refresh.add_argument(
        "--force", action="store_true", help="ignore the cache and re-fetch from the API"
    )
    p_refresh.set_defaults(func=cmd_refresh_reference_data)

    p_refresh_travel = sub.add_parser(
        "refresh-travel-times",
        help=(
            "fetch real ground travel times between venues via OSRM and cache them "
            "(never touches data/*.yml; needs network)"
        ),
    )
    p_refresh_travel.add_argument(
        "--force", action="store_true", help="ignore the cache and re-fetch every pair"
    )
    p_refresh_travel.set_defaults(func=cmd_refresh_travel_times)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
