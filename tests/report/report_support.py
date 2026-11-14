"""Shared by the report page's browser tests: a small two-league world, the report built
from it in several shapes, and a Chromium to drive.

A browser is found through Playwright's own install, or through `PLAYWRIGHT_CHROMIUM_EXECUTABLE`
or a `chromium-*` build under `PLAYWRIGHT_BROWSERS_PATH`; tests skip when none is available.
"""

from __future__ import annotations

import glob
import os
from datetime import date

import factories as f
import pytest

from terminliste.report.render import render_report
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.hard import OneMatchPerTeamPerDay
from terminliste.scoring.soft import ConsecutiveHomeDays, PreferredWeekday, RestComfort
from terminliste.solvers.base import Candidate, SolverResult


class _FixedTravel:
    def hours(self, a, b):
        return 1.0


def _world():
    va, vb, vc = f.venue("va"), f.venue("vb"), f.venue("vc")
    a_m = f.team("a_m", "club_a", "va", gender="men")
    a_w = f.team("a_w", "club_a", "va", gender="women")
    b_m = f.team("b_m", "club_b", "vb", gender="men")
    b_w = f.team("b_w", "club_b", "vb", gender="women")
    opp_m = f.team("opp_m", "opp1", "vc", gender="men")
    opp_w = f.team("opp_w", "opp2", "vc", gender="women")
    clubs = [
        f.club("club_a", [a_m, a_w], color="#112233"),
        f.club("club_b", [b_m, b_w], color="#445566"),
        f.club("opp1", [opp_m], color="#778899"),
        f.club("opp2", [opp_w], color="#aabbcc"),
    ]
    comp_m = f.competition("elite", ["a_m", "b_m", "opp_m"], gender="men")
    comp_w = f.competition("topp", ["a_w", "b_w", "opp_w"], gender="women")
    return f.world(clubs, [va, vb, vc], [comp_m, comp_w]), f.season(competitions=["elite", "topp"])


# Everything on 6 June is club A's: a league match, a cup round and a European
# leg. Club A's women play at home the next day, so both are a back-to-back pair.
DAY = "2026-06-06"
OPPONENT = "Union Saint-Gilloise"


def _base_matches():
    return [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va", kickoff_time="18:00"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 13), "vb", round_index=1),
    ]


@pytest.fixture(scope="module")
def report_url(tmp_path_factory):
    return _report_url(tmp_path_factory, _base_matches())


@pytest.fixture(scope="module")
def midweek_url(tmp_path_factory):
    # A Tuesday league match in a round of its own: the only midweek round.
    extra = f.match("elite", "b_m", "opp_m", date(2026, 6, 9), "vb", round_index=2)
    return _report_url(tmp_path_factory, _base_matches() + [extra])


@pytest.fixture(scope="module")
def other_months_url(tmp_path_factory):
    # League fixtures in June only; the cup round and the European leg fall in
    # July and September, months with no league fixture, so they are listed under
    # "Other dates".
    return _report_url(
        tmp_path_factory, _base_matches(), cup_on=date(2026, 7, 8), europe_on=date(2026, 9, 17)
    )


@pytest.fixture(scope="module")
def maybe_url(tmp_path_factory):
    # Club A's men play a cup round on 13 June only if they win the one on the 6th.
    maybe = f.commitment(
        "a_m", date(2026, 6, 13), hard=False, probability=0.5, source="cup", label="NM Cup: Round 2"
    )
    return _report_url(tmp_path_factory, _base_matches(), commitments=f.commitment_set(maybe))


@pytest.fixture(scope="module")
def rules_url(tmp_path_factory):
    # Club A's men play twice on 6 June (a hard break: one match per team per day).
    # Round 4's two games each come four days after the team's previous one, a
    # rest of three days, which RestComfort scores as a short-rest cost.
    extra = [
        f.match("elite", "opp_m", "a_m", date(2026, 6, 6), "vc", round_index=2),
        f.match("elite", "a_m", "opp_m", date(2026, 6, 10), "va", round_index=3),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 17), "vb", round_index=3),
    ]
    return _report_url(tmp_path_factory, _base_matches() + extra, rest=True)


def _report_url(
    tmp_path_factory,
    matches,
    rest=False,
    cup_venue="away",
    with_europe=True,
    cup_on=date(2026, 6, 6),
    europe_on=date(2026, 6, 6),
    cup_dates=None,
    commitments=None,
):
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    comps = [world.competition("elite"), world.competition("topp")]
    constraints = [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps)]
    if rest:
        constraints += [RestComfort(competitions=comps), OneMatchPerTeamPerDay()]
    candidate = Candidate(matches=matches, score=evaluate(matches, constraints, ctx), label="Option 1")
    # A second option with a rule set of its own, for the per-option lens state.
    other = Candidate(
        matches=matches,
        score=evaluate(matches, [ConsecutiveHomeDays(competitions=comps)], ctx),
        label="Option 2",
    )
    cup = f.cup_schedule(
        "cup1",
        [f.cup_placement("r1", cup_dates or {"a_m": cup_on}, round_name="Round 1", venue_type=cup_venue)],
        competition_name="NM Cup",
        competition_short_name="NM",
    )
    round_ = f.european_round(
        "q3",
        forced_date=europe_on,
        ties=[f.european_tie("a_m", opponent=OPPONENT, home_leg="first")],
    )
    europe = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    path = render_report(
        world,
        season,
        SolverResult(candidates=[candidate, other], solver="test"),
        tmp_path_factory.mktemp("report") / "report.html",
        cup_schedules=[cup],
        european_competitions=[europe] if with_europe else None,
        resolved_legs={("cl", "q3"): (europe_on, europe_on)} if with_europe else None,
        commitments=commitments,
    )
    return path.as_uri()


def _browser_executable() -> str | None:
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if explicit:
        return explicit
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-*/chrome"):
        found = sorted(glob.glob(os.path.join(root, pattern)))
        if found:
            return found[-1]
    return None


def _no_browser(reason: str):
    # CI installs Chromium, so a missing browser there is a broken setup, not a reason to skip.
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        instance, reason = _launch_chromium(playwright)
        if instance is None:
            _no_browser(reason)
        yield instance
        instance.close()


def _launch_chromium(playwright):
    """The launched browser, or None and why it could not start."""
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        return playwright.chromium.launch(), ""
    except sync_api.Error:
        pass
    executable = _browser_executable()
    if not executable:
        return None, "no Chromium available for Playwright"
    try:
        return playwright.chromium.launch(executable_path=executable), ""
    except sync_api.Error as exc:
        return None, f"Chromium could not be launched: {exc}"

