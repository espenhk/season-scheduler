"""The committed baselines load, score, and match their committed reports.

Two jobs: a data guard, so a CSV with stale team ids or a sidecar without
provenance fails here rather than at the next refresh; and a staleness check,
since a re-weighted constraint with `baselines/reports/` not regenerated
leaves the committed number quietly lying about the current ruleset.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import terminliste.baseline as baseline_module
import yaml
from terminliste.baseline import (
    BaselineError,
    discover_baselines,
    evaluate_baseline,
    load_baseline,
)
from terminliste.model.loader import load_world
from terminliste.rounds.cup_schedule import CupSchedulingError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_ROOT / "baselines" / "sources"
REPORTS_DIR = PROJECT_ROOT / "baselines" / "reports"


@pytest.fixture(scope="module")
def world():
    return load_world(PROJECT_ROOT / "data")


@pytest.fixture(scope="module")
def sources():
    return discover_baselines(SOURCES_DIR)


def test_at_least_one_baseline_is_committed(sources):
    assert sources, "baselines/sources/ should hold at least one real schedule"


def test_every_baseline_scores(world, sources):
    for source in sources:
        evaluation = evaluate_baseline(source, world)
        assert evaluation.matches, f"{source.id} produced no matches"
        assert evaluation.score.num_matches == len(evaluation.matches)


def test_every_baseline_has_a_committed_report(sources):
    for source in sources:
        assert (REPORTS_DIR / f"{source.id}.json").exists()
        assert (REPORTS_DIR / f"{source.id}.md").exists()


def test_reports_are_up_to_date():
    """`--check` is the contract; run it rather than re-deriving it here."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "refresh_baselines.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "baselines/reports/ is out of date — run "
        f"`python scripts/refresh_baselines.py`.\n{result.stdout}\n{result.stderr}"
    )


def test_writes_to_an_out_of_tree_reports_dir_do_not_crash_on_the_display_path(tmp_path):
    """`Path.relative_to` raises outside `PROJECT_ROOT`, but the report must
    still be written — only the "wrote ..." line falls back to an absolute
    path."""
    result = subprocess.run(
        [
            sys.executable, str(PROJECT_ROOT / "scripts" / "refresh_baselines.py"),
            "--reports", str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "eliteserien_toppserien_2026.json").exists()
    assert (tmp_path / "out" / "eliteserien_toppserien_2026.md").exists()


_DECLARED_COUNT_RE = re.compile(r"^(\w+)\s*\((\d+)\)")


def _declared_hard_violation_counts(expected: list[str]) -> dict[str, int]:
    """Parse `"<constraint_id> (<count>): ..."` sidecar entries into a max
    count per rule. An entry that doesn't start with that shape contributes
    nothing — see the test below for why an unparsed entry must not count as
    "any number of violations is fine"."""
    counts: dict[str, int] = {}
    for entry in expected:
        match = _DECLARED_COUNT_RE.match(entry)
        if match:
            counts[match.group(1)] = int(match.group(2))
    return counts


def _undeclared_or_exceeded(
    actual: dict[str, int], declared: dict[str, int]
) -> tuple[list[str], dict[str, tuple[int, int]]]:
    """Compare actual hard-violation counts against a sidecar's declared
    ones. Returns (rule ids not declared at all, {rule id: (actual, declared)}
    for any rule that fired more often than declared) — both empty means the
    sidecar's honest about everything the baseline currently breaks."""
    undeclared = sorted(set(actual) - set(declared))
    exceeded = {
        constraint_id: (actual[constraint_id], declared[constraint_id])
        for constraint_id in actual
        if constraint_id in declared and actual[constraint_id] > declared[constraint_id]
    }
    return undeclared, exceeded


def test_every_hard_violation_is_declared_in_the_sidecar(world, sources):
    """A baseline may only break hard rules its sidecar owns up to, and only
    as often as it declares.

    Tied to the score, `expected_hard_violations` can't rot into a stale
    paragraph explaining away violations nobody re-read. The count matters as
    much as the rule id: a 7th `fixed_requirement` break against a declared 6
    is a real finding, not the same known gap.
    """
    for source in sources:
        evaluation = evaluate_baseline(source, world)
        actual = {r.constraint_id: r.count for r in evaluation.score.hard_results() if r.count}
        declared = _declared_hard_violation_counts(source.expected_hard_violations)

        undeclared, exceeded = _undeclared_or_exceeded(actual, declared)
        assert not undeclared, (
            f"{source.id} breaks {undeclared}, which its sidecar does not declare as "
            '"<rule> (<count>): ...". Either the fixture data is wrong or '
            "expected_hard_violations needs updating."
        )
        assert not exceeded, (
            f"{source.id} breaks these rules more times than its sidecar declares, as "
            f"(actual, declared): {exceeded}. A new instance of an already-declared rule "
            "is still a real finding about the fixture data — update "
            "expected_hard_violations or investigate."
        )


def test_declared_count_parsing_reads_the_convention_sidecars_use():
    entries = [
        "full_round_on_date (16): every club missing a match on 2026-05-16.",
        "fixed_requirement (6): the named 16 May home fixtures.",
        "this entry has no parseable count and should be ignored",
    ]
    assert _declared_hard_violation_counts(entries) == {
        "full_round_on_date": 16,
        "fixed_requirement": 6,
    }


def test_undeclared_or_exceeded_catches_a_new_instance_of_a_declared_rule():
    """A 7th break of a rule declared to break 6 times must not pass silently
    just because the rule id itself was declared."""
    undeclared, exceeded = _undeclared_or_exceeded(
        actual={"fixed_requirement": 7, "full_round_on_date": 16},
        declared={"fixed_requirement": 6, "full_round_on_date": 16},
    )
    assert undeclared == []
    assert exceeded == {"fixed_requirement": (7, 6)}


def test_undeclared_or_exceeded_flags_a_wholly_new_rule():
    undeclared, exceeded = _undeclared_or_exceeded(
        actual={"min_rest_days": 1}, declared={"fixed_requirement": 6}
    )
    assert undeclared == ["min_rest_days"]
    assert exceeded == {}


def test_undeclared_or_exceeded_is_silent_when_actual_is_within_declared():
    """Fewer violations than declared is not a failure — an improvement in
    the fixture data (or the ruleset) shouldn't force a sidecar edit."""
    undeclared, exceeded = _undeclared_or_exceeded(
        actual={"fixed_requirement": 4}, declared={"fixed_requirement": 6}
    )
    assert undeclared == []
    assert exceeded == {}


def test_sidecar_is_required(tmp_path):
    (tmp_path / "orphan.csv").write_text("competition,date,home_team,away_team\n", encoding="utf-8")
    assert discover_baselines(tmp_path) == []


def test_missing_required_field_is_rejected(tmp_path):
    sidecar = tmp_path / "bad.yml"
    sidecar.write_text(yaml.safe_dump({"id": "bad", "name": "Bad"}), encoding="utf-8")
    with pytest.raises(BaselineError, match="missing required field"):
        load_baseline(sidecar)


def test_cup_scheduling_failure_is_wrapped_as_baseline_error(world, sources, monkeypatch):
    """`evaluate_baseline` presents one error type to its callers, and
    `refresh_baselines.py` only catches `(BaselineError, DataError)` — an
    unwrapped `CupSchedulingError` would escape as a raw traceback instead of
    the clean FAIL every other bad-data path produces."""

    def _boom(*args, **kwargs):
        raise CupSchedulingError("no room for round 1")

    monkeypatch.setattr(baseline_module, "schedule_cups", _boom)
    with pytest.raises(BaselineError, match="cup scheduling failed"):
        evaluate_baseline(sources[0], world)


def test_missing_schedule_file_is_rejected(tmp_path):
    sidecar = tmp_path / "bad.yml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "id": "bad",
                "name": "Bad",
                "season": "2026",
                "schedule_file": "nope.csv",
                "verified": False,
                "retrieved": "2026-08-16",
                "sources": ["somewhere"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BaselineError, match="does not exist"):
        load_baseline(sidecar)


def _base_sidecar_fields(**overrides) -> dict:
    fields = {
        "id": "bad",
        "name": "Bad",
        "season": "2026",
        "schedule_file": "nope.csv",
        "verified": False,
        "retrieved": "2026-08-16",
        "sources": ["somewhere"],
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("bad_verified", ["no", "false", 0, 1])
def test_verified_must_be_a_real_bool_not_a_coerced_scalar(tmp_path, bad_verified):
    """`bool("no")` is `True`, so coercing would flip a quoted "no" through to
    "trusted" — the one thing this field exists to prevent."""
    sidecar = tmp_path / "bad.yml"
    sidecar.write_text(
        yaml.safe_dump(_base_sidecar_fields(verified=bad_verified)), encoding="utf-8"
    )
    with pytest.raises(BaselineError, match="verified"):
        load_baseline(sidecar)


@pytest.mark.parametrize("list_field", ["sources", "expected_hard_violations", "competitions"])
def test_scalar_string_in_a_list_field_is_rejected(tmp_path, list_field):
    """A sentence where a YAML list was expected must be rejected outright,
    not iterated into one entry per character."""
    sidecar = tmp_path / "bad.yml"
    sidecar.write_text(
        yaml.safe_dump(_base_sidecar_fields(**{list_field: "a plain sentence, not a list"})),
        encoding="utf-8",
    )
    with pytest.raises(BaselineError, match=f"{list_field!r} must be a list"):
        load_baseline(sidecar)
