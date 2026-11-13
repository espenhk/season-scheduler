"""Links and export of the report page (#42): the URL hash that carries the page's state,
the ids a link can point at, and the CSV and calendar files built in the browser.

Uses the browser fixtures of `conftest.py` and skips under the same conditions.
"""

from __future__ import annotations

import re
from datetime import date

import factories as f
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from report_support import _base_matches, _report_url, _world  # noqa: E402

from terminliste.external_schedule import load_external_schedule  # noqa: E402
from terminliste.report.render import render_report  # noqa: E402
from terminliste.scoring.base import EvalContext, evaluate  # noqa: E402
from terminliste.scoring.soft import ConsecutiveHomeDays  # noqa: E402
from terminliste.solvers.base import Candidate, SolverResult  # noqa: E402

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}
# Round 2 of the men's league: its one fixture, and the round's own id.
LATE_FIXTURE = "fx-elite-2026-06-13-b_m-opp_m"
LATE_ROUND = "rd-elite-round-2"


def _open(browser, url, viewport=DESKTOP, **options):
    phone = viewport == PHONE
    context = browser.new_context(viewport=viewport, has_touch=phone, is_mobile=phone, **options)
    page = context.new_page()
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(url)
    page.owner = context
    return page


@pytest.fixture
def open_page(browser):
    pages = []

    def opener(url, viewport=DESKTOP, **options):
        pages.append(_open(browser, url, viewport, **options))
        return pages[-1]

    yield opener
    for page in pages:
        assert page.problems == []
        page.owner.close()


def _hash(page):
    return page.evaluate("location.hash")


def _view(page, name, panel="#panel-1"):
    page.click(f'{panel} .view-btn[data-view="{name}"]')


def _active_view(page, panel="#panel-1"):
    return page.locator(f"{panel} .view-btn.active").get_attribute("data-view")


def _selected_tab(page):
    return page.evaluate("[...document.querySelectorAll('.tab')].findIndex(t => t.getAttribute('aria-selected') === 'true') + 1")


def _focused_fixture(page):
    return page.evaluate("document.activeElement.closest('[id^=\"fx-\"]')?.id")


def _go_to(page, fragment):
    """Moves to a new hash the way editing the address bar does."""
    page.evaluate("fragment => { location.hash = fragment; }", fragment)
    page.wait_for_timeout(150)


# -- hash state ----------------------------------------------------------------


def test_the_hash_follows_the_option_view_club_and_picked_day(open_page, report_url):
    page = open_page(report_url)
    assert _hash(page) == ""

    _view(page, "month-calendar")
    assert _hash(page) == "#option=1&view=month-calendar"
    page.select_option("#panel-1 .club-select", "club_a")
    assert _hash(page) == "#option=1&view=month-calendar&club=club_a"
    page.click('#panel-1 button[data-date="2026-06-06"]')
    assert _hash(page).endswith("&date=2026-06-06")
    page.click('#panel-1 button[data-date="2026-06-06"]')
    assert "date=" not in _hash(page)

    page.click("#tab-2")
    assert _hash(page).startswith("#option=2&view=month-calendar&club=club_a")


def test_the_hash_carries_the_lens_the_competitions_shown_and_the_list_filter(open_page, rules_url):
    page = open_page(rules_url)
    _view(page, "combined-list")
    page.select_option("#panel-1 .lens-select", "rest_comfort")
    page.check("#panel-1 .lens-only-box")
    page.click('#panel-1 .kind-btn[data-kind="cup"]')
    page.fill("#panel-1 .list-filter", "Union")

    state = dict(pair.split("=") for pair in _hash(page)[1:].split("&"))
    assert state == {
        "option": "1", "view": "combined-list", "rule": "rest_comfort", "only": "1",
        "show": "league,cup", "q": "Union",
    }


def test_a_hash_restores_the_state_on_load(open_page, rules_url):
    page = open_page(
        rules_url + "#option=2&view=combined-list&club=club_a&rule=consecutive_home_days&show=league,cup&q=club"
    )
    assert _selected_tab(page) == 2
    assert page.locator("#panel-2").is_visible() and page.locator("#panel-1").is_hidden()
    assert _active_view(page, "#panel-2") == "combined-list"
    assert page.locator("#panel-2 .club-select").input_value() == "club_a"
    assert page.locator("#panel-2 .lens-select").input_value() == "consecutive_home_days"
    assert page.locator("#panel-2 .list-filter").input_value() == "club"
    assert page.locator('#panel-2 .kind-btn[data-kind="cup"]').get_attribute("aria-pressed") == "true"
    assert page.locator('#panel-2 .kind-btn[data-kind="european"]').get_attribute("aria-pressed") == "false"


def test_a_picked_day_is_restored_with_its_games_open(open_page, report_url):
    page = open_page(report_url + "#option=1&view=month-calendar&date=2026-06-06")
    detail = page.locator("#panel-1 #cal-day-detail")
    assert "Club A" in detail.inner_text() or "A M" in detail.inner_text()
    assert detail.locator("[data-uid]").count() == 1
    assert page.locator('#panel-1 button[data-date="2026-06-06"]').get_attribute("aria-expanded") == "true"


def test_values_that_name_nothing_are_dropped_and_the_hash_cleaned(open_page, report_url):
    page = open_page(
        report_url
        + "#option=9&view=nope&club=nobody&team=ghost&rule=made_up&only=1&show=bogus&date=2026-13-45&target=fx-nothing&junk"
    )
    assert _selected_tab(page) == 1
    assert _active_view(page) == "by-competition"
    assert page.locator("#panel-1 .club-select").input_value() == ""
    assert _hash(page) == "#option=1&view=by-competition"


def test_the_timeline_needs_a_club_so_a_link_to_it_without_one_lands_on_the_default_view(open_page, report_url):
    page = open_page(report_url + "#view=club-timeline")
    assert _active_view(page) == "by-competition"
    page = open_page(report_url + "#view=club-timeline&club=club_a")
    assert _active_view(page) == "club-timeline"


def test_a_hash_that_is_not_ours_leaves_the_page_alone(open_page, report_url):
    page = open_page(report_url)
    _view(page, "combined-list")
    page.focus(".skip-link")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    assert _hash(page) == "#main"
    assert _active_view(page) == "combined-list"


def test_editing_the_hash_moves_the_page(open_page, report_url):
    page = open_page(report_url)
    _go_to(page, "option=2&view=combined-calendar&club=club_b")
    assert _selected_tab(page) == 2
    assert _active_view(page, "#panel-2") == "combined-calendar"
    assert page.locator("#panel-2 .club-select").input_value() == "club_b"
    _go_to(page, "option=1&view=month-calendar")
    assert _selected_tab(page) == 1
    assert page.locator("#panel-1 .club-select").input_value() == ""


def test_back_and_forward_step_through_the_options_and_views(open_page, report_url):
    page = open_page(report_url)
    _view(page, "combined-list")
    page.select_option("#panel-1 .club-select", "club_a")  # refines the entry, adds none
    page.click("#tab-2")
    _view(page, "month-calendar", "#panel-2")
    assert _hash(page).startswith("#option=2&view=month-calendar")

    page.go_back()
    page.wait_for_timeout(150)
    assert _selected_tab(page) == 2 and _active_view(page, "#panel-2") == "combined-list"
    page.go_back()
    page.wait_for_timeout(150)
    assert _selected_tab(page) == 1 and _active_view(page, "#panel-1") == "combined-list"
    assert page.locator("#panel-1 .club-select").input_value() == "club_a"
    page.go_back()
    page.wait_for_timeout(150)
    assert _hash(page) == "" and _active_view(page) == "by-competition"
    assert page.locator("#panel-1 .club-select").input_value() == ""

    page.go_forward()
    page.go_forward()
    page.go_forward()
    page.wait_for_timeout(150)
    assert _selected_tab(page) == 2 and _active_view(page, "#panel-2") == "month-calendar"


def test_choosing_a_club_or_a_day_rewrites_the_entry_instead_of_adding_one(open_page, report_url):
    page = open_page(report_url)
    start = page.evaluate("history.length")
    page.select_option("#panel-1 .club-select", "club_a")
    page.select_option("#panel-1 .club-select", "club_b")
    page.select_option("#panel-1 .lens-select", "consecutive_home_days")
    assert page.evaluate("history.length") == start


# -- ids a link can point at -----------------------------------------------------


def test_every_round_and_fixture_has_one_id_in_every_view(open_page, rules_url):
    page = open_page(rules_url)
    page.click('#panel-1 .kind-btn[data-kind="cup"]')
    page.click('#panel-1 .kind-btn[data-kind="european"]')
    for view in ("by-competition", "combined-calendar", "combined-list", "month-calendar", "club-timeline"):
        if view == "club-timeline":
            page.select_option("#panel-1 .club-select", "club_a")
        _view(page, view)
        if view == "month-calendar":
            page.click('#panel-1 button[data-date="2026-06-06"]')
        ids = page.evaluate("[...document.querySelectorAll('[id^=\"fx-\"], [id^=\"rd-\"]')].map(n => n.id)")
        assert len(ids) == len(set(ids)), view
        drawn = page.evaluate("document.querySelectorAll('.panel:not([hidden]) [data-uid]').length")
        assert len([i for i in ids if i.startswith("fx-")]) == drawn, view
    assert all(re.fullmatch(r"(fx|rd)-[A-Za-z0-9_-]+", i) for i in ids)


def test_a_fixture_link_scrolls_to_it_focuses_it_and_keeps_the_roving_tab_stop(open_page, report_url):
    page = open_page(report_url + f"#view=by-competition&target={LATE_FIXTURE}")
    assert _focused_fixture(page) == LATE_FIXTURE
    box = page.locator(f"#{LATE_FIXTURE}").bounding_box()
    assert 0 <= box["y"] < DESKTOP["height"]
    assert _hash(page) == f"#option=1&view=by-competition&target={LATE_FIXTURE}"
    # Tab leaves the group; Shift+Tab comes back to the same card, not to the group's first.
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    assert _focused_fixture(page) == LATE_FIXTURE


def test_a_bare_fixture_id_works_as_a_link(open_page, report_url):
    page = open_page(report_url + f"#{LATE_FIXTURE}")
    assert _focused_fixture(page) == LATE_FIXTURE
    assert _hash(page).endswith(f"&target={LATE_FIXTURE}")


def test_a_fixture_link_opens_its_folded_round_on_a_phone(open_page, report_url):
    page = open_page(report_url + f"#view=by-competition&target={LATE_FIXTURE}", PHONE)
    round_ = page.locator(f"#{LATE_ROUND}")
    assert "is-open" in round_.get_attribute("class")
    assert _focused_fixture(page) == LATE_FIXTURE
    box = page.locator(f"#{LATE_FIXTURE}").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= PHONE["height"]
    # The round that was not linked stays folded.
    assert "is-open" not in page.locator("#rd-elite-round-1").get_attribute("class")


def test_a_round_link_opens_and_scrolls_to_the_round(open_page, report_url):
    page = open_page(report_url + f"#view=by-competition&target={LATE_ROUND}", PHONE)
    assert "is-open" in page.locator(f"#{LATE_ROUND}").get_attribute("class")
    assert page.evaluate("document.activeElement.id") == LATE_ROUND
    assert page.locator(f"#{LATE_FIXTURE}").is_visible()
    assert _hash(page).endswith(f"&target={LATE_ROUND}")


def test_a_link_to_a_fixture_lifts_the_filter_that_hides_it(open_page, report_url):
    page = open_page(report_url + f"#view=combined-list&club=club_a&target={LATE_FIXTURE}")
    # Club B's fixture is not club A's: the club filter gives way so the link lands.
    assert _focused_fixture(page) == LATE_FIXTURE
    assert page.locator("#panel-1 .club-select").input_value() == ""


def test_a_link_to_a_fixture_in_the_month_view_opens_its_day(open_page, report_url):
    page = open_page(report_url + f"#view=month-calendar&target={LATE_FIXTURE}")
    assert page.locator('#panel-1 button[data-date="2026-06-13"]').get_attribute("aria-expanded") == "true"
    assert _focused_fixture(page) == LATE_FIXTURE
    assert "date=2026-06-13" in _hash(page)


def test_opening_a_card_puts_it_in_the_hash_and_changing_anything_else_drops_it(open_page, report_url):
    page = open_page(report_url)
    page.click(f"#{LATE_FIXTURE} .fx-main")
    assert _hash(page).endswith(f"&target={LATE_FIXTURE}")
    page.click(f"#{LATE_FIXTURE} .fx-main")
    assert "target" not in _hash(page)

    page.click(f"#{LATE_FIXTURE} .fx-main")
    page.select_option("#panel-1 .club-select", "club_b")
    assert "target" not in _hash(page)


def test_a_strip_jump_puts_the_round_in_the_hash(open_page, report_url):
    page = open_page(report_url)
    page.locator("#panel-1 .strip-cell").nth(1).click()
    assert _hash(page).endswith(f"&target={LATE_ROUND}")


# -- export ----------------------------------------------------------------------


def _download(page, button, tmp_path):
    page.click("#panel-1 .export-toggle")
    with page.expect_download() as info:
        page.click(f"#panel-1 {button}")
    download = info.value
    path = tmp_path / download.suggested_filename
    download.save_as(path)
    return download.suggested_filename, path


def _rows(matches):
    return sorted((m.competition_id, m.date, m.home_team, m.away_team, m.venue) for m in matches)


def test_the_export_panel_opens_closes_and_says_what_it_holds(open_page, report_url):
    page = open_page(report_url)
    toggle = page.locator("#panel-1 .export-toggle")
    panel = page.locator("#panel-1 .export-panel")
    assert toggle.get_attribute("aria-expanded") == "false" and panel.is_hidden()
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true" and panel.is_visible()
    assert "Option 1: 3 league fixtures in the CSV, 3 fixtures in the calendar." in panel.locator(".export-scope").inner_text()
    page.keyboard.press("Escape")
    assert panel.is_hidden() and page.evaluate("document.activeElement.classList.contains('export-toggle')")

    page.select_option("#panel-1 .club-select", "club_a")
    toggle.click()
    assert "Option 1, Club A: 2 league fixtures" in panel.locator(".export-scope").inner_text()
    assert panel.locator("a").count() == 0  # the raw JSON is linked only when the report names one


def test_the_csv_reads_back_through_external_schedule(open_page, report_url, tmp_path):
    page = open_page(report_url)
    name, path = _download(page, ".export-csv", tmp_path)
    assert name == "test-option-1.csv"
    text = path.read_bytes().decode("utf-8")
    assert text.startswith("competition,date,home_team,away_team,venue\r\n") and text.endswith("\r\n")

    world = _world()[0]
    loaded, warnings = load_external_schedule(path, world)
    assert _rows(loaded) == _rows(_base_matches())
    assert page.locator("#panel-1 .export-note").inner_text() == f"Downloaded {name}."


def test_the_csv_follows_the_club_filter_and_is_named_for_it(open_page, report_url, tmp_path):
    page = open_page(report_url)
    page.select_option("#panel-1 .club-select", "club_a")
    name, path = _download(page, ".export-csv", tmp_path)
    assert name == "test-option-1-club_a.csv"
    loaded, _ = load_external_schedule(path, _world()[0])
    expected = [m for m in _base_matches() if "a_m" in (m.home_team, m.away_team) or "a_w" in (m.home_team, m.away_team)]
    assert _rows(loaded) == _rows(expected) and len(loaded) == 2


def test_the_csv_holds_league_fixtures_whatever_competitions_are_shown(open_page, report_url, tmp_path):
    page = open_page(report_url)
    page.click('#panel-1 .kind-btn[data-kind="cup"]')
    page.click('#panel-1 .kind-btn[data-kind="european"]')
    _, path = _download(page, ".export-csv", tmp_path)
    loaded, _ = load_external_schedule(path, _world()[0])
    assert len(loaded) == 3


def test_the_second_options_csv_is_its_own_file(open_page, report_url, tmp_path):
    page = open_page(report_url)
    page.click("#tab-2")
    page.click("#panel-2 .export-toggle")
    with page.expect_download() as info:
        page.click("#panel-2 .export-csv")
    assert info.value.suggested_filename == "test-option-2.csv"


def _unfold(text):
    return re.sub(r"\r\n[ \t]", "", text)


def _lf(text):
    """The text with plain newlines, so `^` and `$` in a pattern see each content line whole."""
    return text.replace("\r\n", "\n")


def _events(text):
    return [block.split("\r\n") for block in _unfold(text).split("BEGIN:VEVENT\r\n")[1:]]


def _calendar(page, tmp_path, club=None):
    if club:
        page.select_option("#panel-1 .club-select", club)
    name, path = _download(page, ".export-ics", tmp_path)
    return name, path.read_bytes().decode("utf-8")


def test_the_calendar_is_a_well_formed_icalendar_with_crlf_lines(open_page, report_url, tmp_path):
    page = open_page(report_url)
    name, text = _calendar(page, tmp_path)
    assert name == "test-option-1.ics"
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT") == 3
    assert all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n"))
    uids = re.findall(r"^UID:(.+)$", _lf(text), re.M)
    assert len(uids) == len(set(uids)) == 3
    for event in _events(text):
        fields = {line.split(":")[0].split(";")[0] for line in event if ":" in line}
        assert {"UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "LOCATION"} <= fields
    assert re.search(r"^DTSTAMP:\d{8}T\d{6}Z$", _lf(text), re.M)


def test_kickoffs_are_oslo_times_written_in_utc_and_a_game_without_one_is_a_whole_day(open_page, report_url, tmp_path):
    page = open_page(report_url)
    _, text = _calendar(page, tmp_path)
    by_summary = {next(ln for ln in e if ln.startswith("SUMMARY:")): e for e in _events(text)}
    timed = by_summary["SUMMARY:Club A v Opp1"]
    # 18:00 on 6 June is CEST (UTC+2); two hours long.
    assert "DTSTART:20260606T160000Z" in timed and "DTEND:20260606T180000Z" in timed
    untimed = by_summary["SUMMARY:Club A Kvinner v Opp2"]
    assert "DTSTART;VALUE=DATE:20260607" in untimed and "DTEND;VALUE=DATE:20260608" in untimed


def test_the_calendar_follows_the_club_and_the_competitions_shown(open_page, report_url, tmp_path):
    page = open_page(report_url)
    page.click('#panel-1 .kind-btn[data-kind="cup"]')
    page.click('#panel-1 .kind-btn[data-kind="european"]')
    name, text = _calendar(page, tmp_path, club="club_a")
    assert name == "test-option-1-club_a.ics"
    summaries = re.findall(r"^SUMMARY:(.+)$", _lf(_unfold(text)), re.M)
    assert len(summaries) == 4  # two league games, the cup round and the European leg
    assert any("Union Saint-Gilloise" in s for s in summaries)
    assert all("Club B" not in s for s in summaries)


def test_winter_and_summer_kickoffs_convert_to_utc_across_the_clock_change(tmp_path_factory, open_page, tmp_path):
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 3, 28), "va", kickoff_time="18:00"),  # before: UTC+1
        f.match("elite", "b_m", "opp_m", date(2026, 3, 29), "vb", kickoff_time="18:00", round_index=1),  # after: UTC+2
        f.match("elite", "a_m", "opp_m", date(2026, 10, 24), "va", kickoff_time="18:00", round_index=2),  # summer
        f.match("elite", "b_m", "opp_m", date(2026, 10, 25), "vb", kickoff_time="18:00", round_index=3),  # winter
    ]
    page = open_page(_report_url(tmp_path_factory, matches))
    _, text = _calendar(page, tmp_path)
    starts = re.findall(r"^DTSTART:(\d{8}T\d{6}Z)$", _lf(text), re.M)
    assert starts == ["20260328T170000Z", "20260329T160000Z", "20261024T160000Z", "20261025T170000Z"]


OPPONENT_NEEDING_ESCAPES = "Ålesund; Tromsø, Bodø\\Glimt – the very long-named opponents of the evening FK Æøå"


class _Hour:
    def hours(self, a, b):
        return 1.0


def _european_report(tmp_path_factory, opponent=OPPONENT_NEEDING_ESCAPES, json_name=None):
    """A report with one European tie against `opponent`, for text that needs care in a calendar."""
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_Hour(), detail=True)
    comps = [world.competition("elite"), world.competition("topp")]
    matches = _base_matches()
    candidate = Candidate(
        matches=matches, score=evaluate(matches, [ConsecutiveHomeDays(competitions=comps)], ctx), label="Option 1"
    )
    round_ = f.european_round(
        "q3", forced_date=date(2026, 6, 20), ties=[f.european_tie("a_m", opponent=opponent, home_leg="first")]
    )
    europe = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    path = render_report(
        world,
        season,
        SolverResult(candidates=[candidate], solver="test"),
        tmp_path_factory.mktemp("european") / "report.html",
        european_competitions=[europe],
        resolved_legs={("cl", "q3"): (date(2026, 6, 20), date(2026, 6, 20))},
        json_name=json_name,
    )
    return path.as_uri()


def test_calendar_text_is_escaped_and_long_lines_fold_inside_the_75_octet_limit(tmp_path_factory, open_page, tmp_path):
    page = open_page(_european_report(tmp_path_factory))
    page.click('#panel-1 .kind-btn[data-kind="european"]')
    _, text = _calendar(page, tmp_path)
    lines = text.split("\r\n")
    assert any(line.startswith(" ") for line in lines), "a line was long enough to fold"
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    unfolded = _unfold(text)
    escaped = "Ålesund\\; Tromsø\\, Bodø\\\\Glimt"
    assert escaped in unfolded
    # Unfolding gives back the original characters: no multi-byte one was split.
    assert unfolded.encode("utf-8").decode("utf-8") == unfolded
    assert unfolded.count("\\;") >= 1 and "Æøå" in unfolded


def test_an_empty_selection_disables_the_files(open_page, report_url):
    page = open_page(report_url)
    page.select_option("#panel-1 .club-select", "opp1")
    page.select_option("#panel-1 .club-select", "club_b")
    page.click('#panel-1 .kind-btn[data-kind="league"]')
    page.click("#panel-1 .export-toggle")
    assert page.locator("#panel-1 .export-ics").is_disabled()
    assert page.locator("#panel-1 .export-csv").is_enabled()


def test_the_raw_json_is_linked_when_the_report_has_one(tmp_path_factory, open_page):
    page = open_page(_european_report(tmp_path_factory, json_name="season.json"))
    page.click("#panel-1 .export-toggle")
    link = page.locator("#panel-1 .export-panel a")
    assert link.get_attribute("href") == "season.json" and link.get_attribute("download") is not None


# -- the fairness section is drawn when opened ---------------------------------------


def test_the_fairness_rows_are_built_on_first_opening_and_only_once(open_page, report_url):
    page = open_page(report_url)
    section = page.locator("#panel-1 details.fairness")
    assert section.locator(".fairness-row").count() == 0
    section.locator("summary").click()
    rows = section.locator(".fairness-row")
    rows.first.wait_for()
    bars = rows.first.locator(".fairness-bar")
    assert bars.count() == 2  # the two dual clubs
    assert bars.first.locator(".ffill").evaluate("n => n.style.width") == "100%"
    section.locator("summary").click()
    section.locator("summary").click()
    assert section.locator(".fairness-row").count() == rows.count()


@pytest.mark.parametrize("button", [".export-csv", ".export-ics"])
def test_a_download_leaves_the_panel_open_the_focus_on_the_button_and_the_note_visible(
    open_page, report_url, tmp_path, button
):
    page = open_page(report_url)
    name, _ = _download(page, button, tmp_path)
    assert page.locator("#panel-1 .export-toggle").get_attribute("aria-expanded") == "true"
    assert page.locator("#panel-1 .export-panel").is_visible()
    assert page.evaluate(f"document.activeElement.matches('{button}')")
    note = page.locator("#panel-1 .export-note")
    assert note.is_visible() and note.inner_text() == f"Downloaded {name}."


def test_back_keeps_the_folded_rounds_and_puts_focus_on_the_view_radio(open_page, report_url):
    page = open_page(report_url + "#view=by-competition", PHONE)
    page.click("#rd-elite-round-1 .round-head, #rd-elite-round-1 > :first-child")
    assert "is-open" in page.locator("#rd-elite-round-1").get_attribute("class")
    _view(page, "combined-list")
    _view(page, "by-competition")
    # Focus on something the redraw removes, so only the restore can put it back on a radio.
    page.locator("#rd-elite-round-1 .fx-main").first.focus()
    assert page.evaluate("document.activeElement.classList.contains('view-btn')") is False
    page.go_back()
    page.wait_for_timeout(150)
    page.go_back()
    page.wait_for_timeout(150)
    assert _active_view(page) == "by-competition"
    assert "is-open" in page.locator("#rd-elite-round-1").get_attribute("class")
    assert page.evaluate("document.activeElement.classList.contains('view-btn')")
    assert page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true"
