# Baselines — real schedules, scored against our rules

A generated schedule scoring 93/100 tells you nothing on its own. It only
means something next to what the *real*, published fixture list scores under
the same rules. This directory is that comparison, kept in-tree so it can be
looked up instead of re-derived, and regenerated with one command so the
number never drifts away from the ruleset that produced it.

```sh
python scripts/refresh_baselines.py           # re-score everything, rewrite reports/
python scripts/refresh_baselines.py --check    # fail if reports/ is stale (CI / pre-commit)
```

Both are offline and deterministic: they read `data/` and `sources/` and
nothing else.

## Layout

```
baselines/
├── sources/       # inputs — the real schedules, committed
│   ├── <id>.csv    # fixtures, in the format terminliste/external_schedule.py reads
│   └── <id>.yml    # provenance sidecar: where it came from, when, verified?
└── reports/       # outputs — generated, committed, never hand-edited
    ├── <id>.json   # machine-readable score, sorted for clean diffs
    └── <id>.md     # the same verdict as prose + tables
```

The CSV is what gets scored; the sidecar is what tells you whether to believe
it. `terminliste/baseline.py` refuses to score a CSV that has no sidecar —
discovery is driven by the `.yml` files, so a stray CSV dropped in here is
ignored rather than silently promoted to a committed baseline. An
unattributed fixture list claiming to be official is the one kind of data
this project most wants to keep out; the `verified: false` markers in
`data/venues.yml` are the same instinct applied to reference data.

## Reading a baseline's score

Two numbers matter and they are not interchangeable.

**Soft score** is the schedule's quality under our preferences, and it is
meaningful for a partial baseline as well as a complete one.

**Hard violations** are not, unless you know the coverage. A partial file
fails every rule keyed to a date it never reaches — not because the real
schedule is broken, but because the data stops early. That is why each
sidecar carries `expected_hard_violations` spelling out which failures are
artefacts, and why `tests/test_baselines.py` asserts that a baseline breaks
*only* rules its sidecar owns up to. An undeclared hard violation is a test
failure, which forces a decision: either the fixture data is wrong, or the
sidecar owes the reader an explanation. Left to prose alone, that paragraph
would rot into something nobody re-reads.

## What's committed today

`eliteserien_toppserien_2026` — round 1 of both leagues (8 Eliteserien
matches on 14–15 March, 6 Toppserien matches on 20–21 March), marked
`coverage: partial` and `verified: false`.

It is round 1 and not the full season for a boring reason: the sandbox this
was assembled in cannot reach a single fixture source. fotball.no,
eliteserien.no, fotmob, worldfootball, transfermarkt, sofascore and Wikipedia
are all refused by the egress proxy with a 403 policy denial, so `WebFetch`
and `curl` are both dead ends and web *search* is the only channel left.
Search returns clean, corroborated pairings for each league's opening round,
but past that the round numbering drifts between queries — one set of eight
fixtures came back labelled "runde 2" in one query and "runde 3" in another —
and the dates stop being quoted at all. A schedule with wrong dates scores
exactly as confidently as one with right dates, so guessing the rest would
have produced a reference baseline worse than no baseline at all.

Extending it is the obvious next job, and needs nothing but a machine that
can reach the data.

## Refreshing, or adding a season

1. **Get the fixtures.** Export or scrape the published list into
   `sources/<id>.csv` with the columns `competition,date,home_team,away_team`
   (plus optional `venue`). Ids must match `data/` — `cli.py validate` lists
   them, and `external_schedule.py` rejects anything unknown by name rather
   than guessing. Put both leagues in one file: the dual-club rules that
   motivate this project only fire when Eliteserien and Toppserien are scored
   together. `SOURCING_FIXTURES.md` in this directory walks through a
   recommended API (TheSportsDB, already used elsewhere in this project),
   a fallback API, and `scripts/fetch_real_schedule.py`, which converts
   either one's JSON into this CSV format and does the team-name matching
   for you.
2. **Write the sidecar.** Copy an existing `<id>.yml`. `id`, `name`,
   `season`, `schedule_file`, `verified`, `retrieved` and `sources` are
   required; `coverage`, `contains`, `expected_hard_violations` and `notes`
   are optional but are what make the report readable a year later. Set
   `verified: true` only if you pulled it from a source you can cite and
   re-fetch.
3. **Re-score.** `python scripts/refresh_baselines.py`, then commit
   `sources/` and `reports/` together.
4. **Check the new hard violations.** If the run reports a rule the sidecar
   does not mention, `tests/test_baselines.py` will fail. Work out which side
   is wrong before silencing it — that failure is the baseline earning its
   keep.

For a new season, also add the season and competition YAML under `data/`
first; a baseline is scored against `data/seasons/<season>.yml`, so the
season has to exist before its fixture list can mean anything.

## When to re-run this

Any change that could move a baseline's number: a new constraint, a
re-weighted old one, a correction to `data/`. Commit the report diff
alongside the change — that diff is the useful artefact, because it says in
points what the edit did to a schedule nobody made up.
