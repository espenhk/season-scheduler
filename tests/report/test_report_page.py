"""Browser smoke test for the report page (#42): fixtures are drawn by
report.js, so the strings a reader sees (and the club filter and day-detail
behaviour) can only be checked in a real browser.

Skipped when Playwright or a Chromium to drive is unavailable. A browser is
found through Playwright's own install, or through `PLAYWRIGHT_CHROMIUM_EXECUTABLE`
or a `chromium-*` build under `PLAYWRIGHT_BROWSERS_PATH`.
"""

from __future__ import annotations

import glob
import os
from datetime import date

import factories as f
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from terminliste.report.render import render_report  # noqa: E402
from terminliste.scoring.base import EvalContext, evaluate  # noqa: E402
from terminliste.solvers.base import Candidate, SolverResult  # noqa: E402


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


@pytest.fixture(scope="module")
def world():
    return _world()[0]


@pytest.fixture(scope="module")
def report_url(tmp_path_factory):
    world, season = _world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va", kickoff_time="18:00"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 13), "vb", round_index=1),
    ]
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    candidate = Candidate(matches=matches, score=evaluate(matches, [], ctx), label="Option 1")
    cup = f.cup_schedule(
        "cup1",
        [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, round_name="Round 1", venue_type="away")],
        competition_name="NM Cup",
        competition_short_name="NM",
    )
    round_ = f.european_round(
        "q3",
        forced_date=date(2026, 6, 6),
        ties=[f.european_tie("a_m", opponent=OPPONENT, home_leg="first")],
    )
    europe = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    path = render_report(
        world,
        season,
        SolverResult(candidates=[candidate], solver="test"),
        tmp_path_factory.mktemp("report") / "report.html",
        cup_schedules=[cup],
        european_competitions=[europe],
        resolved_legs={("cl", "q3"): (date(2026, 6, 6), date(2026, 6, 6))},
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


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except sync_api.Error:
            executable = _browser_executable()
            if not executable:
                pytest.skip("no Chromium available for Playwright")
            try:
                instance = playwright.chromium.launch(executable_path=executable)
            except sync_api.Error as exc:
                pytest.skip(f"Chromium could not be launched: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser, report_url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(report_url)
    yield page
    assert page.problems == []
    page.close()


def _view(page, name):
    page.click(f'.view-btn[data-view="{name}"]')
    return page.locator(f'.view-pane[data-view="{name}"] .pane-body')


def _texts(locator):
    return [" ".join(t.split()) for t in locator.all_inner_texts()]


def test_by_competition_shows_league_cup_and_european_fixtures(page, world):
    body = _view(page, "by-competition")

    cards = _texts(body.locator(".fx"))
    assert len(cards) == 5
    assert any("v TBD" in c and world.team_short_label("a_m") in c for c in cards)  # cup
    assert any(f"v {OPPONENT}" in c for c in cards)  # European
    assert any("18:00" in c and world.team_short_label("opp_m") in c for c in cards)  # league
    # Both of club A's home days are flagged as a back-to-back pair.
    assert body.locator(".fx.paired").count() == 2
    assert body.locator(".pair-badge").count() == 2


def test_combined_views_tag_each_fixture_with_its_competition(page):
    calendar = _view(page, "combined-calendar")
    cards = _texts(calendar.locator(".fx"))
    assert len(cards) == 5
    assert any("NM" in c and "v TBD" in c for c in cards)
    assert any(f"v {OPPONENT}" in c for c in cards)

    rows = _texts(_view(page, "combined-list").locator("tr.fx-row"))
    assert len(rows) == 5
    assert any("TBD (Round 1)" in r for r in rows)
    assert any(f"{OPPONENT} (q3)" in r for r in rows)


def test_tapping_a_day_lists_its_games_with_short_competition_tags(page, world):
    body = _view(page, "month-calendar")
    assert "Tap a day above" in body.locator(".day-detail-empty").inner_text()
    assert body.locator(f'.cal-day[data-date="{DAY}"] .cal-dot').count() == 3

    body.locator(f'.cal-day[data-date="{DAY}"]').click()

    rows = body.locator(".day-detail .day-row")
    texts = _texts(rows)
    assert len(texts) == 3
    assert any("v TBD" in t for t in texts)
    assert any(f"v {OPPONENT}" in t for t in texts)
    league_tag = rows.locator(".comp-tag", has_text=world.competition("elite").short_name).first
    assert league_tag.get_attribute("title") == world.competition("elite").name
    assert body.locator(".day-detail .rlabel").inner_text().strip() == "Saturday 06 June 2026"


def test_club_filter_hides_other_clubs_fixtures_and_empty_rounds(page):
    page.select_option(".club-select", "club_b")

    body = _view(page, "by-competition")
    assert len(_texts(body.locator(".fx"))) == 1
    assert body.locator(".round").count() == 1
    assert body.locator(".comp").count() == 1  # the cup and European competitions collapse

    assert len(_texts(_view(page, "combined-calendar").locator(".fx"))) == 1
    assert len(_texts(_view(page, "combined-list").locator("tr.fx-row"))) == 1

    calendar = _view(page, "month-calendar")
    assert calendar.locator(".cal-dot").count() == 1  # only club B's 13 June match
    calendar.locator(f'.cal-day[data-date="{DAY}"]').click()
    assert calendar.locator(".day-detail-empty").inner_text() == "No games on this day."

    page.select_option(".club-select", "")
    assert len(_texts(_view(page, "by-competition").locator(".fx"))) == 5
