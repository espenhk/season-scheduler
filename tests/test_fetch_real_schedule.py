"""`scripts/fetch_real_schedule.py` — team-name matching and JSON-to-CSV conversion.

Fully offline: every test feeds synthetic API JSON through `--from-json` (or
imports the conversion functions directly), so nothing here depends on
`baselines/SOURCING_FIXTURES.md`'s live-fetch path actually working — that
part could not be verified from this sandbox (see the guide for why) and is
exercised here only up to the point where a real HTTP call would happen.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "fetch_real_schedule.py"

sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.model.loader import load_world  # noqa: E402


def _load_script():
    """Import the standalone script as a module — it lives outside any
    package (`scripts/` has no `__init__.py`, matching `publish_web.py` and
    `refresh_baselines.py`), so `importlib` is the straightforward way in."""
    spec = importlib.util.spec_from_file_location("fetch_real_schedule", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules before exec: dataclass field resolution (this
    # module uses `from __future__ import annotations`, so fields are lazily
    # resolved as strings) looks the module up by name there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.fixture(scope="module")
def world():
    return load_world(PROJECT_ROOT / "data")


def test_exact_match_ignores_diacritics_and_club_tokens(script, world):
    team_ids = world.competition("eliteserien_2026").teams
    # "Kristiansund" (bare) should still find kristiansund_m, whose full name
    # is "Kristiansund BK" — the "BK" suffix is exactly what a real API is
    # likely to drop.
    team_id, fuzzy = script.resolve_team(world, team_ids, "Kristiansund")
    assert (team_id, fuzzy) == ("kristiansund_m", False)


def test_exact_match_handles_ascii_spelling_of_diacritics(script, world):
    team_ids = world.competition("eliteserien_2026").teams
    team_id, fuzzy = script.resolve_team(world, team_ids, "FK Bodo/Glimt")
    assert (team_id, fuzzy) == ("bodo_glimt_m", False)


def test_gender_disambiguation_uses_the_competitions_own_roster(script, world):
    """"Brann" must resolve to the men's team when matched against
    Eliteserien's roster and the women's team against Toppserien's — the
    same api_name, disambiguated purely by which competition is being
    converted, never by guessing from the name alone."""
    men = script.resolve_team(world, world.competition("eliteserien_2026").teams, "Brann")
    women = script.resolve_team(world, world.competition("toppserien_2026").teams, "Brann Kvinner")
    assert men == ("brann_m", False)
    assert women == ("brann_w", False)


def test_unmatched_name_is_reported_not_guessed(script, world):
    team_ids = world.competition("eliteserien_2026").teams
    team_id, fuzzy = script.resolve_team(world, team_ids, "Some Unknown FC")
    assert team_id is None


@pytest.mark.parametrize(
    "unrelated_name",
    [
        "Asane",  # normalizes to "asane", which contains "san" — Sandefjord's short_name
        "Sandnes Ulf",  # also contains "san"
    ],
)
def test_short_code_does_not_win_a_fuzzy_match(script, world, unrelated_name):
    """A 3-letter short_name like Sandefjord's "SAN" must never be used as a
    fuzzy substring key — it turns up inside unrelated club names by
    coincidence, and a fuzzy match is supposed to mean "probably the same
    club", not "shares three letters with it"."""
    team_ids = world.competition("eliteserien_2026").teams
    team_id, fuzzy = script.resolve_team(world, team_ids, unrelated_name)
    assert team_id is None


def test_near_miss_is_flagged_fuzzy_not_silently_accepted(script, world):
    """"Aalesund" (common English spelling) vs our "Aalesunds FK" is a real
    near-miss, not a contrived one — this is what motivated defaulting fuzzy
    matches to skipped-and-reported rather than auto-accepted."""
    team_ids = world.competition("toppserien_2026").teams
    team_id, fuzzy = script.resolve_team(world, team_ids, "Aalesund")
    assert (team_id, fuzzy) == ("aalesund_w", True)


def test_convert_writes_only_confident_rows_by_default(script, world):
    fixtures = [
        script.RawFixture(_d("2026-03-14"), "Hamarkameratene", "Viking FK"),
        script.RawFixture(_d("2026-03-15"), "Some Unknown FC", "Viking FK"),
    ]
    rows, problems = script.convert(world, "eliteserien_2026", fixtures, allow_fuzzy=False)
    assert rows == [
        {
            "competition": "eliteserien_2026",
            "date": "2026-03-14",
            "home_team": "hamkam_m",
            "away_team": "viking_m",
            "venue": "",
        }
    ]
    assert len(problems) == 1
    assert "Some Unknown FC" in problems[0]


def test_convert_fuzzy_gate(script, world):
    fixtures = [script.RawFixture(_d("2026-03-21"), "Valerenga Kvinner", "Aalesund")]

    skipped, problems = script.convert(world, "toppserien_2026", fixtures, allow_fuzzy=False)
    assert skipped == []
    assert "aalesund_w (fuzzy)" in problems[0]
    assert "valerenga_w (exact)" in problems[0]

    accepted, _ = script.convert(world, "toppserien_2026", fixtures, allow_fuzzy=True)
    assert accepted == [
        {
            "competition": "toppserien_2026",
            "date": "2026-03-21",
            "home_team": "valerenga_w",
            "away_team": "aalesund_w",
            "venue": "",
        }
    ]


def test_parse_thesportsdb_schema(script):
    payload = {
        "events": [
            {"idEvent": "1", "dateEvent": "2026-03-14", "strHomeTeam": "A", "strAwayTeam": "B", "intRound": "1"},
            {"idEvent": "2", "dateEvent": "not-a-date", "strHomeTeam": "A", "strAwayTeam": "B"},
            {"idEvent": "3", "strHomeTeam": "A", "strAwayTeam": "B"},
        ]
    }
    fixtures = script._parse_thesportsdb(payload)
    assert len(fixtures) == 1
    assert fixtures[0].event_date.isoformat() == "2026-03-14"


def test_parse_api_football_schema(script):
    payload = {
        "response": [
            {
                "fixture": {"date": "2026-03-14T16:00:00+00:00"},
                "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
            },
            {"fixture": {}, "teams": {"home": {"name": "A"}, "away": {"name": "B"}}},
        ]
    }
    fixtures = script._parse_api_football(payload)
    assert len(fixtures) == 1
    assert fixtures[0].event_date.isoformat() == "2026-03-14"


def test_cli_end_to_end_via_from_json(tmp_path):
    payload = {
        "events": [
            {"idEvent": "1", "dateEvent": "2026-03-14", "strHomeTeam": "Hamarkameratene", "strAwayTeam": "Viking FK"},
            {"idEvent": "2", "dateEvent": "2026-03-14", "strHomeTeam": "Molde FK", "strAwayTeam": "Rosenborg BK"},
        ]
    }
    in_path = tmp_path / "raw.json"
    in_path.write_text(json.dumps(payload), encoding="utf-8")
    out_path = tmp_path / "out.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--competition",
            "eliteserien_2026",
            "--from-json",
            str(in_path),
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "competition,date,home_team,away_team,venue"
    assert len(lines) == 3


def test_cli_append_merges_into_existing_file(tmp_path):
    out_path = tmp_path / "combined.csv"
    first = {"events": [{"idEvent": "1", "dateEvent": "2026-03-14", "strHomeTeam": "Viking FK", "strAwayTeam": "Molde FK"}]}
    second = {"events": [{"idEvent": "2", "dateEvent": "2026-03-20", "strHomeTeam": "Bodo/Glimt", "strAwayTeam": "Honefoss BK"}]}
    (tmp_path / "a.json").write_text(json.dumps(first), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(second), encoding="utf-8")

    for competition, name in (("eliteserien_2026", "a.json"), ("toppserien_2026", "b.json")):
        args = [
            sys.executable, str(SCRIPT_PATH),
            "--competition", competition,
            "--from-json", str(tmp_path / name),
            "--out", str(out_path),
        ]
        if competition == "toppserien_2026":
            args.append("--append")
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # header + one row per competition
    assert any("eliteserien_2026" in line for line in lines)
    assert any("toppserien_2026" in line for line in lines)


def test_cli_append_with_incompatible_columns_leaves_original_file_untouched(tmp_path):
    """A file with an extra column (e.g. a hand-added `kickoff`) must fail
    the --append cleanly rather than being truncated to a bare header —
    `csv.DictWriter.writerows` raising mid-write, after the header was
    already flushed to the real destination, is exactly the bug this guards."""
    out_path = tmp_path / "existing.csv"
    original = "competition,date,home_team,away_team,venue,kickoff\neliteserien_2026,2026-03-14,hamkam_m,viking_m,,16:00\n"
    out_path.write_text(original, encoding="utf-8")

    payload = {"events": [{"idEvent": "1", "dateEvent": "2026-03-20", "strHomeTeam": "Bodo/Glimt", "strAwayTeam": "Honefoss BK"}]}
    in_path = tmp_path / "new.json"
    in_path.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--competition", "toppserien_2026",
            "--from-json", str(in_path),
            "--out", str(out_path),
            "--append",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "kickoff" in result.stderr
    assert out_path.read_text(encoding="utf-8") == original
    # No temp file left behind either.
    assert not (tmp_path / ".existing.csv.tmp").exists()


def test_cli_fails_clearly_on_unknown_competition(tmp_path):
    in_path = tmp_path / "raw.json"
    in_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--competition", "not_a_real_competition",
            "--from-json", str(in_path),
            "--out", str(tmp_path / "out.csv"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "unknown competition" in result.stderr


def test_cli_requires_league_id_and_season_without_from_json(tmp_path):
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--competition", "eliteserien_2026",
            "--out", str(tmp_path / "out.csv"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "--from-json" in result.stderr or "required" in result.stderr


def _d(iso: str):
    from datetime import date

    return date.fromisoformat(iso)
