"""Accessibility of the report page (#41): an automated axe-core pass over every view,
option, colour scheme and width, and the keyboard flows axe cannot judge.

Uses the browser fixtures of `conftest.py` and skips under the same conditions.
"""

from __future__ import annotations

import itertools
import re

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
axe_api = pytest.importorskip("axe_playwright_python.sync_playwright")

from report_support import DAY  # noqa: E402

VIEWS = ("by-competition", "month-calendar", "combined-calendar", "combined-list", "club-timeline")
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "phone": {"width": 390, "height": 844}}


def _open(browser, url, scheme="light", viewport="desktop"):
    phone = viewport == "phone"
    context = browser.new_context(
        viewport=VIEWPORTS[viewport], color_scheme=scheme, has_touch=phone, is_mobile=phone
    )
    page = context.new_page()
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(url)
    page.context_ref = context
    return page


@pytest.fixture
def kb(browser, rules_url):
    page = _open(browser, rules_url)
    yield page
    assert page.problems == []
    page.context_ref.close()


def _view(page, name, panel="#panel-1"):
    button = page.locator(f'{panel} .view-btn[data-view="{name}"]')
    button.click()
    return page.locator(f'{panel} .view-pane[data-view="{name}"] .pane-body')


def _active(page, expression="n => n.id"):
    return page.evaluate(f"({expression})(document.activeElement)")


# -- axe ---------------------------------------------------------------------


def _violations(page, label):
    found = []
    for violation in axe_api.Axe().run(page).response["violations"]:
        for node in violation["nodes"]:
            found.append(f"{label}: {violation['impact']} {violation['id']} {node['target']}")
    return found


def _open_filters(page, panel):
    toggle = page.locator(f"{panel} .filters-toggle")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
        toggle.click()


def _walk_the_page(page, club="club_a"):
    """Visits each option and view, opening what a reader opens, and axe-checks every state."""
    found = _violations(page, "opened")
    for panel_number in (1, 2):
        panel = f"#panel-{panel_number}"
        if panel_number == 2:
            page.click("#tab-2")
            found += _violations(page, "option 2")
        for kind in ("cup", "european"):
            _open_filters(page, panel)
            button = page.locator(f'{panel} .kind-btn[data-kind="{kind}"]')
            if button.get_attribute("aria-pressed") != "true":
                button.click()
        page.evaluate(
            "p => document.querySelectorAll(p + ' .cols details.item, ' + p + ' .section-list > details')"
            ".forEach(d => { d.open = true; })",
            panel,
        )
        found += _violations(page, f"{panel} rules open")
        for view in VIEWS if panel_number == 1 else ("combined-list",):
            body = _view(page, view, panel)
            page.evaluate(
                "b => b.querySelectorAll('details').forEach(d => { d.open = true; })",
                body.element_handle(),
            )
            if view == "month-calendar":
                body.locator(".cal-day[data-date]").nth(2).click()
            elif view == "by-competition":
                page.evaluate(
                    "p => document.querySelectorAll(p + ' .round-toggle[aria-expanded=false]')"
                    ".forEach(b => b.click())",
                    panel,
                )
                body.locator(".fx-main").first.click()
            elif view == "combined-list":
                page.evaluate(
                    "p => document.querySelectorAll(p + ' .fx-group-toggle').forEach(b => b.click())",
                    panel,
                )
            elif view == "club-timeline":
                _open_filters(page, panel)
                page.select_option(f"{panel} .club-select", club)
                body.locator(".tl-main").first.click()
            found += _violations(page, f"{panel} {view}")
        page.click(f"{panel} .legend-toggle")
        found += _violations(page, f"{panel} legend")
        page.click(f"{panel} .legend-toggle")
    return found


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_axe_finds_no_violations_in_any_view(browser, rules_url, scheme, viewport):
    page = _open(browser, rules_url, scheme, viewport)
    found = _walk_the_page(page)
    assert page.problems == []
    page.context_ref.close()
    assert not found, "\n".join(found)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_axe_finds_no_violations_in_the_months_listed_apart(browser, other_months_url, scheme):
    page = _open(browser, other_months_url, scheme)
    _view(page, "month-calendar").locator(".other-date").first.click()
    found = _violations(page, "other dates")
    page.context_ref.close()
    assert not found, "\n".join(found)


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_axe_finds_no_violations_with_the_export_panel_open_and_a_link_applied(browser, rules_url, scheme, viewport):
    page = _open(browser, rules_url, scheme, viewport)
    page.goto(rules_url + "#option=2&view=combined-list&club=club_a")
    page.click("#panel-2 .export-toggle")
    found = _violations(page, "export open")
    page.press("body", "Escape")
    page.locator("#panel-2 details.fairness > summary").click()
    found += _violations(page, "fairness open")
    assert page.problems == []
    page.context_ref.close()
    assert not found, "\n".join(found)


def test_the_export_button_and_panel_are_a_labelled_disclosure_reachable_by_keyboard(kb):
    toggle = kb.locator("#panel-1 .export-toggle")
    assert toggle.get_attribute("aria-controls") == "export-1"
    toggle.focus()
    kb.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true"
    kb.keyboard.press("Tab")
    assert kb.evaluate("document.activeElement.classList.contains('export-csv')")
    kb.keyboard.press("Tab")
    assert kb.evaluate("document.activeElement.classList.contains('export-ics')")
    kb.keyboard.press("Escape")
    assert toggle.get_attribute("aria-expanded") == "false"
    assert kb.evaluate("document.activeElement.classList.contains('export-toggle')")


# -- landmarks, headings, names ---------------------------------------------


def test_the_page_has_a_skip_link_a_main_landmark_and_one_h1(kb):
    assert kb.locator("main#main").count() == 1
    assert kb.locator("h1").count() == 1
    assert (
        kb.get_by_role("link", name="Skip to the schedule options").get_attribute("href") == "#main"
    )


def test_headings_never_skip_a_level_in_any_view(kb):
    for view in VIEWS:
        _view(kb, view)
        if view == "club-timeline":
            kb.select_option("#panel-1 .club-select", "club_a")
        levels = kb.evaluate(
            "Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))"
            ".filter(h => h.getClientRects().length).map(h => +h.tagName[1])"
        )
        assert levels[0] == 1
        assert all(b - a <= 1 for a, b in itertools.pairwise(levels)), (view, levels)


def test_the_option_panel_and_its_fixtures_have_headings(kb):
    names = kb.locator("#panel-1 h2").all_inner_texts()
    assert names == ["Option 1: score and rules", "Option 1: fixtures"]


def test_a_rules_description_is_in_the_page_for_assistive_technology_not_only_a_tooltip(kb):
    kb.locator("#panel-1 details.season-part", has_text="Full score breakdown").locator(
        "summary"
    ).first.click()
    rows = kb.locator("#panel-1 table.breakdown .phone-desc")
    assert rows.count() > 0
    clip = rows.first.evaluate("n => getComputedStyle(n).clip")
    assert rows.first.inner_text().strip() and clip != "auto"  # drawn off-screen, still in the tree


# -- option tabs --------------------------------------------------------------


def test_option_tabs_follow_the_aria_tabs_pattern(kb):
    tabs = kb.get_by_role("tablist", name="Schedule options").get_by_role("tab")
    assert tabs.count() == 2
    for number in (1, 2):
        assert kb.get_attribute(f"#tab-{number}", "aria-controls") == f"panel-{number}"
        assert kb.get_attribute(f"#panel-{number}", "aria-labelledby") == f"tab-{number}"
        assert kb.get_attribute(f"#panel-{number}", "role") == "tabpanel"
    assert [t.get_attribute("tabindex") for t in tabs.all()] == ["0", "-1"]
    # The visible pieces run together without spaces, so the name spells them out.
    assert re.fullmatch(r"Option 1, score [\d.]+", tabs.first.get_attribute("aria-label"))


def test_arrow_keys_move_between_option_tabs_and_enter_opens_one(kb):
    kb.focus("#tab-1")
    kb.keyboard.press("ArrowRight")
    assert _active(kb) == "tab-2"
    assert kb.get_attribute("#tab-1", "aria-selected") == "true"  # moving does not open
    assert kb.locator("#panel-2").is_hidden()
    kb.keyboard.press("Enter")
    assert kb.locator("#panel-2").is_visible() and kb.locator("#panel-1").is_hidden()
    assert kb.get_attribute("#tab-2", "aria-selected") == "true"
    assert [kb.get_attribute(f"#tab-{n}", "tabindex") for n in (1, 2)] == ["-1", "0"]
    kb.keyboard.press("ArrowRight")  # wraps
    assert _active(kb) == "tab-1"
    kb.keyboard.press("End")
    assert _active(kb) == "tab-2"
    kb.keyboard.press("Home")
    assert _active(kb) == "tab-1"


# -- view switcher ------------------------------------------------------------


def test_the_view_switcher_is_a_labelled_radio_group_with_arrow_key_selection(kb):
    group = kb.get_by_role("radiogroup", name="View")
    radios = group.get_by_role("radio")
    assert radios.count() == 5

    def checked():
        return [
            r.get_attribute("data-view")
            for r in radios.all()
            if r.get_attribute("aria-checked") == "true"
        ]

    assert checked() == ["by-competition"]
    assert [r.get_attribute("tabindex") for r in radios.all()] == ["0", "-1", "-1", "-1", "-1"]
    radios.first.focus()
    kb.keyboard.press("ArrowRight")
    assert checked() == ["month-calendar"]
    assert kb.evaluate("document.activeElement.dataset.view") == "month-calendar"
    assert kb.locator('#panel-1 .view-pane[data-view="month-calendar"]').is_visible()
    kb.keyboard.press("End")
    assert checked() == ["club-timeline"]
    kb.keyboard.press("ArrowRight")  # wraps
    assert checked() == ["by-competition"]
    kb.keyboard.press("ArrowLeft")
    assert checked() == ["club-timeline"]
    assert [r.get_attribute("tabindex") for r in radios.all()] == ["-1", "-1", "-1", "-1", "0"]


# -- skip link ----------------------------------------------------------------


def test_the_skip_link_is_first_in_the_tab_order_shows_on_focus_and_lands_in_main(kb):
    kb.keyboard.press("Tab")
    link = kb.get_by_role("link", name="Skip to the schedule options")
    assert _active(kb, "n => n.className") == "skip-link"
    assert link.bounding_box()["y"] >= 0
    kb.keyboard.press("Enter")
    assert _active(kb) == "main"
    kb.keyboard.press("Tab")
    assert kb.evaluate("!!document.activeElement.closest('main')")


# -- fixtures -----------------------------------------------------------------


def test_each_card_is_named_by_a_full_sentence(kb):
    body = _view(kb, "by-competition")
    control = body.locator(".fx-main").first
    assert control.locator(".sr-only").text_content() == (
        "Saturday 6 June 18:00, Club A at home to Opp1, Va. elite round 1. Breaks a hard rule."
    )
    assert kb.get_by_role(
        "button", name=re.compile(r"^Saturday 6 June 18:00, Club A at home")
    ).count()
    # The abbreviated line is out of speech, so the sentence is not read twice.
    assert control.locator(".fx-visual").get_attribute("aria-hidden") == "true"


def test_a_badge_names_its_rule_and_says_whether_it_helps_or_costs(kb):
    body = _view(kb, "by-competition")
    names = [b.get_attribute("aria-label") for b in body.locator(".flag").all()]
    assert names and all(", " in n for n in names)
    assert any(
        n.endswith("pts") or n.endswith("broken rule") or n.endswith("forgone") for n in names
    )


def test_each_group_of_cards_and_badges_is_one_tab_stop_walked_with_the_arrow_keys(kb):
    _view(kb, "by-competition")
    groups = kb.evaluate(
        """() => Array.from(document.querySelectorAll('[data-rove-group]')).map(g => {
          const items = Array.from(g.querySelectorAll('[data-rove]'))
            .filter(i => i.closest('[data-rove-group]') === g && i.getClientRects().length);
          return { items: items.length, stops: items.filter(i => i.tabIndex === 0).length };
        }).filter(g => g.items)"""
    )
    assert groups and all(g["stops"] == 1 for g in groups)
    assert any(g["items"] >= 3 for g in groups)  # otherwise the walk below proves nothing
    kb.evaluate(
        """() => {
          const busiest = Array.from(document.querySelectorAll('[data-rove-group]'))
            .find(g => g.querySelectorAll('[data-rove]').length >= 3);
          busiest.setAttribute('data-walk', '');
        }"""
    )

    def position():
        return kb.evaluate(
            "() => Array.from(document.querySelectorAll('[data-walk] [data-rove]'))"
            ".indexOf(document.activeElement)"
        )

    kb.locator("[data-walk] [data-rove]").first.focus()
    kb.keyboard.press("ArrowRight")
    assert position() == 1
    kb.keyboard.press("End")
    assert position() == kb.locator("[data-walk] [data-rove]").count() - 1
    kb.keyboard.press("ArrowLeft")
    assert position() == kb.locator("[data-walk] [data-rove]").count() - 2
    kb.keyboard.press("Home")
    assert position() == 0
    kb.keyboard.press(
        "ArrowRight"
    )  # the stop follows focus, so Tab out and Shift+Tab back return here
    kb.keyboard.press("Tab")
    assert kb.evaluate("() => !document.activeElement.closest('[data-walk]')")
    kb.keyboard.press("Shift+Tab")
    assert position() == 1


def test_enter_and_space_open_and_close_a_cards_detail_line(kb):
    body = _view(kb, "by-competition")
    card = body.locator(".fx").first
    control = card.locator(".fx-main")
    control.focus()
    kb.keyboard.press("Enter")
    assert card.locator(".fx-detail").is_visible()
    assert control.get_attribute("aria-expanded") == "true"
    assert " ".join(card.locator(".fx-detail").inner_text().split()) == "Club A v Opp1 · Va"
    kb.keyboard.press("Space")
    assert card.locator(".fx-detail").count() == 0
    assert control.get_attribute("aria-expanded") == "false"


def test_a_timeline_card_opens_its_detail_from_the_keyboard_too(kb):
    kb.select_option("#panel-1 .club-select", "club_a")
    body = _view(kb, "club-timeline")
    control = body.locator(".tl-main").first
    control.focus()
    kb.keyboard.press("Enter")
    assert control.get_attribute("aria-expanded") == "true"
    assert body.locator(".tl-fx .fx-detail").first.is_visible()


# -- month calendar -----------------------------------------------------------


def test_the_calendar_is_one_tab_stop_with_readable_day_names(kb):
    body = _view(kb, "month-calendar")
    days = body.locator(".cal-day[data-date]")
    assert days.evaluate_all("els => els.filter(e => e.tabIndex === 0).length") == 1
    label = body.locator(f'.cal-day[data-date="{DAY}"]').get_attribute("aria-label")
    assert label.startswith("Saturday 6 June 2026: ") and "2026-06-06" not in label


def test_picking_a_day_announces_its_games_and_tab_reaches_them(kb):
    body = _view(kb, "month-calendar")
    day = body.locator(f'.cal-day[data-date="{DAY}"]')
    day.focus()
    kb.keyboard.press("Enter")
    live = body.locator('[role="status"]')
    text = live.inner_text()
    assert text.startswith("Saturday 6 June 2026: ") and "shown just below" in text
    assert kb.get_by_role("region", name="Games on Saturday 6 June 2026").is_visible()
    assert _active(kb, "n => n.dataset.date") == DAY  # focus stays, so the arrows still walk days
    kb.keyboard.press("Tab")
    assert kb.evaluate("!!document.activeElement.closest('#cal-day-detail')")
    day.focus()
    kb.keyboard.press("Enter")
    assert live.inner_text() == "Day details closed."


def test_arrow_keys_in_the_calendar_move_the_single_tab_stop(kb):
    body = _view(kb, "month-calendar")
    body.locator(f'.cal-day[data-date="{DAY}"]').focus()
    kb.keyboard.press("ArrowRight")
    assert _active(kb, "n => n.dataset.date") == "2026-06-07"
    assert body.locator(".cal-day[data-date]").evaluate_all(
        "els => els.filter(e => e.tabIndex === 0).map(e => e.dataset.date)"
    ) == ["2026-06-07"]
    kb.keyboard.press("End")
    assert _active(kb, "n => n.dataset.date") == "2026-06-30"
    kb.keyboard.press("Home")
    assert _active(kb, "n => n.dataset.date") == "2026-06-01"


# -- colour is never the only signal -------------------------------------------


def test_edges_and_badges_differ_without_colour(kb):
    styles = kb.evaluate(
        """() => {
          const out = {};
          for (const cls of ['edge-good', 'edge-cost', 'edge-broken']) {
            const n = document.createElement('span');
            n.className = 'fx ' + cls;
            document.body.appendChild(n);
            out[cls] = getComputedStyle(n).borderLeftStyle;
            n.remove();
          }
          for (const cls of ['flag-good', 'flag-cost', 'flag-broken']) {
            const n = document.createElement('span');
            n.className = 'flag ' + cls;
            document.body.appendChild(n);
            out[cls] = getComputedStyle(n, '::before').content;
            n.remove();
          }
          return out;
        }"""
    )
    assert len({styles["edge-good"], styles["edge-cost"], styles["edge-broken"]}) == 3
    assert len({styles["flag-good"], styles["flag-cost"], styles["flag-broken"]}) == 3
    assert all(
        styles[k] not in ("none", "normal") for k in ("flag-good", "flag-cost", "flag-broken")
    )


# -- review follow-ups ---------------------------------------------------------


def test_a_club_scope_rule_row_prints_its_description_where_the_icon_cannot_be_focused(kb):
    kb.select_option("#panel-1 .club-select", "club_a")
    descriptions = kb.locator("#panel-1 .scope-card summary.top .rule-desc")
    assert descriptions.count() > 0
    for description in descriptions.all():
        assert description.bounding_box()["width"] > 1
        assert description.inner_text().strip()


def test_leaving_the_tabs_puts_the_tab_stop_back_on_the_open_option(kb):
    kb.focus("#tab-1")
    kb.keyboard.press("ArrowRight")
    assert _active(kb) == "tab-2"
    kb.keyboard.press("Tab")
    assert kb.evaluate("!document.activeElement.closest('[role=tablist]')")
    kb.keyboard.press("Shift+Tab")
    assert _active(kb) == "tab-1"
    assert [kb.get_attribute(f"#tab-{n}", "tabindex") for n in (1, 2)] == ["0", "-1"]


def test_the_season_strip_is_one_tab_stop_walked_with_the_arrow_keys(kb):
    cells = kb.locator("#panel-1 .strip-cell:not([disabled])")
    assert cells.count() >= 3
    assert cells.evaluate_all("els => els.filter(e => e.tabIndex === 0).length") == 1
    cells.first.focus()
    kb.keyboard.press("ArrowRight")
    assert cells.evaluate_all("els => els.indexOf(document.activeElement)") == 1
    kb.keyboard.press("End")
    assert cells.evaluate_all("(els) => els.indexOf(document.activeElement)") == cells.count() - 1
    kb.keyboard.press("Home")
    assert cells.evaluate_all("els => els.indexOf(document.activeElement)") == 0
    assert cells.evaluate_all("els => els.filter(e => e.tabIndex === 0).length") == 1


def test_a_neutral_row_draws_no_bar_but_a_small_gain_or_cost_keeps_a_visible_one(kb):
    widths = kb.evaluate(
        """() => {
          const out = {};
          for (const cls of ['neutral', 'gain', 'cost']) {
            const box = document.createElement('span');
            box.className = 'contrib ' + cls;
            box.innerHTML = '<span class="share-track"><span class="share-fill" style="width:0%"></span></span>';
            document.body.appendChild(box);
            out[cls] = box.querySelector('.share-fill').getBoundingClientRect().width;
            box.remove();
          }
          return out;
        }"""
    )
    assert widths["neutral"] == 0
    assert widths["gain"] >= 6 and widths["cost"] >= 6
