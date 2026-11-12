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
import re
from collections import Counter
from datetime import date

import factories as f
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from terminliste.report.render import render_report  # noqa: E402
from terminliste.scoring.base import EvalContext, evaluate  # noqa: E402
from terminliste.scoring.hard import OneMatchPerTeamPerDay  # noqa: E402
from terminliste.scoring.registry import rule_name  # noqa: E402
from terminliste.scoring.soft import (  # noqa: E402
    ConsecutiveHomeDays,
    PreferredWeekday,
    RestComfort,
)
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


def _report_url(tmp_path_factory, matches, rest=False, cup_venue="away", with_europe=True):
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
        [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, round_name="Round 1", venue_type=cup_venue)],
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
        SolverResult(candidates=[candidate, other], solver="test"),
        tmp_path_factory.mktemp("report") / "report.html",
        cup_schedules=[cup],
        european_competitions=[europe] if with_europe else None,
        resolved_legs={("cl", "q3"): (date(2026, 6, 6), date(2026, 6, 6))} if with_europe else None,
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
    with sync_api.sync_playwright() as playwright:
        instance, reason = _launch_chromium(playwright)
        if instance is None:
            _no_browser(reason)
        yield instance
        instance.close()


def _launch_chromium(playwright):
    """The launched browser, or None and why it could not start."""
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


# -- scoring badges (#29) ------------------------------------------------------
#
# The 6 June league match is a Saturday (off the preferred Sunday) and half of
# club A's back-to-back home pair; 7 June is the Sunday other half.

LEAGUE_RULES = {"consecutive_home_days", "preferred_weekday"}


def _club_a_men(scope, element):
    """Club A's men's league fixture drawn as `element`: the one carrying both the
    off-day and the home-pair rule."""
    return scope.locator(f'{element}[data-rules~="preferred_weekday"][data-rules~="consecutive_home_days"]').first


def _assert_league_badges(element):
    assert set(element.get_attribute("data-rules").split()) == LEAGUE_RULES
    assert element.get_attribute("data-delta") == "12.5"  # half of the 25-point pair, off-day earns nothing
    assert sorted(_texts(element.locator(".flag"))) == ["home pair", "off-day"]


def test_badges_render_on_cards_rows_and_day_rows_with_their_rules_and_delta(page):
    for view in ("by-competition", "combined-calendar"):
        body = _view(page, view)
        _assert_league_badges(_club_a_men(body, ".fx"))
    _assert_league_badges(_club_a_men(_view(page, "combined-list"), "tr.fx-row"))
    month = _view(page, "month-calendar")
    month.locator(f'.cal-day[data-date="{DAY}"]').click()
    _assert_league_badges(_club_a_men(month, ".day-row"))


def test_cup_and_european_fixtures_stay_unbadged(page):
    body = _view(page, "combined-calendar")
    unbadged = body.locator('.fx[data-rules=""]')
    assert unbadged.count() == 2  # the cup round and the European leg
    assert unbadged.locator(".flag").count() == 0


def test_badge_tint_follows_the_fixtures_net_points(page):
    body = _view(page, "combined-calendar")
    pair_sunday = body.locator('.fx[data-rules="consecutive_home_days"]')
    assert "edge-good" in pair_sunday.get_attribute("class")  # +12.5
    assert "edge-" not in body.locator('.fx[data-rules="preferred_weekday"]').get_attribute("class")  # 0


def test_hovering_a_badge_shows_the_events_detail_and_points(page):
    body = _view(page, "combined-calendar")
    badge = _club_a_men(body, ".fx").locator('.flag[data-rule="consecutive_home_days"]')

    badge.hover()

    tooltip = page.locator("#flag-tooltip")
    tooltip.wait_for(state="visible")
    text = " ".join(tooltip.inner_text().split())
    assert "Back-to-back home days" in text and "+12.5 pts" in text
    assert "split over 2 fixtures" in text
    assert "home 2026-06-06" in text  # the event's own detail

    off_day = _club_a_men(body, ".fx").locator('.flag[data-rule="preferred_weekday"]')
    off_day.hover()
    tooltip.wait_for(state="visible")
    text_off = " ".join(tooltip.inner_text().split())
    assert "Saturday; elite prefers Sunday (misses +1)" in text_off
    assert "reward forgone" in text_off
    assert off_day.get_attribute("aria-label") == "off-day"


def test_tapping_a_badge_toggles_its_tooltip_on_touch_screens(touch_page):
    page = touch_page
    body = _view(page, "combined-list")
    badge = _club_a_men(body, "tr.fx-row").locator(".flag").first
    badge.tap()
    assert "open" in badge.get_attribute("class")
    assert page.input_value(".lens-select") == ""  # the first tap only shows the tooltip
    page.keyboard.press("Escape")
    assert "open" not in badge.get_attribute("class")


# -- rule lens (#30) -----------------------------------------------------------
#
# Club A's men's league match (6 June) and women's (7 June) are the two fixtures of
# the home pair; the men's match and club B's (13 June) are the Saturdays off the
# preferred Sunday. The cup round and European leg carry no rule.

PAIR = "consecutive_home_days"
OFF_DAY = "preferred_weekday"


@pytest.fixture
def touch_page(browser, report_url):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(report_url)
    yield page
    assert page.problems == []
    context.close()


def _open_filters(page, panel="#panel-1"):
    """Expands the phone bar's collapsed filter fields (the button is hidden on desktop)."""
    toggle = page.locator(f"{panel} .filters-toggle")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
        toggle.click()


def _lens(page, rule, panel="#panel-1"):
    _open_filters(page, panel)
    page.select_option(f"{panel} .lens-select", rule)


def _counts(body, element):
    return len(body.locator(f"{element}.lens-hit").all()), len(body.locator(f"{element}.lens-dim").all())


def test_lens_selector_lists_only_rules_with_fixtures_and_their_totals(page):
    options = page.locator("#panel-1 .lens-select option")
    assert [o.get_attribute("value") for o in options.all()] == ["", PAIR, OFF_DAY]
    texts = _texts(options)
    assert texts[1].startswith("Back-to-back home days (+25") and texts[1].endswith(", 2)")
    # The off-day rule is shown inversely: its fixtures are the ones that missed the reward.
    assert texts[2] == "Preferred weekday (2 off-day)"


def test_lens_dims_other_fixtures_and_emphasises_matching_ones_in_every_view(page):
    _lens(page, PAIR)
    for view in ("by-competition", "combined-calendar"):
        assert _counts(_view(page, view), ".fx") == (2, 3)
    assert _counts(_view(page, "combined-list"), "tr.fx-row") == (2, 3)

    month = _view(page, "month-calendar")
    assert month.locator(".cal-day.lens-hit").count() == 2
    assert [d.get_attribute("data-date") for d in month.locator(".cal-day.lens-hit").all()] == [
        "2026-06-06",
        "2026-06-07",
    ]
    assert "lens-dim" in month.locator('.cal-day[data-date="2026-06-13"]').get_attribute("class")
    assert month.locator('.cal-day[data-date="2026-06-06"] .cal-hits').inner_text() == "1"
    month.locator(f'.cal-day[data-date="{DAY}"]').click()
    assert _counts(month, ".day-row") == (1, 2)  # the home-pair match among the cup and European rows

    # The badge of the picked rule is marked on the fixtures that carry it.
    badge = _view(page, "combined-calendar").locator(f'.fx.lens-hit .flag[data-rule="{PAIR}"]').first
    assert "lens-rule" in badge.get_attribute("class")


def test_lens_on_the_inverse_rule_highlights_the_off_day_fixtures_the_badges_show(page):
    _lens(page, OFF_DAY)
    body = _view(page, "combined-calendar")
    hits = body.locator(".fx.lens-hit")
    assert hits.count() == 2
    assert set(hits.evaluate_all("els => els.map(e => e.getAttribute('data-rules'))")) >= {OFF_DAY}
    caption = " ".join(page.locator("#panel-1 .lens-text").inner_text().split())
    assert "2 fixtures off the preferred day (rule total +" in caption
    assert "from 1 on-day fixture)" in caption


def test_lens_combines_with_the_club_filter_and_only_matching_hides_and_collapses(page):
    _lens(page, PAIR)
    page.select_option(".club-select", "club_b")
    body = _view(page, "combined-calendar")
    assert _counts(body, ".fx") == (0, 1)  # club B has no home pair: its one match is dimmed

    page.select_option(".club-select", "club_a")
    assert _counts(_view(page, "combined-calendar"), ".fx") == (2, 2)  # + club A's cup round and European leg

    page.check(".lens-only-box")
    for view in ("by-competition", "combined-calendar"):
        body = _view(page, view)
        assert len(body.locator(".fx").all()) == 2
    by_comp = _view(page, "by-competition")
    assert by_comp.locator(".comp").count() == 2  # the cup and European competitions collapse
    assert by_comp.locator(".round").count() == 2
    assert len(_view(page, "combined-list").locator("tr.fx-row").all()) == 2
    month = _view(page, "month-calendar")
    assert month.locator(".cal-dot").count() == 2  # only the two home-pair days keep a dot

    page.select_option(".club-select", "club_b")
    assert "No fixtures match" in _view(page, "by-competition").inner_text()


def test_lens_persists_across_views_and_carries_over_to_the_other_option(page):
    _lens(page, PAIR)
    page.check(".lens-only-box")
    _view(page, "month-calendar")
    _view(page, "by-competition")
    panel = page.locator("#panel-1")
    assert panel.get_attribute("data-lens-rule") == PAIR
    assert panel.get_attribute("data-lens-only") == "true"
    assert page.input_value("#panel-1 .lens-select") == PAIR

    page.click('.tab[data-panel="panel-2"]')
    assert page.locator("#panel-2").get_attribute("data-lens-rule") == PAIR
    assert page.locator("#panel-2").get_attribute("data-lens-only") == "true"
    assert page.input_value("#panel-2 .lens-select") == PAIR
    assert page.locator("#panel-2 .view-pane:not([hidden]) .fx").count() == 2

    page.click('.tab[data-panel="panel-1"]')
    assert page.locator("#panel-1").get_attribute("data-lens-rule") == PAIR
    assert page.locator("#panel-1 .view-pane:not([hidden]) .fx").count() == 2  # still only the matches


def test_a_lens_on_a_rule_the_other_option_never_scores_is_dropped_there(page):
    _lens(page, OFF_DAY)
    page.click('.tab[data-panel="panel-2"]')
    assert page.locator("#panel-2").get_attribute("data-lens-rule") == ""
    assert page.locator("#panel-2 .lens-hit, #panel-2 .lens-dim").count() == 0
    assert [o.get_attribute("value") for o in page.locator("#panel-2 .lens-select option").all()] == ["", PAIR]


def test_clearing_the_lens_restores_every_fixture(page):
    _lens(page, PAIR)
    page.check(".lens-only-box")
    assert page.locator("#panel-1 .lens-caption").is_visible()

    page.click("#panel-1 .lens-clear")

    assert page.input_value("#panel-1 .lens-select") == ""
    assert not page.locator("#panel-1 .lens-caption").is_visible()
    assert page.is_disabled(".lens-only-box") and not page.is_checked(".lens-only-box")
    body = _view(page, "combined-calendar")
    assert len(body.locator(".fx").all()) == 5
    assert body.locator(".lens-hit, .lens-dim").count() == 0


def test_show_in_calendar_from_the_scorecard_sets_the_lens_and_scrolls_to_the_controls(page):
    item = page.locator(f'#panel-1 details.item[data-rule="{PAIR}"]')
    item.locator("summary").click()

    item.locator(".lens-link").click()

    assert page.input_value("#panel-1 .lens-select") == PAIR
    assert page.locator("#panel-1").get_attribute("data-lens-rule") == PAIR
    page.wait_for_function("() => document.querySelector('#panel-1 .controls').getBoundingClientRect().top < 300")


def test_opening_a_scorecard_entry_lists_its_fixtures_grouped_by_team(page, world):
    item = page.locator(f'#panel-1 details.item[data-rule="{PAIR}"]')
    assert item.locator(".occ-row").count() == 0  # built lazily
    item.locator("summary").click()
    # The list is built on the toggle event, after the click returns.
    item.locator(".occ-row").first.wait_for()

    groups = _texts(item.locator(".occ-group > summary"))
    assert groups == ["Club A ×2"]
    rows = _texts(item.locator(".occ-row"))
    assert len(rows) == 2
    assert rows[0].startswith("Sat 06/06") and "+12.5" in rows[0] and "1 of 2 fixtures" in rows[0]
    assert rows[1].startswith("Sun 07/06")

    off_day = page.locator(f'#panel-1 details.item[data-rule="{OFF_DAY}"]')
    off_day.locator("summary").click()
    off_day.locator(".occ-row").first.wait_for()
    assert "2 fixtures off the preferred day" in off_day.locator(".occ-summary").inner_text()
    # Grouped by competition; the summary already says the reward is forgone.
    assert _texts(off_day.locator(".occ-group > summary")) == [f"{world.competition('elite').name} ×2"]
    assert not any("reward forgone" in t for t in _texts(off_day.locator(".occ-row")))


def test_an_occurrence_row_jumps_to_and_flashes_its_fixture(page):
    item = page.locator(f'#panel-1 details.item[data-rule="{PAIR}"]')
    item.locator("summary").click()
    row = item.locator(".occ-row").nth(1)
    uid = row.get_attribute("data-fx")

    row.click()

    flashed = page.locator("#panel-1 .view-pane:not([hidden]) .flash")
    flashed.wait_for()
    assert flashed.count() == 1 and flashed.get_attribute("data-uid") == uid

    # In the month calendar the row opens its day. (A club pick hides the season
    # scorecard, so the row is only reachable while no club is selected.)
    page.wait_for_timeout(2600)
    month = _view(page, "month-calendar")
    row.click()
    flashed = month.locator(".flash")
    flashed.wait_for()
    assert flashed.get_attribute("data-uid") == uid
    assert month.locator(".cal-day.selected").get_attribute("data-date") == "2026-06-07"


def test_clicking_a_badge_selects_its_rule_while_hover_still_shows_its_tooltip(page):
    body = _view(page, "combined-calendar")
    badge = _club_a_men(body, ".fx").locator(f'.flag[data-rule="{OFF_DAY}"]')

    badge.hover()
    page.locator("#flag-tooltip").wait_for(state="visible")
    assert "highlight this rule" in page.locator("#flag-tooltip").inner_text()
    badge.click()

    assert page.input_value("#panel-1 .lens-select") == OFF_DAY
    assert page.locator("#panel-1 .fx.lens-hit").count() == 2


def test_the_second_tap_on_a_badge_selects_its_rule_on_touch_screens(touch_page):
    page = touch_page
    body = _view(page, "combined-calendar")
    badge = _club_a_men(body, ".fx").locator(f'.flag[data-rule="{PAIR}"]')

    badge.tap()
    assert "open" in badge.get_attribute("class") and page.input_value(".lens-select") == ""
    badge.tap()

    assert page.input_value(".lens-select") == PAIR
    assert "open" not in badge.get_attribute("class")


def test_the_lens_controls_do_not_overflow_a_phone_screen(touch_page):
    page = touch_page
    _lens(page, PAIR)
    for view in ("by-competition", "combined-list", "month-calendar", "combined-calendar"):
        _view(page, view)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def _effective_opacity(locator):
    return locator.evaluate(
        """el => { let o = 1; for (let n = el; n; n = n.parentElement) o *= +getComputedStyle(n).opacity; return o; }"""
    )


def test_a_tooltip_on_a_dimmed_fixture_is_fully_opaque(page):
    _lens(page, PAIR)
    body = _view(page, "combined-calendar")
    dimmed = body.locator(f'.fx.lens-dim[data-rules~="{OFF_DAY}"]').first
    assert _effective_opacity(dimmed) < 1

    dimmed.locator(".flag").first.hover()

    tooltip = page.locator("#flag-tooltip")
    tooltip.wait_for(state="visible")
    assert _effective_opacity(tooltip) == 1
    page.mouse.move(0, 0)
    tooltip.wait_for(state="hidden")
    assert page.locator("#flag-tooltip").count() == 1  # one shared tip, nothing orphaned by re-renders


def test_a_row_from_another_rules_list_selects_that_rule_before_flashing(page):
    _lens(page, PAIR)
    item = page.locator(f'#panel-1 details.item[data-rule="{OFF_DAY}"]')
    item.locator("summary").click()
    row = item.locator(".occ-row").first
    uid = row.get_attribute("data-fx")

    row.click()

    assert page.input_value("#panel-1 .lens-select") == OFF_DAY
    flashed = page.locator("#panel-1 .view-pane:not([hidden]) .flash")
    flashed.wait_for()
    assert flashed.get_attribute("data-uid") == uid
    assert "lens-hit" in flashed.get_attribute("class") and "lens-dim" not in flashed.get_attribute("class")


def test_a_click_without_pointer_type_still_shows_the_tooltip_first_on_a_no_hover_device(browser, report_url):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.goto(report_url)
    assert page.evaluate("matchMedia('(hover: none)').matches")
    body = _view(page, "combined-calendar")
    badge = _club_a_men(body, ".fx").locator(f'.flag[data-rule="{PAIR}"]')
    press = "b => b.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 1}))"

    badge.evaluate(press)
    assert "open" in badge.get_attribute("class") and page.input_value(".lens-select") == ""
    badge.evaluate(press)
    assert page.input_value(".lens-select") == PAIR

    # Enter (detail 0) selects at once.
    page.click(".lens-clear")
    badge = _club_a_men(_view(page, "combined-calendar"), ".fx").locator(f'.flag[data-rule="{PAIR}"]')
    badge.evaluate("b => b.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 0}))")
    assert page.input_value(".lens-select") == PAIR
    context.close()


# -- sticky summary and controls bar (#31) ----------------------------------


def _rect(page, selector):
    return page.evaluate(
        "s => { const r = document.querySelector(s).getBoundingClientRect(); return {top: r.top, bottom: r.bottom, h: r.height}; }",
        selector,
    )


@pytest.fixture
def short_page(browser, report_url):
    # Short enough that the small test calendar can scroll past the bar.
    page = browser.new_page(viewport={"width": 1280, "height": 420})
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(report_url)
    yield page
    assert page.problems == []
    page.close()


def test_the_bar_stays_in_view_and_summarises_the_selected_option(short_page):
    page = short_page
    _view(page, "combined-calendar")
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    bar = _rect(page, "#panel-1 .controls")
    assert bar["top"] == 0 and 0 < bar["h"] < 420 * 0.4
    summary = " ".join(page.inner_text("#panel-1 .bar-summary").split())
    total = page.inner_text("#panel-1 .total").split("/")[0].strip()
    assert summary.startswith(f"Option 1 {total}/100") and "feasible" in summary.lower()

    page.click('.tab[data-panel="panel-2"]')
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    assert not page.locator("#panel-1 .controls").is_visible()
    assert _rect(page, "#panel-2 .controls")["top"] == 0
    assert page.inner_text("#panel-2 .bar-opt") == "Option 2"


def test_the_bar_controls_drive_the_view_club_and_rule_filters_and_chips_clear_them(short_page):
    page = short_page
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    _view(page, "combined-calendar")
    page.select_option("#panel-1 .club-select", "club_b")
    page.select_option("#panel-1 .lens-select", PAIR)
    assert _rect(page, "#panel-1 .controls")["top"] == 0
    assert page.inner_text("#panel-1 .club-clear .chip-text") == "Club B"
    assert rule_name(PAIR) in page.inner_text("#panel-1 .lens-clear .chip-text")

    page.click("#panel-1 .lens-clear")
    assert page.input_value("#panel-1 .lens-select") == "" and not page.locator("#panel-1 .lens-clear").is_visible()
    page.click("#panel-1 .club-clear")
    assert page.input_value("#panel-1 .club-select") == "" and not page.locator("#panel-1 .club-clear").is_visible()
    assert page.evaluate("document.activeElement.className") == "club-select"
    body = _view(page, "combined-calendar")  # clearing the club leaves the timeline
    assert len(body.locator(".fx").all()) == 5


def test_scroll_targets_clear_the_sticky_bar(short_page):
    page = short_page
    item = page.locator(f'#panel-1 details.item[data-rule="{PAIR}"]')
    item.locator("summary").click()
    item.locator(".lens-link").click()
    # The drawn view starts at, not under, the bar.
    page.wait_for_function(
        "() => { const b = document.querySelector('#panel-1 .controls').getBoundingClientRect(),"
        " p = document.querySelector('#panel-1 .view-pane:not([hidden])').getBoundingClientRect();"
        " return p.top >= b.bottom - 1 && p.top < b.bottom + 40; }"
    )

    page.evaluate("window.scrollTo(0, 0)")
    item.locator(".occ-row").nth(1).click()
    flashed = page.locator("#panel-1 .view-pane:not([hidden]) .flash")
    flashed.wait_for()
    page.wait_for_timeout(700)
    bar, target = _rect(page, "#panel-1 .controls"), flashed.bounding_box()
    assert target["y"] >= bar["bottom"] and target["y"] + target["height"] <= 420


def test_the_legend_is_a_popover_that_hides_card_entries_in_the_month_view(page):
    toggle = page.locator("#panel-1 .legend-toggle")
    panel = page.locator("#panel-1 .legend-panel")
    assert not panel.is_visible()
    toggle.click()
    assert panel.is_visible() and toggle.get_attribute("aria-expanded") == "true"
    assert panel.locator(".legend-cards").is_visible() and panel.locator(".flag-legend .flag").count() > 0
    page.keyboard.press("Escape")
    assert not panel.is_visible() and toggle.get_attribute("aria-expanded") == "false"

    _view(page, "month-calendar")
    toggle.click()
    assert not panel.locator(".legend-cards").is_visible() and panel.locator(".flag-legend").is_visible()
    page.mouse.click(5, 5)
    assert not panel.is_visible()


def test_the_phone_bar_stays_compact_collapses_its_filters_and_never_overflows(touch_page):
    page = touch_page
    bar = _rect(page, "#panel-1 .controls")
    assert bar["h"] < 844 * 0.15
    fields = page.locator("#panel-1 .bar-fields")
    toggle = page.locator("#panel-1 .filters-toggle")
    assert not fields.is_visible() and toggle.get_attribute("aria-expanded") == "false"

    toggle.tap()
    assert fields.is_visible() and toggle.get_attribute("aria-expanded") == "true"
    assert _rect(page, "#panel-1 .controls")["h"] < 844 * 0.25
    page.select_option("#panel-1 .lens-select", PAIR)
    # Picking a rule keeps the fields open, since it enables "Only show matching".
    assert fields.is_visible()
    page.check("#panel-1 .lens-only")
    assert not fields.is_visible() and "1" in toggle.inner_text()
    assert page.locator("#panel-1 .lens-clear").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.tap("#panel-1 .lens-clear")
    assert page.input_value("#panel-1 .lens-select") == ""
    assert page.evaluate("document.activeElement.classList.contains('filters-toggle')")


def test_focus_and_anchors_leave_room_for_the_bar(page):
    height = _rect(page, "#panel-1 .controls")["h"]
    padding = page.evaluate("parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)")
    assert padding >= height


def test_all_five_phone_view_buttons_fit_the_bar_without_scrolling(browser, report_url):
    for width in (390, 360):
        context = browser.new_context(viewport={"width": width, "height": 800}, has_touch=True)
        page = context.new_page()
        page.goto(report_url)
        boxes = [b.bounding_box() for b in page.locator("#panel-1 .view-btn").all()]
        assert len(boxes) == 5
        assert all(b["x"] >= 0 and b["x"] + b["width"] <= width for b in boxes)
        assert len({round(b["y"]) for b in boxes}) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        strip = page.locator("#panel-1 .view-toggle")
        assert strip.evaluate("e => e.scrollWidth <= e.clientWidth")
        assert page.get_by_role("button", name="Combined calendar").count() >= 1
        context.close()


def _open_phone_filters(page):
    page.evaluate("document.querySelector('#panel-1 .controls').scrollIntoView({block: 'start'})")
    page.tap("#panel-1 .filters-toggle")
    assert page.locator("#panel-1 .bar-fields").is_visible()


def test_phone_filters_close_after_a_change_a_real_scroll_and_escape(touch_page):
    page = touch_page
    toggle = page.locator("#panel-1 .filters-toggle")
    fields = page.locator("#panel-1 .bar-fields")

    _open_phone_filters(page)
    page.select_option("#panel-1 .club-select", "club_b")
    assert not fields.is_visible() and toggle.get_attribute("aria-expanded") == "false"
    assert page.locator("#panel-1 .club-clear").is_visible()
    assert page.evaluate("document.activeElement.classList.contains('filters-toggle')")

    # A small nudge (e.g. focus scrolling) keeps it open; a real scroll closes it.
    toggle.tap()
    page.evaluate("window.scrollBy(0, 5)")
    page.wait_for_timeout(100)
    assert fields.is_visible()
    page.evaluate("window.scrollBy(0, -200)")
    page.wait_for_function("() => !document.querySelector('#panel-1 .bar-fields').offsetParent")
    assert toggle.get_attribute("aria-expanded") == "false"

    toggle.tap()
    page.focus("#panel-1 .lens-select")
    page.keyboard.press("Escape")
    assert not fields.is_visible() and toggle.get_attribute("aria-expanded") == "false"
    assert page.evaluate("document.activeElement.classList.contains('filters-toggle')")


# -- round, week and day totals and the season strip (#32) --------------------
#
# A total is the net of the badges on the games shown: the home-pair matches
# (6 and 7 June) carry 12.5 each, the 13 June match nothing, and the cup round
# and European leg carry no badges.


def _points(text):
    """A total as the page writes it: "+12.5 pts", "−38 pts" or "0 pts"."""
    return float(text.replace(" pts", "").replace("−", "-"))


@pytest.fixture
def midweek_page(browser, midweek_url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(midweek_url)
    yield page
    assert page.problems == []
    page.close()


def test_round_labels_total_the_visible_badges_and_count_their_games(page):
    body = _view(page, "by-competition")
    # The league rounds with a badge (the 13 June off-day one nets to 0) keep their net.
    assert sorted(_texts(body.locator(".round .rsum"))) == sorted(
        ["+12.5 pts · 1 game", "0 pts · 1 game", "+12.5 pts · 1 game", "1 game", "1 game"]
    )
    assert body.locator(".round .rlabel.rl-good").count() == 2  # the two home-pair rounds
    assert body.locator(".rtag").count() == 0  # no round is all midweek
    for round_ in body.locator(".round:has(.rnet)").all():
        deltas = round_.locator(".fx").evaluate_all("els => els.map(e => Number(e.getAttribute('data-delta')))")
        assert _points(round_.locator(".rsum .rnet").inner_text()) == pytest.approx(sum(deltas), abs=0.05)


def test_a_round_with_no_badges_says_its_games_and_no_points(page):
    body = _view(page, "by-competition")
    # The cup round and the European leg carry no badges.
    assert _texts(body.locator(".round:not(:has(.rnet)) .rsum")) == ["1 game", "1 game"]
    assert body.locator(".round:not(:has(.rnet)) .rsum .rnet").count() == 0


def test_combined_calendar_weeks_total_their_visible_games(page):
    body = _view(page, "combined-calendar")
    # Week 23 holds the two home-pair matches plus the cup round and European leg.
    assert sorted(_texts(body.locator(".round .rsum"))) == ["+25 pts · 4 games", "0 pts · 1 game"]
    assert body.locator(".round .rlabel.rl-good").count() == 1


def test_the_club_filter_resets_the_round_and_week_totals(page):
    page.select_option(".club-select", "club_b")
    assert _texts(_view(page, "by-competition").locator(".round .rsum")) == ["0 pts · 1 game"]
    assert _texts(_view(page, "combined-calendar").locator(".round .rsum")) == ["0 pts · 1 game"]

    page.select_option(".club-select", "club_a")
    assert _texts(_view(page, "combined-calendar").locator(".round .rsum")) == ["+25 pts · 4 games"]


def test_month_days_show_their_net_and_the_day_detail_its_totals(page):
    body = _view(page, "month-calendar")
    day = body.locator(f'.cal-day[data-date="{DAY}"]')
    assert _texts(day.locator(".cal-net > span")) == ["3", "+12.5"]  # the count and the net, each its own line
    assert "3 games; net +12.5 pts" in day.get_attribute("aria-label")
    day.click()
    assert _texts(body.locator(".day-detail .rsum")) == ["+12.5 pts · 3 games"]
    assert _texts(body.locator('.cal-day[data-date="2026-06-13"] .cal-net > span')) == ["1", "0"]

    page.select_option(".club-select", "club_b")
    assert body.locator(f'.cal-day[data-date="{DAY}"] .cal-net').count() == 0  # no club B game that day


def test_the_combined_list_scores_each_fixture_in_its_own_column(page):
    body = _view(page, "combined-list")
    assert _texts(body.locator("th"))[-1].lower() == "score"  # the header is upper-cased by CSS
    assert sorted(_texts(body.locator("tr.fx-row .fx-score"))) == sorted(["+12.5", "+12.5", "0", "0", "0"])


def test_the_season_strip_has_a_button_per_round_in_both_league_rows(page):
    strip = page.locator("#panel-1 .season-strip")
    _view(page, "by-competition")
    assert strip.is_visible()
    assert strip.locator(".strip-caption").inner_text() == "Net points per round — click to jump"
    assert strip.locator(".strip-cells").evaluate_all("els => els.every(e => e.scrollWidth <= e.clientWidth)")
    assert len(_texts(strip.locator(".strip-name"))) == 2
    cells = strip.locator(".strip-cell")
    assert _texts(cells) == ["1 +12.5", "2 0", "1 +12.5"]
    assert cells.first.get_attribute("aria-label") == "Round 1: +12.5 pts · 1 game"

    _view(page, "combined-calendar")
    assert strip.is_visible()
    for view in ("month-calendar", "combined-list"):
        _view(page, view)
        assert not strip.is_visible()


def test_the_strip_follows_the_club_filter(page):
    page.select_option(".club-select", "club_b")
    _view(page, "by-competition")  # a dual club opens on the timeline; the strip belongs to the round views
    cells = page.locator("#panel-1 .strip-cell")
    assert _texts(cells) == ["1", "2 0", "1"]  # the two rounds with no club B game are empty
    assert cells.first.is_disabled()
    assert cells.first.get_attribute("aria-label") == "Round 1: no fixtures shown"


def test_clicking_a_strip_cell_brings_its_round_below_the_sticky_bar(short_page):
    page = short_page
    _view(page, "by-competition")
    page.evaluate("window.scrollTo(0, 0)")
    assert _rect(page, "#panel-1 .view-pane:not([hidden]) .round")["top"] > 420  # starts off screen

    page.locator("#panel-1 .strip-cell").first.click()

    page.wait_for_function(
        "() => { const b = document.querySelector('#panel-1 .controls').getBoundingClientRect(),"
        " r = document.querySelector('#panel-1 .view-pane:not([hidden]) .round').getBoundingClientRect();"
        " return r.top >= b.bottom - 1 && r.top < b.bottom + 40; }"
    )


def test_totals_and_strip_do_not_overflow_a_phone_screen(touch_page):
    page = touch_page
    for view in ("by-competition", "combined-calendar", "combined-list", "month-calendar"):
        _view(page, view)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    _view(page, "by-competition")
    # The phone strip scrolls inside itself, with a fade on its right edge.
    fade = page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('#panel-1 .strip-cells'));"
        " return s.maskImage || s.webkitMaskImage; }"
    )
    assert "gradient" in fade


def test_a_round_with_only_midweek_games_is_tagged(midweek_page):
    body = _view(midweek_page, "by-competition")
    assert body.locator(".rtag").count() == 1
    tagged = body.locator(".round:has(.rtag)")
    assert "Tue 09/06" in tagged.inner_text()
    summary = " ".join(tagged.locator(".rsum").inner_text().split()).lower()  # the tag is upper-cased by CSS
    assert "1 game" in summary and summary.endswith("midweek")
    assert tagged.locator(".rtag").get_attribute("title") == "Midweek: every game falls Monday to Thursday"
    # The strip cell's name is the parts joined with " · ", midweek last.
    assert page_label(midweek_page, 2) == "Round 3: 0 pts · 1 game · midweek"


@pytest.fixture
def rules_page(browser, rules_url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(rules_url)
    yield page
    assert page.problems == []
    page.close()


def test_round_labels_count_costs_and_repeated_badges_and_flag_hard_breaks(rules_page):
    body = _view(rules_page, "by-competition")
    round_four = body.locator(".round").nth(3)  # Eliteserien round 4: two short-rest games
    summary = " ".join(round_four.locator(".rsum").inner_text().split())
    assert summary.startswith("−") and " pts · 2 games · 2 with costs · 2 × Comfortable rest" in summary
    assert round_four.locator(".rnet.cost").count() == 1  # a net below zero reads amber
    assert page_label(rules_page, 3) == "Round 4: −24 pts · 2 games · 2 with costs · 2 × Comfortable rest · midweek"
    # Hard breaks show as a count in the label and as a badge on the season strip.
    assert body.locator(".rsum", has_text="hard break").count() >= 1
    strip = rules_page.locator("#panel-1 .season-strip")
    assert strip.locator(".strip-hard").count() >= 1
    assert strip.locator(".strip-cell.sc-cost .strip-net").count() >= 1


def page_label(page, index):
    return page.locator("#panel-1 .strip-cell").nth(index).get_attribute("aria-label")


INK = "rgb(148, 98, 10)"  # --warn-ink, the text colour of a cost figure


def test_cost_figures_are_set_in_the_ink_colour(rules_page):
    # Each view is drawn only while active, so each figure is read in its own view.
    assert _view(rules_page, "by-competition").locator(".round .rnet.cost").first.evaluate(
        "e => getComputedStyle(e).color") == INK
    assert rules_page.locator("#panel-1 .strip-cell.sc-cost .strip-net").first.evaluate(
        "e => getComputedStyle(e).color") == INK
    assert _view(rules_page, "month-calendar").locator(
        '.cal-day[data-date="2026-06-10"] .cal-value.cost').evaluate("e => getComputedStyle(e).color") == INK
    assert _view(rules_page, "combined-list").locator("tr.fx-row .fx-score.cost").first.evaluate(
        "e => getComputedStyle(e).color") == INK


def test_costs_colour_the_combined_list_score_and_the_day_figures(rules_page):
    assert _view(rules_page, "combined-list").locator("tr.fx-row .fx-score.cost").count() >= 2
    month = _view(rules_page, "month-calendar")
    for day in ("2026-06-10", "2026-06-17"):  # the two short-rest games of round 4
        assert month.locator(f'.cal-day[data-date="{day}"] .cal-value.cost').count() == 1


# -- club scope and timeline (issue #33) ---------------------------------------


@pytest.fixture(scope="module")
def clash_url(tmp_path_factory):
    # Both of club A's teams are at home on 6 June: the timeline's clash tag.
    d = date(2026, 6, 6)
    matches = [
        f.match("elite", "a_m", "opp_m", d, "va"),
        f.match("topp", "a_w", "opp_w", d, "va"),
    ]
    return _report_url(tmp_path_factory, matches)


def _wire(page):
    return page.evaluate("JSON.parse(document.getElementById('report-data').textContent)")


def _signed(total):
    text = f"{round(total, 1):+.1f}"
    return text.replace("-", "−")


def _club_teams(data, club):
    return [tid for tid, team in data["teams"].items() if team["club"] == club]


def _club_expected(data, option, club):
    """A club's figure per rule, rebuilt from the wire the way the page builds it:
    the badges on its fixtures that name one of its teams or the club, plus the
    season-level events attributed to it. Preferred weekday (inverse) is per
    competition, so it belongs to no club."""
    names = {data["clubNames"][club]} | {data["teams"][tid]["label"] for tid in _club_teams(data, club)}
    totals = Counter()
    for match in option["matches"]:
        if club not in (data["teams"][match["h"]]["club"], data["teams"][match["a"]]["club"]):
            continue
        for flag in match.get("f", []):
            rule = data["rules"][flag[0]]
            group = option["groups"][flag[4]] if len(flag) > 4 else ""
            if not rule.get("inv") and group in names:
                totals[rule["id"]] += flag[1]
    for rule_index, total, _count in option["clubUnpinned"].get(club, []):
        totals[data["rules"][rule_index]["id"]] += total
    return totals


def _club_fixture_count(data, club):
    league = sum(
        1
        for m in data["options"][0]["matches"]
        if club in (data["teams"][m["h"]]["club"], data["teams"][m["a"]]["club"])
    )
    cup = sum(1 for e in data["shared"] if data["teams"][e["t"]]["club"] == club)
    return league + cup, len(data["options"][0]["matches"]) + len(data["shared"])


def _share_text(total, gross):
    """A club rule's share of the club's own movement, as the page writes it: rounded half
    up, the sign kept, and a non-zero share that rounds to nothing reads "<1%"."""
    if not total:
        return "0%"
    sign = "+" if total > 0 else "−"
    share = int(100 * abs(total) / gross + 0.5) if gross else 0
    return f"{sign}{share}%" if share else f"{sign}<1%"


def _club_gross(data, expected):
    """The club's movement: the magnitudes of its soft rules' totals."""
    hard = {r["id"] for r in data["rules"] if r.get("hard")}
    return sum(abs(round(total, 2)) for rule, total in expected.items() if rule not in hard)


def _raw_text(total):
    return "0.0" if round(total, 1) == 0 else _signed(total)


def test_selecting_a_club_restates_the_breakdown_as_its_own_share(page):
    data = _wire(page)
    option = data["options"][0]
    expected = _club_expected(data, option, "club_a")
    gross = _club_gross(data, expected)
    page.select_option("#panel-1 .club-select", "club_a")
    page.locator("#panel-1 .scope-breakdown > summary").click()

    # Satisfied hard rules sit in one collapsed line above the table, not as rows.
    satisfied = [i for i in option["order"] if data["rules"][i].get("hard") and round(expected[data["rules"][i]["id"]], 1) == 0]
    rows = page.locator("#panel-1 .scope-breakdown tbody tr")
    assert rows.count() == len(option["order"]) - len(satisfied)
    collapsed = page.locator("#panel-1 .scope-breakdown .hard-ok > summary")
    if satisfied:
        noun = "rule" if len(satisfied) == 1 else "rules"
        assert collapsed.inner_text() == f"{len(satisfied)} hard {noun} satisfied ✓"
    else:
        assert collapsed.count() == 0
    visible = [i for i in option["order"] if i not in satisfied]
    for i, index in enumerate(visible):
        rule = data["rules"][index]
        row = rows.nth(i)
        assert row.locator(".name").inner_text() == rule_name(rule["id"])
        total = expected[rule["id"]]
        cell = row.locator("td").nth(2)
        if rule.get("hard"):
            assert cell.inner_text() == (_signed(total) if round(total, 1) else "—"), rule["id"]
        else:
            assert cell.locator(".share-text").inner_text() == _share_text(total, gross), rule["id"]
            assert cell.locator(".raw").inner_text() == _raw_text(total), rule["id"]


def test_the_club_scorecard_lists_its_biggest_upsides_and_problems_from_its_share(page):
    data = _wire(page)
    option = data["options"][0]
    expected = _club_expected(data, option, "club_a")
    gross = _club_gross(data, expected)
    page.select_option("#panel-1 .club-select", "club_a")

    ups = page.locator("#panel-1 .scope-card .col").nth(0).locator("details.item")
    downs = page.locator("#panel-1 .scope-card .col").nth(1).locator("details.item")
    got_ups = {(ups.nth(i).locator(".name").inner_text(), ups.nth(i).locator(".share-text").inner_text()) for i in range(ups.count())}
    got_downs = {(downs.nth(i).locator(".name").inner_text(), downs.nth(i).locator(".share-text").inner_text()) for i in range(downs.count())}
    want_ups = {(rule_name(rule), _share_text(total, gross)) for rule, total in expected.items() if total > 0}
    want_downs = {(rule_name(rule), _share_text(total, gross)) for rule, total in expected.items() if total < 0}
    assert got_ups == want_ups
    assert got_downs == want_downs


def test_the_scope_is_announced_and_the_season_scorecard_comes_back_on_clear(page):
    data = _wire(page)
    before = page.inner_text("#panel-1 .cols.season-part")
    page.select_option("#panel-1 .club-select", "club_a")

    banner = page.locator("#panel-1 .scope-banner")
    assert banner.is_visible()
    matches = data["options"][0]["clubHeadlines"]["club_a"][0]["value"]
    assert page.inner_text("#panel-1 .scope-text").startswith(f"Club A — {matches} match")
    assert "season-wide" in page.inner_text("#panel-1 .scope-text")
    assert "Preferred weekday is scored per competition and left out." in page.inner_text("#panel-1 .scope-text")
    assert page.locator("#panel-1 .cols.season-part").is_hidden()

    page.click("#panel-1 .scope-clear")
    assert page.input_value("#panel-1 .club-select") == ""
    assert banner.is_hidden()
    assert page.locator("#panel-1 .scope-card").inner_html() == ""
    assert page.inner_text("#panel-1 .cols.season-part") == before


def test_the_shown_count_sits_in_the_banner_for_a_club_and_under_the_bar_for_a_rule(page):
    data = _wire(page)
    count = page.locator("#panel-1 .scope-count")
    assert count.is_hidden()
    page.select_option("#panel-1 .club-select", "club_a")
    shown, total = _club_fixture_count(data, "club_a")
    assert count.inner_text() == f"{shown} of {total} fixtures shown"
    assert page.locator("#panel-1 .scope-banner .scope-count").is_visible()

    page.select_option("#panel-1 .club-select", "")
    assert count.is_hidden()
    page.select_option("#panel-1 .lens-select", PAIR)
    page.check("#panel-1 .lens-only-box")
    assert page.locator("#panel-1 .lens-caption .lens-count").is_visible()
    assert re.fullmatch(r"\d+ of \d+ fixtures shown", page.inner_text("#panel-1 .lens-count"))


def test_a_dual_club_opens_on_a_timeline_of_both_teams_in_date_order(page):
    data = _wire(page)
    teams = _club_teams(data, "club_a")
    counts = Counter()
    for e in data["shared"]:
        if e["t"] in teams:
            counts[(e["d"], e["t"])] += 1
    for m in data["options"][0]["matches"]:
        for tid in (m["h"], m["a"]):
            if tid in teams:
                counts[(m["d"], tid)] += 1
    dates = sorted({d for d, _ in counts})

    page.select_option("#panel-1 .club-select", "club_a")
    assert page.get_attribute("#panel-1", "data-current-view") == "club-timeline"
    # The lane headings are uppercased by CSS, so compare their text, not what is drawn.
    assert page.locator("#panel-1 .tl-lane").all_text_contents() == [data["teams"][tid]["label"] for tid in teams]

    rows = page.locator("#panel-1 .tl-row")
    assert _texts(rows.locator(".tl-date b")) == [date.fromisoformat(d).strftime("%a %d/%m") for d in dates]
    for i, d in enumerate(dates):
        for j, tid in enumerate(teams):
            lane = rows.nth(i).locator(".tl-cell").nth(j)
            assert lane.locator(".tl-fx").count() == counts[(d, tid)], (d, tid)


def test_home_pairs_across_the_two_teams_are_tagged_on_both_days(page):
    page.select_option("#panel-1 .club-select", "club_a")
    rows = page.locator("#panel-1 .tl-row")
    # Club A's men play at home on 6 June and its women on 7 June.
    assert _texts(rows.nth(0).locator(".tl-tag")) == ["home pair"]
    assert _texts(rows.nth(1).locator(".tl-tag")) == ["home pair"]
    assert rows.nth(1).locator(".tl-cell").nth(1).locator(".tl-ha").inner_text() == "H"


def test_two_teams_of_one_club_at_home_on_the_same_day_are_a_flagged_clash(clash_url, browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(clash_url)
    page.select_option("#panel-1 .club-select", "club_a")
    first = page.locator("#panel-1 .tl-row").nth(0)
    assert "tl-clash" in first.get_attribute("class")
    tag = first.locator(".tl-tag")
    assert tag.inner_text() == "both home the same day"
    assert "tl-tag-bad" in tag.get_attribute("class")
    context.close()


def test_the_timeline_without_a_club_offers_the_dual_clubs_and_clearing_leaves_it(page):
    _view(page, "club-timeline")
    buttons = page.locator("#panel-1 .tl-club-btn")
    assert _texts(buttons) == ["Club A", "Club B"]
    buttons.nth(1).click()
    assert page.input_value("#panel-1 .club-select") == "club_b"
    assert page.locator("#panel-1 .tl-row").count() > 0

    page.click("#panel-1 .club-clear")
    assert page.get_attribute("#panel-1", "data-current-view") == "by-competition"


def test_the_club_and_view_carry_over_to_the_other_option(page):
    page.select_option("#panel-1 .club-select", "club_b")
    page.locator(".tab").nth(1).click()
    assert page.input_value("#panel-2 .club-select") == "club_b"
    assert page.get_attribute("#panel-2", "data-current-view") == "club-timeline"


def test_a_dual_club_keeps_the_view_the_reader_is_on(page):
    _view(page, "month-calendar")
    page.select_option("#panel-1 .club-select", "club_a")
    assert page.get_attribute("#panel-1", "data-current-view") == "month-calendar"
    _view(page, "by-competition")
    page.select_option("#panel-1 .club-select", "club_b")
    assert page.get_attribute("#panel-1", "data-current-view") == "club-timeline"


@pytest.fixture(scope="module")
def tbd_url(tmp_path_factory):
    # Club A's women play away on 5 June; its men's cup tie on 6 June has no venue
    # decided yet. The two must not read as an away pair.
    matches = [f.match("topp", "opp_w", "a_w", date(2026, 6, 5), "vc")]
    return _report_url(tmp_path_factory, matches, cup_venue="unknown", with_europe=False)


def test_a_tie_with_no_venue_decided_is_marked_and_never_paired(tbd_url, browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(tbd_url)
    page.select_option("#panel-1 .club-select", "club_a")
    rows = page.locator("#panel-1 .tl-row")
    assert _texts(rows.nth(0).locator(".tl-tag")) == []  # 5 June: no pair with the tie
    assert _texts(rows.nth(1).locator(".tl-tag")) == []  # 6 June: the tie is not an away venue
    cup = rows.nth(1).locator(".tl-cell").nth(0).locator(".tl-fx").filter(has_text="TBD")
    assert cup.locator(".tl-ha").inner_text() == "?"
    assert cup.locator(".tl-ha").get_attribute("aria-label") == "Venue not decided"
    context.close()


def test_a_team_filter_narrows_every_view_and_the_timeline_to_one_lane(page):
    data = _wire(page)
    women = next(tid for tid, t in data["teams"].items() if t["club"] == "club_a" and t["gender"] == "women")
    page.select_option("#panel-1 .club-select", "club_a")
    select = page.locator("#panel-1 .team-select")
    assert page.locator("#panel-1 .team-filter").is_visible()
    assert _texts(select.locator("option")) == ["All", "Men", "Women"]

    select.select_option(women)
    shown = sum(
        1 for m in data["options"][0]["matches"] if women in (m["h"], m["a"])
    ) + sum(1 for e in data["shared"] if e["t"] == women)
    total = len(data["options"][0]["matches"]) + len(data["shared"])
    assert page.inner_text("#panel-1 .scope-count") == f"{shown} of {total} fixtures shown"
    assert "With Women picked" in page.inner_text("#panel-1 .scope-text")
    assert page.locator("#panel-1 .tl-lane").all_text_contents() == [data["teams"][women]["label"]]

    _view(page, "combined-list")
    assert page.locator('#panel-1 .view-pane[data-view="combined-list"] tbody tr.fx-row').count() == shown

    page.click("#panel-1 .team-clear")
    assert select.input_value() == ""
    assert page.locator("#panel-1 .team-clear").is_hidden()
    club_total, _ = _club_fixture_count(data, "club_a")
    assert page.locator('#panel-1 .view-pane[data-view="combined-list"] tbody tr.fx-row').count() == club_total
    _view(page, "club-timeline")
    assert page.locator("#panel-1 .tl-lane").count() == 2


def test_a_hidden_team_is_lifted_when_a_list_row_points_at_its_fixture(page):
    data = _wire(page)
    men = next(tid for tid, t in data["teams"].items() if t["club"] == "club_a" and t["gender"] == "men")
    page.select_option("#panel-1 .club-select", "club_a")
    item = page.locator('#panel-1 .scope-card details.item[data-rule="' + PAIR + '"]')
    lifted = False
    for i in range(2):
        page.select_option("#panel-1 .team-select", men)
        if not item.evaluate("e => e.open"):
            item.locator("summary").click()
        row = item.locator(".occ-row").nth(i)
        row.click()
        flashed = page.locator("#panel-1 .view-pane:not([hidden]) .flash")
        flashed.wait_for()
        assert flashed.is_visible()
        lifted = lifted or page.input_value("#panel-1 .team-select") == ""
    assert lifted


def test_the_breakdown_and_upsides_give_the_clubs_place_among_teams(page):
    data = _wire(page)
    option = data["options"][0]
    ranks = {row["id"]: row for row in option["clubRanks"]["club_a"]}
    page.select_option("#panel-1 .club-select", "club_a")
    page.locator("#panel-1 .scope-breakdown > summary").click()
    rows = page.locator("#panel-1 .scope-breakdown tbody tr")
    for i, index in enumerate(option["order"]):
        rule_id = data["rules"][index]["id"]
        cell = rows.nth(i).locator("td").nth(4).inner_text()
        if rule_id not in ranks:
            assert cell == "\u2014"
            continue
        row = ranks[rule_id]
        unit = "dual clubs" if row["by"] == "club" else "teams"
        parts = [f"{label} {_ordinal(rank)} of {row['of']} {unit} ({_events(count)})" for label, count, rank in row["found"]]
        assert cell == "; ".join(parts), rule_id


def _ordinal(n):
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _events(n):
    return f"{n} event" if n == 1 else f"{n} events"


def test_the_club_scorecard_and_timeline_fit_a_phone_without_overflow(touch_page):
    _open_filters(touch_page)
    touch_page.select_option("#panel-1 .club-select", "club_a")
    assert touch_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    touch_page.locator("#panel-1 .scope-card").scroll_into_view_if_needed()
    assert touch_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert touch_page.locator("#panel-1 .scope-card .cols").bounding_box()["width"] <= 390 - 32
    _view(touch_page, "club-timeline")
    assert touch_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert touch_page.locator("#panel-1 .tl-grid").bounding_box()["width"] <= 390 - 32


_SHARE_TEXT = re.compile(r"^[+−]?\d+%$")


def test_the_scorecard_names_rules_and_weighs_each_as_a_share_of_the_movement(page):
    cols = page.locator("#panel-1 .cols")
    names = _texts(cols.locator(".name"))
    shares = _texts(cols.locator(".share-text"))
    assert names and shares
    assert all("_" not in name for name in names), names
    assert all(_SHARE_TEXT.match(share) for share in shares), shares
    assert rule_name(PAIR) in names


def test_how_is_this_scored_explains_the_score_in_words(page):
    page.click("#panel-1 .how-scored > summary")
    text = " ".join(page.locator("#panel-1 .how-body").inner_text().split())
    assert "20 + 80 × s" in text
    assert "per match" in text
    assert "Per match, 0 gives 60" in text
    assert "a red − takes them away" in text


def test_the_score_explanation_and_club_breakdown_fit_a_phone_without_overflow(browser, report_url):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(report_url)
    _open_filters(page)
    page.select_option("#panel-1 .club-select", "club_a")
    page.click("#panel-1 .how-scored > summary")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#panel-1 .scope-breakdown > summary").click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert problems == []
    context.close()


_COVERAGE_TEXT = re.compile(r"^\d+ of \d+ (matches|home matches of two-team clubs|gaps|team half-seasons) \(\d+%\)$")


def test_coverage_counts_sit_under_the_rule_names_in_the_scorecard(page):
    texts = _texts(page.locator("#panel-1 .cols .coverage"))
    assert texts, "the scorecard shows at least one counted rule"
    assert all(_COVERAGE_TEXT.match(t) for t in texts), texts


def test_all_rules_list_groups_every_soft_rule_and_states_the_net(page):
    summary = page.locator("#panel-1 .all-soft > summary")
    page.click("#panel-1 .all-soft > summary")
    heads = page.locator("#panel-1 .all-soft .group-head").evaluate_all("els => els.map(e => e.textContent.trim())")
    assert [h.split(" (")[0] for h in heads] == ["Helping", "Hurting", "Neutral, zero or did not apply"]
    listed = page.locator("#panel-1 .all-soft .item.compact").count()
    neutral_list = page.locator("#panel-1 .all-soft .neutral-list")
    neutral = neutral_list.inner_text() if neutral_list.count() else ""
    neutral_count = len(neutral.split(", ")) if neutral else 0
    assert summary.inner_text() == f"Show all {listed + neutral_count} soft rules: helping, hurting, neutral"
    net = " ".join(page.locator("#panel-1 .all-soft .soft-sum").inner_text().split())
    assert re.match(r"^Helped by [+−]\d+\.\d, hurt by [+−]\d+\.\d, net [+−]\d+\.\d\.$", net), net


def test_the_club_breakdown_hides_rank_and_fixtures_on_a_phone(browser, report_url):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.goto(report_url)
    _open_filters(page)
    page.select_option("#panel-1 .club-select", "club_a")
    page.locator("#panel-1 .scope-breakdown > summary").click()
    assert page.locator("#panel-1 .scope-breakdown .col-extra").first.is_hidden()
    assert page.locator("#panel-1 .scope-breakdown .col-type").first.is_hidden()
    assert page.locator("#panel-1 .scope-breakdown th", has_text="Events").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()



# -- comparing the options (#35) ----------------------------------------------


def _first_visible_round(page, panel):
    """The title line of the first round in the drawn view that is on screen below the bar."""
    return page.evaluate(
        """(panel) => {
          const top = document.querySelector(panel + ' .controls').getBoundingClientRect().bottom;
          for (const n of document.querySelectorAll(panel + ' .view-pane:not([hidden]) [data-uid]')) {
            const r = n.getBoundingClientRect();
            if (r.bottom > top && r.top < innerHeight) return n.closest('.round').innerText.split('\\n')[0];
          }
          return null;
        }""",
        panel,
    )


def test_the_comparison_sits_above_the_tabs_and_each_option_name_opens_its_tab(page):
    assert page.locator(".compare").is_visible()
    assert page.locator(".compare .cmp-open").count() == 2
    assert page.locator(".compare").bounding_box()["y"] < page.locator(".tabs").bounding_box()["y"]

    page.click('.cmp-open[data-option="1"]')
    assert page.get_attribute('.tab[data-panel="panel-2"]', "aria-selected") == "true"
    assert page.locator("#panel-2").is_visible()
    assert not page.locator("#panel-1").is_visible()


def test_the_comparison_fits_a_phone_screen_without_overflow(browser, report_url):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(report_url)
    assert page.locator(".compare").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # The table fits its own wrapper, so it never has to scroll sideways on a phone.
    assert page.evaluate("(() => { const w = document.querySelector('.cmp-wrap'); return w.scrollWidth <= w.clientWidth; })()")
    assert page.problems == []
    context.close()


def test_switching_options_keeps_the_round_in_view(page):
    _view(page, "by-competition")
    page.evaluate(
        "window.scrollTo(0, document.querySelectorAll('#panel-1 .round')[1].getBoundingClientRect().top + window.scrollY)"
    )
    page.wait_for_timeout(400)  # the anchor is read once scrolling settles
    before = _first_visible_round(page, "#panel-1")
    assert before
    # Scroll up to the tabs, as a reader does, then flip to the other option.
    page.mouse.wheel(0, -20000)
    page.wait_for_timeout(800)
    page.click('.tab[data-panel="panel-2"]')
    page.wait_for_timeout(1000)  # the anchor scroll is smooth
    assert _first_visible_round(page, "#panel-2") == before
