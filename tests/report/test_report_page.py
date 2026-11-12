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
from terminliste.scoring.soft import ConsecutiveHomeDays, PreferredWeekday  # noqa: E402
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


def _report_url(tmp_path_factory, matches):
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    comps = [world.competition("elite"), world.competition("topp")]
    constraints = [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps)]
    candidate = Candidate(matches=matches, score=evaluate(matches, constraints, ctx), label="Option 1")
    # A second option with a rule set of its own, for the per-option lens state.
    other = Candidate(
        matches=matches,
        score=evaluate(matches, [ConsecutiveHomeDays(competitions=comps)], ctx),
        label="Option 2",
    )
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
        SolverResult(candidates=[candidate, other], solver="test"),
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


def _no_browser(reason: str):
    # CI installs Chromium, so a missing browser there is a broken setup, not a reason to skip.
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except sync_api.Error:
            executable = _browser_executable()
            if not executable:
                _no_browser("no Chromium available for Playwright")
            try:
                instance = playwright.chromium.launch(executable_path=executable)
            except sync_api.Error as exc:
                _no_browser(f"Chromium could not be launched: {exc}")
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
    assert "home pair" in text and "+12.5 pts" in text
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
    assert texts[1].startswith("consecutive home days (+25") and texts[1].endswith(", 2)")
    # The off-day rule is shown inversely: its fixtures are the ones that missed the reward.
    assert texts[2] == "preferred weekday (2 off-day)"


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


def test_lens_persists_across_views_and_is_kept_per_option(page):
    _lens(page, PAIR)
    page.check(".lens-only-box")
    _view(page, "month-calendar")
    _view(page, "by-competition")
    panel = page.locator("#panel-1")
    assert panel.get_attribute("data-lens-rule") == PAIR
    assert panel.get_attribute("data-lens-only") == "true"
    assert page.input_value("#panel-1 .lens-select") == PAIR

    page.click('.tab[data-panel="panel-2"]')
    assert page.locator("#panel-2").get_attribute("data-lens-rule") == ""
    assert page.locator("#panel-2 .lens-hit, #panel-2 .lens-dim").count() == 0
    assert [o.get_attribute("value") for o in page.locator("#panel-2 .lens-select option").all()] == ["", PAIR]
    _lens(page, PAIR, "#panel-2")

    page.click('.tab[data-panel="panel-1"]')
    assert page.locator("#panel-1").get_attribute("data-lens-rule") == PAIR
    assert page.locator("#panel-1 .view-pane:not([hidden]) .fx").count() == 2  # still only the matches


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

    groups = _texts(item.locator(".occ-group > summary"))
    assert groups == ["Club A ×2"]
    rows = _texts(item.locator(".occ-row"))
    assert len(rows) == 2
    assert rows[0].startswith("Sat 06/06") and "+12.5" in rows[0] and "1 of 2 fixtures" in rows[0]
    assert rows[1].startswith("Sun 07/06")

    off_day = page.locator(f'#panel-1 details.item[data-rule="{OFF_DAY}"]')
    off_day.locator("summary").click()
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

    # In the month calendar the row opens its day; a hiding club filter is lifted.
    page.wait_for_timeout(2600)
    month = _view(page, "month-calendar")
    page.select_option(".club-select", "club_b")
    row.click()
    flashed = month.locator(".flash")
    flashed.wait_for()
    assert flashed.get_attribute("data-uid") == uid
    assert page.input_value(".club-select") == ""
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
    body = _view(page, "combined-calendar")
    page.select_option("#panel-1 .club-select", "club_b")
    page.select_option("#panel-1 .lens-select", PAIR)
    assert _rect(page, "#panel-1 .controls")["top"] == 0
    assert page.inner_text("#panel-1 .club-clear .chip-text") == "Club B"
    assert PAIR.replace("_", " ") in page.inner_text("#panel-1 .lens-clear .chip-text")

    page.click("#panel-1 .lens-clear")
    assert page.input_value("#panel-1 .lens-select") == "" and not page.locator("#panel-1 .lens-clear").is_visible()
    page.click("#panel-1 .club-clear")
    assert page.input_value("#panel-1 .club-select") == "" and not page.locator("#panel-1 .club-clear").is_visible()
    assert len(body.locator(".fx").all()) == 5
    assert page.evaluate("document.activeElement.className") == "club-select"


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


def test_all_four_phone_view_buttons_fit_the_bar_without_scrolling(browser, report_url):
    for width in (390, 360):
        context = browser.new_context(viewport={"width": width, "height": 800}, has_touch=True)
        page = context.new_page()
        page.goto(report_url)
        boxes = [b.bounding_box() for b in page.locator("#panel-1 .view-btn").all()]
        assert len(boxes) == 4
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
    assert sorted(_texts(body.locator(".round .rsum"))) == sorted(
        ["+12.5 pts · 1 game", "0 pts · 1 game", "+12.5 pts · 1 game", "0 pts · 1 game", "0 pts · 1 game"]
    )
    assert body.locator(".round .rlabel.rl-good").count() == 2  # the two home-pair rounds
    assert body.locator(".rtag").count() == 0  # no round is all midweek
    for round_ in body.locator(".round").all():
        deltas = round_.locator(".fx").evaluate_all("els => els.map(e => Number(e.getAttribute('data-delta')))")
        assert _points(round_.locator(".rsum .rnet").inner_text()) == pytest.approx(sum(deltas), abs=0.05)


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
    assert day.locator(".cal-net").inner_text() == "+12.5"
    assert "net +12.5 pts" in day.get_attribute("aria-label")
    day.click()
    assert _texts(body.locator(".day-detail .rsum")) == ["+12.5 pts · 3 games"]
    assert body.locator('.cal-day[data-date="2026-06-13"] .cal-net').inner_text() == "0"

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
    assert len(_texts(strip.locator(".strip-name"))) == 2
    cells = strip.locator(".strip-cell")
    assert _texts(cells) == ["1 +12.5", "2 0", "1 +12.5"]
    assert cells.first.get_attribute("aria-label") == "Round 1: 1 game, net +12.5 pts"

    _view(page, "combined-calendar")
    assert strip.is_visible()
    for view in ("month-calendar", "combined-list"):
        _view(page, view)
        assert not strip.is_visible()


def test_the_strip_follows_the_club_filter(page):
    page.select_option(".club-select", "club_b")
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


def test_a_round_with_only_midweek_games_is_tagged(midweek_page):
    body = _view(midweek_page, "by-competition")
    assert body.locator(".rtag").count() == 1
    tagged = body.locator(".round:has(.rtag)")
    assert "Tue 09/06" in tagged.inner_text()
    summary = " ".join(tagged.locator(".rsum").inner_text().split()).lower()  # the tag is upper-cased by CSS
    assert "1 game" in summary and summary.endswith("midweek")
