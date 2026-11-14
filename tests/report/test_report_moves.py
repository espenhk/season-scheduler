"""Fallback moves in the report: the contingency table's rows and the page's move
badges, tooltips and ghost fixtures."""

from __future__ import annotations

import re
from datetime import date

import factories as f
import pytest
from report_support import _FixedTravel, _world

from terminliste.contingency import ContingencyPlan, ContingencyPlanner, Fallback
from terminliste.report.contingency_view import (
    MoveIndex,
    contingency_view,
    moves_wire,
    reach_phrase,
    shift_text,
)
from terminliste.report.render import render_report
from terminliste.scoring.base import EvalContext, ScheduleIndex, evaluate
from terminliste.scoring.soft import ExpectedDisruption
from terminliste.solvers.base import Candidate, SolverResult

SAT = date(2026, 6, 6)
THU = date(2026, 6, 4)
NODE = "a_m/uefa/q3"


def _commitments():
    return f.commitment_set(
        f.commitment("a_m", THU, hard=False, probability=0.5, source="uefa",
                     label="UEFA Champions League: Matchday 1", node_id=NODE),
    )


def _matches():
    return [
        f.match("elite", "a_m", "opp_m", SAT, "va", kickoff_time="18:00"),
        f.match("topp", "b_w", "opp_w", SAT, "vb"),
    ]


def _plan_for(world, season, matches, commitments):
    planner = ContingencyPlanner(world, season, [world.competition("elite"), world.competition("topp")], commitments)
    return planner, planner.analyse(ScheduleIndex(matches)).plan


# -- view data -------------------------------------------------------------------


def test_trigger_wording_and_shift_text():
    assert reach_phrase("UEFA Champions League: Matchday 3") == "the UEFA Champions League league phase"
    assert reach_phrase("NM Cupen 2026/27 (menn): Round 3") == "round 3 of NM Cupen 2026/27 (menn)"
    assert reach_phrase("NM Cupen 2025/26 (menn): Quarter-final") == "the quarter-final of NM Cupen 2025/26 (menn)"
    assert reach_phrase("UEFA Conference League: Third qualifying round (first leg)") == (
        "the third qualifying round of UEFA Conference League"
    )
    assert shift_text(-1) == "−1 day" and shift_text(2) == "+2 days" and shift_text(1) == "+1 day"


def test_a_match_gets_one_row_per_destination_with_its_triggers_as_chips():
    world, season = _world()
    commitments = _commitments()
    match = f.match("elite", "a_m", "opp_m", SAT, "va", kickoff_time="18:00")
    plan = ContingencyPlan(
        fallbacks={match.key: Fallback(date(2026, 6, 5), "va", "18:00", (NODE,), "same_slot")}
    )

    view = contingency_view(world, plan, [match], commitments)

    assert view["moves"] == 1
    (row,) = view["rows"]
    assert (row["from"], row["to"], row["shift"], row["kind"]) == (
        "Sat 6 Jun", "Fri 5 Jun", "−1 day", "same round, another day"
    )
    assert row["triggers"] == [{"team": world.team_label("a_m"), "round": "UEFA Champions League league phase", "pct": 50}]
    assert row["key"] == "elite-2026-06-06-a_m-opp_m"


def test_triggers_leading_to_the_same_day_combine_into_one_destination_with_the_planners_probability():
    world, season = _world()
    other = "a_m/uefa/q4"
    commitments = f.commitment_set(
        f.commitment("a_m", THU, hard=False, probability=0.5, source="uefa", label="UEFA Europa League: Matchday 1", node_id=NODE),
        f.commitment("a_m", date(2026, 6, 8), hard=False, probability=0.25, source="uefa", label="UEFA Conference League: Matchday 1", node_id=other),
    )
    match = f.match("elite", "a_m", "opp_m", SAT, "va")
    plan = ContingencyPlan(
        fallbacks={match.key: Fallback(date(2026, 6, 5), "va", None, (NODE, other), "same_slot")}
    )

    index = MoveIndex(world, plan, [match], commitments)
    triggers, wire = moves_wire(index)

    (dest,) = wire[match.key]["g"]
    assert dest["p"] == pytest.approx(1 - 0.5 * 0.75)
    assert [t[0] for t in dest["t"]] == [0, 1]
    # Each trigger is dated by the commitment nearest the match, which is what forces the move.
    assert [t[1] for t in dest["t"]] == ["2026-06-04", "2026-06-08"]
    assert [t[3] for t in triggers] == [50, 25]
    assert "k" in dest and "h" not in dest and "v" not in dest
    assert len(contingency_view(world, plan, [match], commitments, index)["rows"]) == 1


def test_different_destinations_under_different_triggers_are_separate_rows_and_wire_entries():
    world, season = _world()
    other = "a_m/uefa/q4"
    commitments = f.commitment_set(
        f.commitment("a_m", THU, hard=False, probability=0.5, source="uefa", label="UEFA Europa League: Matchday 1", node_id=NODE),
        f.commitment("a_m", date(2026, 6, 8), hard=False, probability=0.25, source="uefa", label="UEFA Conference League: Matchday 1", node_id=other),
    )
    match = f.match("elite", "a_m", "opp_m", SAT, "va", kickoff_time="18:00")

    class Split(ContingencyPlan):
        def moves(self, matches):
            from terminliste.contingency import ContingencyMove

            return [
                ContingencyMove(match.key, NODE, SAT, date(2026, 6, 5), "same_slot"),
                ContingencyMove(match.key, other, SAT, date(2026, 6, 9), "next_free_slot"),
            ]

    plan = Split(fallbacks={match.key: Fallback(date(2026, 6, 5), "vc", "20:00", (NODE, other), "same_slot")})

    view = contingency_view(world, plan, [match], commitments)
    _, wire = moves_wire(MoveIndex(world, plan, [match], commitments))

    assert [(r["to"], r["shift"], r["kind"]) for r in view["rows"]] == [
        ("Fri 5 Jun", "−1 day", "same round, another day"),
        ("Tue 9 Jun", "+3 days", "next free slot"),
    ]
    assert view["moves"] == 1
    assert [(d["d"], d["k"], d["p"]) for d in wire[match.key]["g"]] == [
        ("2026-06-05", 0, 0.5), ("2026-06-09", 2, 0.25)
    ]
    # Kickoff and ground are sent only because they differ from the match's own.
    assert wire[match.key]["g"][0]["h"] == "20:00" and wire[match.key]["g"][0]["v"] == "vc"


def test_an_uncovered_match_keeps_its_triggers_and_is_listed_without_a_row():
    world, season = _world()
    commitments = _commitments()
    matches = _matches()
    key = matches[0].key
    plan = ContingencyPlan(uncovered=[key], uncovered_triggers={key: (NODE,)})

    view = contingency_view(world, plan, matches, commitments)
    _, wire = moves_wire(MoveIndex(world, plan, matches, commitments))

    assert view["rows"] == [] and view["moves"] == 0 and len(view["uncovered"]) == 1
    assert wire[key]["x"] == {"t": [[0, "2026-06-04"]], "p": 0.5}


def test_the_planner_records_what_exposes_a_match_it_finds_no_fallback_for():
    world, season = _world()
    league = world.competition("elite")
    final = f.match("elite", "a_m", "opp_m", SAT, "va", round_index=league.rounds - 1)

    _, plan = _plan_for(world, season, [final], _commitments())

    assert plan.uncovered == [final.key]
    assert plan.uncovered_triggers == {final.key: (NODE,)}


# -- the page ----------------------------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def moves_url(tmp_path_factory):
    world, season = _world()
    commitments = _commitments()
    matches = _matches()
    comps = [world.competition("elite"), world.competition("topp")]
    planner, plan = _plan_for(world, season, matches, commitments)
    assert matches[0].key in plan.fallbacks
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    score = evaluate(matches, [ExpectedDisruption(planner=planner)], ctx)
    candidate = Candidate(matches=matches, score=score, label="Option 1", contingency_plan=plan)
    path = render_report(
        world, season, SolverResult(candidates=[candidate], solver="test"),
        tmp_path_factory.mktemp("moves") / "report.html", commitments=commitments,
    )
    return path.as_uri(), plan.fallbacks[matches[0].key].date, comps


@pytest.fixture
def page(browser, moves_url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.goto(moves_url[0])
    yield page
    assert page.problems == []
    page.close()


def _day(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def test_the_badge_names_the_destination_and_the_chance(page, moves_url):
    badge = page.locator("#panel-1 .flag-move").first
    assert badge.inner_text() == f"{_day(moves_url[1].isoformat())} · 50%"
    label = badge.get_attribute("aria-label")
    assert label.startswith(badge.inner_text()) and "may move to" in label
    assert badge.get_attribute("data-rule") == "expected_disruption"


def test_the_badge_tooltip_is_a_from_to_card_with_its_trigger_and_chance(page):
    page.locator("#panel-1 .flag-move").first.hover()
    tip = page.locator("#flag-tooltip")
    tip.wait_for(state="visible")
    text = " ".join(tip.inner_text().split())
    assert text.startswith("May move · ")
    assert "NOW" in text and "IF MOVED" in text and "18:00" in text
    assert "same round, another day" in text
    assert "If " in text and "reach the UEFA Champions League league phase" in text
    assert "plays Thu 4 Jun" in text
    assert "50% chance · −1.5 pts expected" in text or re.search(r"50% chance · −\d\.\d pts expected", text)
    assert "highlight this match" in text


def test_a_ghost_stands_on_the_destination_day_and_is_not_counted(page, moves_url):
    dest = moves_url[1].isoformat()
    block = page.locator("#panel-1 .view-pane[data-view='by-competition'] .round", has=page.locator(".fx-ghost"))
    ghost = block.locator(".fx-ghost")
    assert ghost.count() == 1 and ghost.get_attribute("data-uid") is None
    assert "may move here from Sat 6 Jun" in ghost.inner_text()
    assert any(text.startswith(_day(dest)) for text in block.locator(".day-head").all_inner_texts())
    # Ghosts are no fixture: the round still counts its real games only.
    assert block.locator("[data-uid]").count() == len(block.locator(".fx").all())
    assert "1 game" in block.locator(".rsum").inner_text()


def test_the_ghost_follows_the_filters_of_its_match(page):
    ghosts = "#panel-1 .view-pane[data-view='by-competition'] .fx-ghost"
    page.select_option("#panel-1 .club-select", "club_b")
    page.click('#panel-1 .view-btn[data-view="by-competition"]')
    assert page.locator(ghosts).count() == 0
    page.select_option("#panel-1 .club-select", "club_a")
    page.click('#panel-1 .view-btn[data-view="by-competition"]')
    assert page.locator(ghosts).count() == 1
    assert "1 of 2 fixtures shown" in page.locator("#panel-1 .scope-count").inner_text()


def test_clicking_the_badge_flashes_the_match_and_its_ghost(page):
    page.locator("#panel-1 .flag-move").first.click()
    assert page.locator("#panel-1 .flash").count() == 2
    assert page.input_value("#panel-1 .lens-select") == "expected_disruption"


def test_clicking_a_ghost_finds_its_match(page):
    page.locator("#panel-1 .fx-ghost .ghost-main").first.click()
    assert page.locator("#panel-1 .fx.flash").count() == 1


def test_the_month_calendar_marks_the_destination_day_and_lists_the_ghost(page, moves_url):
    dest = moves_url[1].isoformat()
    page.click('#panel-1 .view-btn[data-view="month-calendar"]')
    body = page.locator('#panel-1 .view-pane[data-view="month-calendar"] .pane-body')
    cell = body.locator(f'.cal-day[data-date="{dest}"]')
    assert cell.locator(".cal-move").count() == 1
    assert "may move here" in cell.get_attribute("aria-label")
    assert body.locator(f'.cal-day[data-date="{SAT.isoformat()}"] .cal-move').count() == 0
    cell.click()
    assert body.locator(".day-detail .fx-ghost").count() == 1
    assert body.locator(".day-detail [data-uid]").count() == len(body.locator(".day-detail .day-row").all())


def test_the_contingency_table_links_to_the_match_in_the_calendar(page, moves_url):
    page.locator("#panel-1 details.contingency > summary").click()
    row = page.locator("#panel-1 .contingency-table tbody tr").first
    cells = [c.strip() for c in row.locator("td").all_inner_texts()]
    assert cells[1] == "Sat 6 Jun" and cells[2] == _day(moves_url[1].isoformat())
    assert cells[3] in ("−1 day", "+1 day") or re.fullmatch(r"[+−]\d+ days?", cells[3])
    assert "50%" in cells[4] and cells[5] == "same round, another day"
    row.locator("a.cont-link").click()
    page.wait_for_function("document.querySelector('#panel-1 .fx.flash')")
    assert page.locator("#panel-1 .fx.flash").count() == 1


@pytest.fixture(scope="module")
def uncovered_url(tmp_path_factory):
    world, season = _world()
    commitments = _commitments()
    final = f.match("elite", "a_m", "opp_m", SAT, "va", round_index=world.competition("elite").rounds - 1)
    planner, plan = _plan_for(world, season, [final], commitments)
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    score = evaluate([final], [ExpectedDisruption(planner=planner)], ctx)
    candidate = Candidate(matches=[final], score=score, label="Option 1", contingency_plan=plan)
    path = render_report(
        world, season, SolverResult(candidates=[candidate], solver="test"),
        tmp_path_factory.mktemp("uncovered") / "report.html", commitments=commitments,
    )
    return path.as_uri()


def test_a_match_with_no_fallback_says_so_on_its_badge_and_in_its_tooltip(browser, uncovered_url):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(uncovered_url)
    badge = page.locator("#panel-1 .flag-nofallback").first
    assert badge.inner_text() == "no fallback"
    assert "no fallback date" in badge.get_attribute("aria-label")
    assert page.locator("#panel-1 .fx-ghost").count() == 0
    badge.hover()
    page.locator("#flag-tooltip").wait_for(state="visible")
    assert page.locator("#flag-tooltip").inner_text().startswith("No fallback")
    page.close()
