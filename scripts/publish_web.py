#!/usr/bin/env python3
"""Build the season schedule report into site/ for the GitHub Pages deploy.

    python scripts/publish_web.py [--season 2026] [--solver local]

Runs the same solve-and-render path as `cli.py generate`, then renames the
resulting self-contained HTML report to site/index.html. site/ is gitignored;
the publish-pages workflow uploads it as the Pages artifact.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = PROJECT_ROOT / "site"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="2026")
    parser.add_argument("--solver", choices=["local", "cpsat"], default="local")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--time-budget", type=float, default=60.0, help="seconds")
    args = parser.parse_args()

    SITE_DIR.mkdir(parents=True, exist_ok=True)
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
            str(SITE_DIR),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )

    generated_html = SITE_DIR / f"{args.season}.html"
    index_html = SITE_DIR / "index.html"
    generated_html.replace(index_html)
    print(f"\nPublished {index_html} (kept {SITE_DIR / f'{args.season}.json'} alongside it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
