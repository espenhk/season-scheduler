# CLAUDE.md

`season-scheduler` (package name `terminliste`) generates a season fixture
list for a set of related football leagues, scores it against hard rules and
soft preferences, and reports the result as a browsable HTML page. Built
around the Norwegian Eliteserien (men, 16 clubs) and Toppserien (women, 12
clubs) for 2026 — six clubs field a team in both, and scheduling the two
leagues together so those pairs get back-to-back home weekends (rather than
clashing, or scheduled independently) is the reason this project exists
rather than a generic single-league scheduler.

How this repo fits with the other espenhk repos (Pages, portfolio, skills): [ARCHITECTURE.md](https://github.com/espenhk/espenhk.github.io/blob/main/ARCHITECTURE.md).

This project was split out of `espenhk/espenhk-hobby-projects` (issue #130
there), with its git history kept. `(#N)` references in code comments and
commits from before the split point at espenhk-hobby-projects issues and PRs,
not this repo's. Refer to old-repo issues from new code as
`espenhk/espenhk-hobby-projects#N`.

---

## Structure

```sh
season-scheduler/
├── data/                   # source of truth: venues, clubs/teams, competitions,
│                            # season calendar, curated travel-time overrides — all YAML
├── baselines/              # real published fixture lists (sources/) + their committed
│                            # score reports (reports/) — see baselines/README.md
├── cli.py                  # validate / generate / score / explain
├── terminliste/
│   ├── model/               # Pydantic schema + loader (World), calendar, travel model
│   ├── rounds/               # circle-method round-robin pairing generator
│   ├── scoring/               # constraint framework: hard.py, soft.py, registry.py
│   ├── solvers/                # local-search (default) and CP-SAT (--solver cpsat) backends
│   ├── report/                  # Jinja2 -> self-contained HTML season report
│   ├── external_schedule.py      # score a real/proposed CSV or JSON schedule
│   └── baseline.py                # score the committed baselines, write their reports
├── scripts/
│   ├── publish_web.py       # build the report into site/ for the Pages deploy
│   ├── refresh_baselines.py  # re-score baselines/sources/ -> baselines/reports/
│   └── fetch_real_schedule.py # convert a real fixture-API JSON into a baseline CSV
├── schedules/                # generated HTML + JSON output (gitignored)
├── site/                     # Pages build output (gitignored, built in CI only)
└── tests/                    # mirrors terminliste/'s structure — model/, rounds/,
                               # scoring/, solvers/, report/, refdata/, scripts/,
                               # integration/ — see tests/README.md for which
                               # folders to run for a given change
```

## State

Complete and functional first version. `validate`, `generate` (local solver),
and `score` all run fully offline with no external dependencies beyond
`pydantic`/`pyyaml`/`jinja2`. `--solver cpsat` needs the optional
`football-scheduler-cpsat` Poetry group (OR-Tools).
`scripts/refresh_baselines.py` re-scores the real fixture lists committed
under `baselines/` and rewrites their reports; run it (and commit the diff)
after any change that could move a schedule's score, and note that its
`--check` mode is asserted by the test suite. The committed 2026 baseline is
currently round 1 only — `baselines/SOURCING_FIXTURES.md` has the recommended
API and `scripts/fetch_real_schedule.py` usage for extending it to a full
season from a machine with real network access (the cloud sandbox's egress
policy blocks every fixture source, so that path is written but unverified).
Club/venue data in `data/` is marked `verified: false` — assembled from web
search rather than a fetchable source, so check it before relying on it for
anything real. See `README.md` for the full architecture writeup, the
constraint list, and suggested next steps.

## Published report

`.github/workflows/publish-pages.yml` runs `scripts/publish_web.py` on every
push to `main` and deploys `site/` with GitHub Pages (source: GitHub Actions)
to <https://espenhk.github.io/season-scheduler/>. Nothing generated is ever
committed.

## Running

```bash
poetry install --with football-scheduler-test   # add football-scheduler-cpsat for OR-Tools
poetry run python cli.py validate
poetry run python -m pytest tests/
poetry run python scripts/refresh_baselines.py --check
```

CI runs only the test folders a PR touches — the path -> suites mapping is in
`tests/README.md` and the `fs-tests-*.yml` workflows.

---

## Code comments

Comments should be brief and explain *why*, not restate *what* the code
already makes clear through legible naming and structure. A single line is
usually enough to orient a reader; multiline comments are the exception, not
the rule — reserve those for something genuinely intricate that a line
can't carry. Comments should describe the code as it stands now, not the
history of how it got there — don't reference a previous implementation, a
past bug, or a since-changed approach. If it's not needed to understand or
improve the current code, leave it out.

If a *why* traces back to a GitHub issue, note the issue number in
parentheses at the end of the relevant sentence, e.g. `(#42)`, rather than
narrating the issue inline ("issue #42 needed..."). This keeps the comment
itself terse while still giving a reader a thread to pull if they want the
full history.

---

## Git & PR conventions

When a pull request's changes fix one or more GitHub issues, the PR
description must list each one on its own line as `closes #<number>` (or
`fixes #<number>`), e.g.:

```
closes #18
closes #19
closes #21
```

This is what lets GitHub auto-close those issues when the PR merges — do
this whether the PR was opened by a person or by an agent, and whether it
fixes one issue or several.

All PRs are merged using squash-merge to keep the main branch history clean
and linear.
