"""Re-planning mid-season: frozen rounds stay byte-identical, known results
narrow the UEFA cascade, and only the teams concerned move (#13)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from terminliste.model.schema import fixture_identity

ROOT = Path(__file__).resolve().parent.parent.parent
FREEZE = "2026-08-15"


def _generate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "generate", "--top-n", "1", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    work = tmp_path_factory.mktemp("replan")
    base = _generate("--time-budget", "30", "--out", str(work / "base"))
    assert base.returncode == 0, base.stderr
    outcomes = work / "outcomes.yml"
    outcomes.write_text("viking_m:\n  champions_league_2026:\n    playoff: lost\n")
    revised = _generate(
        "--time-budget",
        "20",
        "--from-schedule",
        str(work / "base" / "2026.json"),
        "--freeze-before",
        FREEZE,
        "--outcomes",
        str(outcomes),
        "--out",
        str(work / "revised"),
    )
    assert revised.returncode == 0, revised.stderr
    load = lambda name: json.loads((work / name / "2026.json").read_text())["options"][0]  # noqa: E731
    return load("base"), load("revised"), revised.stdout


def _identity(row: dict) -> tuple:
    low, high = sorted((row["home"], row["away"]))
    return (row["competition"], row["leg"], low, high)


def test_frozen_matches_are_byte_identical(runs):
    base, revised, _ = runs
    after = {_identity(m): m for m in revised["matches"]}
    frozen = [m for m in base["matches"] if m["date"] < FREEZE]

    assert len(frozen) > 100
    assert all(after[_identity(m)] == m for m in frozen)
    assert len(revised["matches"]) == len(base["matches"])


def test_only_rounds_after_the_freeze_change(runs):
    base, revised, _ = runs
    moved = revised["replan"]["moved"]

    assert revised["replan"]["frozen"] == sum(m["date"] < FREEZE for m in base["matches"])
    assert all(m["from"] >= FREEZE and m["to"] >= FREEZE for m in moved)


def test_narrowing_a_cascade_moves_only_the_affected_teams(runs):
    base, revised, _ = runs
    moved = revised["replan"]["moved"]

    assert moved, "Viking dropping into the Europa League should displace a match"
    assert all("viking_m" in (m["home"], m["away"]) for m in moved)
    assert len(moved) <= 3


def test_contingencies_are_triggered_and_retired(runs):
    _, revised, _ = runs
    triggered = {c["node"] for c in revised["replan"]["triggered"]}
    retired = {c["node"] for c in revised["replan"]["retired"]}

    assert "viking_m/europa_league_main_2026/league" in triggered
    assert "viking_m/champions_league_main_2026/league" in retired
    assert not any(
        "viking_m/champions_league_main_2026" in f["triggers"]
        for f in revised["contingency_plan"]["fallbacks"]
    )


def test_cli_prints_the_diff(runs):
    assert "Revision of" in runs[2]
    assert "retired viking_m/champions_league_main_2026/league" in runs[2]


def test_revised_schedule_is_feasible(runs):
    assert runs[1]["hard_violations"] == 0


def test_freeze_without_a_schedule_is_refused():
    result = _generate("--freeze-before", FREEZE, "--time-budget", "1")
    assert result.returncode == 1
    assert "--from-schedule" in result.stderr


def test_frozen_rows_round_trip_through_the_loader(runs, world, tmp_path):
    from terminliste.external_schedule import load_schedule

    base, *_ = runs
    path = tmp_path / "in.json"
    rows = [dict(m, locked=m["date"] >= "2026-12-01") for m in base["matches"]]
    path.write_text(json.dumps({"options": [{"label": "Option 1", "matches": rows}]}))
    loaded = load_schedule(path, world)

    assert loaded.has_rounds
    assert len(loaded.locked) == sum(m["date"] >= "2026-12-01" for m in base["matches"])
    assert {fixture_identity(m) for m in loaded.matches if m.date >= date(2026, 12, 1)}
