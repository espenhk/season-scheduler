#!/usr/bin/env python3
"""Generate the season schedule report and publish it as this project's mobile POC.

    python football-scheduler/scripts/publish_web.py [--season 2026] [--solver local]

Runs the same solve-and-render path as `cli.py generate`, then copies the
resulting self-contained HTML report to docs/football-scheduler/index.html —
the path GitHub Pages serves (see docs/README.md for the convention).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DOCS_DIR = REPO_ROOT / "docs" / "football-scheduler"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="2026")
    parser.add_argument("--solver", choices=["local", "cpsat"], default="local")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--time-budget", type=float, default=60.0, help="seconds")
    args = parser.parse_args()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "cli.py",
            "generate",
            "--season",
            args.season,
            "--solver",
            args.solver,
            "--seed",
            str(args.seed),
            "--time-budget",
            str(args.time_budget),
            "--out",
            str(DOCS_DIR),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )

    generated_html = DOCS_DIR / f"{args.season}.html"
    index_html = DOCS_DIR / "index.html"
    generated_html.replace(index_html)
    print(f"\nPublished {index_html} (kept {DOCS_DIR / f'{args.season}.json'} alongside it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
