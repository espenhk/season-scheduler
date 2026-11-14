"""Re-planning mid-season through the CLI: frozen rounds stay byte-identical
in the output, known results narrow the UEFA cascade, and only what the change
touches moves (#13)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False

ROOT = Path(__file__).resolve().parent.parent.parent
# Mid-August: the third qualifying rounds are over, the play-offs still ahead.
FREEZE = "2026-08-18"
# Brann and Tromsø went out in the third qualifying round, Viking lost their play-off.
OUTCOMES = """\
brann_m:
  conference_league_2026: {q3: lost}
tromso_m:
  europa_league_2026: {q3: lost}
viking_m:
  champions_league_2026: {playoff: lost}
"""
LATE_LOCKED = "2026-11-08"


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )


def _generate(*args: str) -> subprocess.CompletedProcess:
    return _cli("generate", "--top-n", "1", "--iterations", "30000", *args)


def _option(path: Path) -> dict:
    return json.loads(path.read_text())["options"][0]


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    work = tmp_path_factory.mktemp("replan")
    base = _generate("--out", str(work / "base"))
    assert base.returncode == 0, base.stderr
    (work / "results.yml").write_text(OUTCOMES)

    # The schedule to revise is the base with one stale entry: Viking's second
    # meeting with Sarpsborg 08 sits on a day Viking's Europa League league
    # phase (now certain) rules out. One late match is locked.
    stale = json.loads((work / "base" / "2026.json").read_text())
    rows = stale["options"][0]["matches"]
    victim = next(m for m in rows if (m["home"], m["away"], m["leg"]) == ("viking_m", "sarpsborg08_m", 2))
    assert victim["date"] > "2026-09-18"
    # A day beside Viking's 17 Sep league-phase match that leaves both clubs'
    # rest otherwise intact, so the commitment is the only thing it breaks.
    teams = {victim["home"], victim["away"]}
    played = [
        date.fromisoformat(m["date"])
        for m in rows
        if m is not victim and teams & {m["home"], m["away"]}
    ]
    victim["date"] = next(
        day.isoformat()
        for day in (date(2026, 9, 18), date(2026, 9, 16), date(2026, 9, 19), date(2026, 9, 15))
        if all(abs((day - other).days) > 2 for other in played)
    )
    locked = next(m for m in rows if m["date"] >= LATE_LOCKED and m is not victim)
    locked["locked"] = True
    (work / "stale.json").write_text(json.dumps(stale))
    return work, victim, locked


def _revise(work: Path, name: str, *extra: str) -> subprocess.CompletedProcess:
    return _generate(
        "--from-schedule", str(work / "stale.json"),
        "--freeze-before", FREEZE,
        "--outcomes", str(work / "results.yml"),
        "--out", str(work / name),
        *extra,
    )


@pytest.fixture(scope="module")
def local(work):
    folder, victim, locked = work
    run = _revise(folder, "local")
    assert run.returncode == 0, run.stderr
    return run, _option(folder / "local" / "2026.json")


def _identity(row: dict) -> tuple:
    return (row["competition"], row["round"], *sorted((row["home"], row["away"])))


def _row_texts(rows: list[dict]) -> dict:
    return {_identity(r): json.dumps(r, sort_keys=True) for r in rows}


def _check_revision(work, victim, locked, revised: dict) -> None:
    stale = _option(work / "stale.json")["matches"]
    after = _row_texts(revised["matches"])
    frozen = [m for m in stale if m["date"] < FREEZE or m.get("locked")]

    assert len(revised["matches"]) == len(stale)
    assert len([m for m in frozen if m["date"] < FREEZE]) > 150
    # Byte for byte, kickoff time and the locked flag included.
    assert all(after[_identity(m)] == json.dumps(m, sort_keys=True) for m in frozen)
    assert revised["hard_violations"] == 0

    moved = revised["replan"]["moved"]
    # The stale match is repaired; anything else that moves sits beside a round
    # the outcomes just made certain, which depends on where the base put it.
    key = (victim["home"], victim["away"], victim["date"])
    repaired = [m for m in moved if (m["home"], m["away"], m["from"]) == key]
    assert len(repaired) == 1 and repaired[0]["to"] >= FREEZE
    settled = {"viking_m", "tromso_m", "brann_m"}
    assert all({m["home"], m["away"]} & settled for m in moved)
    assert revised["replan"]["frozen"] == len(frozen)


def test_the_local_solver_keeps_frozen_matches_and_repairs_only_the_stale_one(work, local):
    folder, victim, locked = work
    _check_revision(folder, victim, locked, local[1])


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs OR-Tools")
def test_cpsat_keeps_frozen_matches_and_repairs_only_the_stale_one(work):
    folder, victim, locked = work
    run = _revise(folder, "cpsat", "--solver", "cpsat", "--time-budget", "60")
    assert run.returncode == 0, run.stderr
    _check_revision(folder, victim, locked, _option(folder / "cpsat" / "2026.json"))


def test_known_results_retire_and_trigger_contingencies(local):
    run, revised = local
    replan = revised["replan"]

    assert "viking_m/europa_league_main_2026/league" in replan["contingencies_triggered"]
    assert "viking_m/champions_league_main_2026/league" in replan["contingencies_retired"]
    assert "brann_m/conference_league_2026/playoff" in replan["contingencies_retired"]
    assert replan["fallbacks_after"] < replan["fallbacks_before"]
    assert not any(
        "viking_m/champions_league_main_2026" in f["trigger"]
        for f in revised["contingency_plan"]["fallbacks"]
    )


def test_the_diff_is_printed_and_reported(work, local):
    run, revised = local
    victim = work[1]
    move = next(m for m in revised["replan"]["moved"] if m["from"] == victim["date"])
    start, end = (date.fromisoformat(move[k]) for k in ("from", "to"))

    assert "Revision of" in run.stdout
    assert f"{start:%a} {start.day} {start:%b} -> {end:%a} {end.day} {end:%b}" in run.stdout
    assert "Changes since the input schedule" in (work[0] / "local" / "2026.html").read_text()


def test_a_second_replan_from_the_output_keeps_the_locked_row(work, local):
    folder, _, locked = work
    again = _generate(
        "--from-schedule", str(folder / "local" / "2026.json"),
        "--freeze-before", "2026-09-01",
        "--outcomes", str(folder / "results.yml"),
        "--out", str(folder / "again"),
    )
    assert again.returncode == 0, again.stderr
    revised = _option(folder / "again" / "2026.json")
    kept = [m for m in revised["matches"] if m.get("locked")]

    assert [_identity(m) for m in kept] == [_identity(locked)]
    assert revised["replan"]["moved"] == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--freeze-before", FREEZE], "--from-schedule"),
        (["--from-schedule", "nowhere.json"], "no such file"),
    ],
)
def test_bad_replan_arguments_fail_before_solving(args, message):
    run = _cli("generate", *args)
    assert run.returncode == 1
    assert message in run.stderr


def test_an_outcome_naming_no_round_is_rejected(work):
    folder, _, _ = work
    (folder / "bad.yml").write_text("brann_m:\n  conference_league_2026: {q9: won}\n")
    run = _cli("generate", "--outcomes", str(folder / "bad.yml"))

    assert run.returncode == 1
    assert "q9" in run.stderr


def test_a_partial_schedule_is_refused(work):
    folder, _, _ = work
    partial = copy.deepcopy(json.loads((folder / "stale.json").read_text()))
    partial["options"][0]["matches"] = partial["options"][0]["matches"][:50]
    (folder / "partial.json").write_text(json.dumps(partial))
    run = _cli("generate", "--from-schedule", str(folder / "partial.json"))

    assert run.returncode == 1
    assert "not a complete season" in run.stderr


def test_a_replan_refuses_to_overwrite_the_schedule_it_revises(work):
    folder, _, _ = work
    before = (folder / "base" / "2026.json").read_text()
    run = _cli("generate", "--from-schedule", str(folder / "base" / "2026.json"), "--out", str(folder / "base"))

    assert run.returncode == 1
    assert "overwrite" in run.stderr and "--out" in run.stderr
    assert (folder / "base" / "2026.json").read_text() == before


BODO_WINS = "bodo_glimt_m:\n  champions_league_2026: {playoff: won}\n"
# A freeze through the middle of a round: Sun 9 Aug is open, Fri 7 Aug is not.
MID_ROUND_FREEZE = "2026-08-09"


def _narrowed(folder: Path, name: str, *extra: str):
    (folder / "bodo.yml").write_text(BODO_WINS)
    run = _generate(
        "--from-schedule", str(folder / "base" / "2026.json"),
        "--freeze-before", MID_ROUND_FREEZE,
        "--outcomes", str(folder / "bodo.yml"),
        "--out", str(folder / name),
        *extra,
    )
    assert run.returncode == 0, run.stderr
    return _option(folder / name / "2026.json")


@pytest.mark.parametrize("solver", ["local", pytest.param("cpsat", marks=pytest.mark.skipif(
    not HAS_ORTOOLS, reason="needs OR-Tools"))])
def test_narrowing_a_cascade_moves_only_matches_of_the_teams_it_touches(work, world, season, solver):
    """Bodø/Glimt reaching the Champions League play-off makes their qualifying
    legs certain and rules out the Europa League branch. Every match that moves
    involves a team whose commitments changed, and every frozen row is intact."""
    from terminliste.frame import build_season_frame
    from terminliste.replan import affected_teams, settle_frame

    folder, _, _ = work
    extra = ("--solver", "cpsat", "--time-budget", "60") if solver == "cpsat" else ()
    revised = _narrowed(folder, f"bodo_{solver}", *extra)
    base = _option(folder / "base" / "2026.json")["matches"]

    freeze = date.fromisoformat(MID_ROUND_FREEZE)
    outcomes = {"bodo_glimt_m": {"champions_league_2026": {"playoff": "won"}}}
    affected = affected_teams(
        settle_frame(build_season_frame(world, season)[0], freeze).commitments,
        settle_frame(build_season_frame(world, season, outcomes)[0], freeze).commitments,
    )
    moved = revised["replan"]["moved"]
    after = _row_texts(revised["matches"])
    frozen = [m for m in base if m["date"] < MID_ROUND_FREEZE]

    assert affected == {"bodo_glimt_m"}
    assert moved, "Bodø's certain play-off should displace at least one match"
    assert all({m["home"], m["away"]} & affected for m in moved)
    assert all(m["from"] >= MID_ROUND_FREEZE and m["to"] >= MID_ROUND_FREEZE for m in moved)
    assert all(after[_identity(m)] == json.dumps(m, sort_keys=True) for m in frozen)
    assert len(frozen) > 150
    assert "bodo_glimt_m/europa_league_main_2026/league" in revised["replan"]["contingencies_retired"]
