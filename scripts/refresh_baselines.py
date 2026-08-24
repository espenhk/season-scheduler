#!/usr/bin/env python3
"""Re-score every committed baseline schedule and rewrite its report.

    python scripts/refresh_baselines.py            # rewrite baselines/reports/
    python scripts/refresh_baselines.py --check     # fail if they are stale

Run this after changing anything that could move a baseline's number — a new
constraint, a re-weighted old one, a correction to `data/` — and commit the
diff alongside the change. That diff is the useful artefact: it says, in
points, what the ruleset edit did to a schedule nobody made up.

`--check` is the same run with the writes withheld and a non-zero exit if the
output would have differed. It is what a CI job or a pre-commit hook should
call, and it is what `tests/test_baselines.py` asserts.

Offline and deterministic, like `validate` and `score` — it reads `data/` and
`baselines/sources/` and nothing else.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
BASELINES_ROOT = PROJECT_ROOT / "baselines"
SOURCES_DIR = BASELINES_ROOT / "sources"
REPORTS_DIR = BASELINES_ROOT / "reports"

sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.baseline import (  # noqa: E402
    BaselineError,
    discover_baselines,
    evaluate_baseline,
    write_reports,
)
from terminliste.model.loader import DataError, load_world  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any committed report is out of date",
    )
    parser.add_argument("--sources", default=str(SOURCES_DIR))
    parser.add_argument("--reports", default=str(REPORTS_DIR))
    args = parser.parse_args()

    try:
        world = load_world(DATA_ROOT)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        sources = discover_baselines(Path(args.sources))
    except BaselineError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if not sources:
        print(f"No baselines found in {args.sources}.", file=sys.stderr)
        return 1

    reports_dir = Path(args.reports)
    stale: list[str] = []

    for source in sources:
        try:
            evaluation = evaluate_baseline(source, world)
        except (BaselineError, DataError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

        score = evaluation.score
        badge = "feasible" if score.feasible else f"{score.hard_violations} hard violation(s)"
        print(
            f"{source.id}: {len(evaluation.matches)} matches, "
            f"{score.points:.1f}/100, soft {score.soft_total:.1f} ({badge})"
        )
        for warning in evaluation.warnings:
            print(f"  ⚠ {warning}")

        if args.check:
            stale.extend(_stale_reports(evaluation, world, reports_dir))
        else:
            for path in write_reports(evaluation, world, reports_dir):
                print(f"  wrote {_display_path(path)}")

    if args.check:
        if stale:
            print("\nOut-of-date baseline report(s):", file=sys.stderr)
            for name in stale:
                print(f"  - {name}", file=sys.stderr)
            print(
                "\nRun: python scripts/refresh_baselines.py — and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("\nAll baseline reports are up to date.")

    return 0


def _display_path(path: Path) -> Path:
    """`path`, shortened for display if it's under `PROJECT_ROOT`.

    Cosmetic, so an out-of-tree path falls back to the absolute form rather
    than raising — this runs after the report is written, where a crash would
    misreport a successful run as a failure.
    """
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _stale_reports(evaluation, world, reports_dir: Path) -> list[str]:
    """Names of this evaluation's reports whose committed text has drifted.

    Compared as text against a throwaway rewrite rather than by re-deriving
    each field, so `--check` cannot pass while the committed file differs in
    some way the comparison forgot to look at.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fresh = write_reports(evaluation, world, Path(tmp))
        stale = []
        for fresh_path in fresh:
            committed = reports_dir / fresh_path.name
            if not committed.exists():
                stale.append(f"{fresh_path.name} (missing)")
            elif committed.read_text(encoding="utf-8") != fresh_path.read_text(encoding="utf-8"):
                stale.append(fresh_path.name)
        return stale


if __name__ == "__main__":
    raise SystemExit(main())
