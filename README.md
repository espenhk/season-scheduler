# Terminliste — football league season scheduler

Generates a season fixture list for a set of related football leagues,
scores it against a mix of hard rules and soft preferences, and reports the
result as a browsable HTML page. Built around Eliteserien (men) and
Toppserien (women) 2026 — six clubs field a team in both, which is the
coupling this project exists to handle well.

See `/CLAUDE.md` at the repo root for how this project fits alongside the
others sharing this Poetry environment.

## Quickstart

```bash
poetry install                     # or: pip install pydantic pyyaml jinja2 pytest

cd football-scheduler
python cli.py validate                              # data integrity, no solving
python cli.py generate --season 2026                 # solve, write schedules/2026.html
python cli.py score my_schedule.csv --season 2026     # score a real/proposed schedule
python cli.py refresh-reference-data                  # diff data/*.yml against a live API
python -m pytest ..                                   # -m pytest tests/ from the repo root
```

`generate` defaults to the local-search solver (no extra dependencies).
`--solver cpsat` uses OR-Tools for a near-optimal search — install it with
`poetry install --with football-scheduler-cpsat` (or `pip install ortools`).

`validate`, `generate` and `score` never touch the network — everything
about them is exactly as reproducible as the YAML in `data/`.
`refresh-reference-data` is the one command that does, and even then it only
reads: it prints a diff against `data/*.yml`, it never rewrites it. See
"Reference-data refresh" below.

## What's here

- **`data/`** — the source of truth: venues, clubs/teams, competitions,
  season calendar (blackouts, fixed requirements), and curated travel-time
  overrides. All YAML, hand-editable. `venues.yml`, `clubs.yml` and
  `travel_overrides.yml` are marked `verified: false` because the sandbox
  this was built in couldn't reach Wikipedia or the stats sites — assembled
  from web search snippets plus general knowledge, and a follow-up pass
  couldn't do better (see the header comment in `venues.yml` for exactly
  what was tried and why it stayed unverified rather than risk swapping
  honest-but-approximate numbers for confident-but-wrong ones). The season
  calendar's competition windows (`competitions/*.yml`) and the club/team
  roster were re-checked against web search on 2026-08-16 and are now on
  firmer footing — see those files' own comments. **Check the venues and
  coordinates before using this for anything real**; `cli.py validate` only
  catches referential and geometric problems (unknown ids, coordinates
  outside Norway), not factual ones. `cli.py refresh-reference-data` is a
  real (if unproven, in this sandbox) path to closing this out — see below.
- **`terminliste/model/`** — Pydantic schema + loader (`World`), the season
  calendar (blackout resolution, anchor-date selection), and the travel-time
  model.
- **`terminliste/rounds/`** — the circle-method round-robin generator (pure
  combinatorics, no dates: who plays whom, home or away, is decided here and
  is correct by construction) and `cup_schedule.py`, the cup's own one-shot
  resolve from declared forced dates/windows to actual per-team dates.
- **`terminliste/scoring/`** — the constraint framework. Every rule (hard or
  soft) is an object with an `evaluate()` method; `registry.py` assembles the
  list for a season. Add a rule by writing a class and registering it — both
  solvers and the report pick it up automatically.
- **`terminliste/solvers/`** — two interchangeable backends behind one
  `Scheduler` protocol:
  - `local_search.py` (default): greedy construction + simulated annealing
    over four move operators. No extra dependencies.
  - `cpsat.py` (`--solver cpsat`): OR-Tools CP-SAT, assigning dates to a
    fixed pairing structure. Needs the optional `football-scheduler-cpsat`
    dependency group.
- **`terminliste/report/`** — Jinja2 → one self-contained HTML file. Tabs
  between candidate schedules, a score header, ranked "biggest upsides /
  biggest problems", a full per-rule breakdown table, and a calendar grid
  with dual-club back-to-back home days highlighted.
- **`terminliste/external_schedule.py`** — loads a CSV or JSON schedule from
  anywhere (a real league's published fixtures, a hand-drafted proposal) and
  scores it against the same rules, so `cli.py score` can point out exactly
  what's wrong with a schedule the tool didn't generate.
- **`terminliste/refdata/`** — an optional, explicit-opt-in fetch/cache
  layer for team and venue reference data. See "Reference-data refresh"
  below.

## Data model

```
Venue     id, name, city, lat/lon, capacity, surface
Team      id, gender, level (senior|second|youth), home_venue, club_id
Club      id, name, teams[]                    # 1+ teams; "dual clubs" have 2+ senior teams
Competition  id, season, gender, format (league|cup), teams[], start/end (optional),
             preferred_weekday, weights{}
             cup_rounds[]                       # cup only: id, name, note, and either a
                                                 # forced_date or a window_start/window_end
Season    id, year, window, competitions[], cup_competitions[], global_blackouts[],
          venue_blackouts[], fixed_requirements[]
Fixture   an unscheduled pairing (home, away, leg, round)         # league only
Match     a Fixture placed on a date at a venue                    # league only
CupSchedule  a cup's rounds resolved to dates (see below)
```

A `Competition`'s `start`/`end` are optional and default to the season's own
window — the common case, when every competition in a season shares one
calendar. 2026 is the exception the fields exist for: Eliteserien and
Toppserien don't actually run the same length of season (Toppserien
finishes almost a month before Eliteserien), so `toppserien_2026.yml` sets
its own, narrower window rather than letting the solver believe it has a
month it doesn't. `Season.start`/`.end` become the outer envelope across
every competition sharing that calendar — global blackouts, discouraged
dates and venue blackouts still apply season-wide.

`Team.level` is wider than today's use needs on purpose — a reserve side is a
data edit (`level: second`), not a rewrite.

A **league** (`format: league`) is generated and dated by the solver — the
round-robin generator decides who plays whom, the solver decides when. A
**cup** (`format: cup`) is schedulable too, just at a much coarser grain: each
`CupRound` is either a `forced_date` (already announced, cannot move) or a
`window_start`/`window_end` it may fall inside — a week, month or quarter,
per `granularity` — since the real Norwegian Cup often isn't more precise
than that for rounds more than a few weeks out. `terminliste/rounds/
cup_schedule.py` resolves a cup's rounds to a `CupSchedule` (an actual date
per entered team) in one pass, honouring round order as a hard rule — round
N is fully placed before round N+1 can start — and keeping each round's
placements inside one calendar week as far as the data allows, degrading to
a warning rather than a failure when it can't. See
`data/competitions/cup_men_2027.yml` and `cup_women_2027.yml` for the
2026-27 Norwegian Cup.

Cup pairings are still drawn round by round and never modelled as fixtures
— nobody knows who plays whom yet — so a `CupSchedule` records only *when*
each entered team's round falls, on the base assumption that every team is
still alive through the final. A season lists which competitions of each
kind it holds under `competitions` (leagues) and `cup_competitions` (cups)
respectively; the two are kept separate because only the former is fed to
the round-robin/solver pipeline — a cup is resolved once, up front, and the
league solver treats the result as fixed input. `CupRoundConflict` then keeps league fixtures a `min_rest_days`-wide window
clear of each team's own resolved cup dates; CP-SAT additionally excludes a
conflicting date from a fixture's candidate set outright, rather than
modelling it as a constraint to satisfy after the fact, since its fixtures
each have a small, fixed candidate window around a round anchor chosen
without cup awareness and would otherwise have no way to route around a
conflict discovered too late.

## Constraints implemented

**Hard** (`terminliste/scoring/hard.py`) — a schedule with any of these
present is not feasible, full stop:
`min_rest_days`, `blackout_dates`, `venue_double_booking`, `club_home_clash`,
`leg_ordering`, `fixed_requirement`, `one_match_per_team_per_day`,
`cup_round_conflict`.

**Soft** (`terminliste/scoring/soft.py`) — scored, signed (reward positive,
penalty negative), and shown ranked in the report:
`preferred_weekday`, `consecutive_home_days`, `consecutive_away_days` (scaled
by travel time, capped at an 8h default), `home_away_breaks`,
`home_away_balance`, `rest_comfort`, `soft_venue_preference`.

Run `python cli.py score <file> --season 2026` on any schedule to see every
rule's contribution, with named examples for anything that fired.

## Reference-data refresh

`python cli.py refresh-reference-data [--season 2026] [--force]` fetches
each competition's teams from [TheSportsDB](https://www.thesportsdb.com)'s
free API (`terminliste/refdata/client.py`), caches the result for a day
(`terminliste/refdata/cache.py`), and diffs the fetched stadium name/capacity
against what's in `data/clubs.yml`/`data/venues.yml` for each matched team
(`terminliste/refdata/refresh.py`). It never writes to `data/*.yml` — a
capacity or a coordinate feeding into the scheduler is worth a human glance
before it changes, so this prints a diff and stops; folding an accepted
change in is still a hand edit, the same as any other data correction.

It fails soft, on purpose: an unreachable API (or, inside this project's own
sandboxed dev environment, an egress policy that blocks the API host
outright) falls back to the last cache, or to reporting "no data to
compare" — never a crash, and never worse than not running it at all. Try
it yourself and you'll likely see exactly that fallback fire, which is the
point:

```
$ python cli.py refresh-reference-data
=== eliteserien_2026 (Norwegian Eliteserien) — source: unavailable ===
  note: could not reach https://www.thesportsdb.com/api/v1/json/3: ...
  no data to compare — using whatever is already in data/*.yml
```

The `API_LEAGUE_NAMES` mapping in `cli.py` (TheSportsDB's league-name string
per competition) is a documented best guess, not something this environment
could confirm against a live call — verify it the first time this runs
somewhere with real network access.

## Testing

```bash
python -m pytest tests/ -v
```

- `test_round_robin.py` — exhaustive n=2..20 checks on the pairing generator,
  plus the triple-round-robin (`rounds_per_pairing=3`) checks backing
  Toppserien: every pairing meets three times with a 2-1 split, and every
  team's season-total home/away count is within 1 of even.
- `test_hard_constraints.py` / `test_soft_constraints.py` — each rule at its
  boundary, hand-built 4-team schedules, plus a check that every hard rule is
  silent on a clean schedule.
- `test_calendar.py`, `test_travel.py`, `test_loader.py` — the supporting
  model layer, including curated overrides, referential-integrity errors,
  and a competition's own (optionally narrower) start/end window.
- `test_cup_schedule.py` — resolving a cup's forced dates/windows to real
  per-team dates: round ordering enforced and rejected when infeasible,
  teams spread within a round's window, and the blackout-fallback warning.
- `test_refdata.py` — the reference-data client, cache and diff logic behind
  `cli.py refresh-reference-data`, entirely mocked at the HTTP layer.
- `test_external_schedule.py` — CSV/JSON parsing, leg inference, coverage
  warnings, and a schedule with deliberate flaws scoring the right hard
  violations.
- `test_solver_contract.py` — parametrized over both backends (`cpsat`
  skipped if OR-Tools isn't installed): diverse top-N candidates, every
  fixture placed once, feasibility achievable, determinism, and a graceful
  report on an unsolvable calendar.
- `test_integration_coupled_leagues.py` — the actual point of this project:
  on the real 2026 data, scheduling the two leagues together produces
  strictly more back-to-back home days for dual clubs than scheduling them
  independently would.

## Suggested next steps

See the write-up delivered alongside this project for the full discussion.
The Norwegian Cup is now in (`cup_men_2027.yml` / `cup_women_2027.yml`,
`terminliste/rounds/cup_schedule.py`, `CupRoundConflict`) — only the
top-flight clubs' rounds are tracked, and only Round 1 (men), Round 2
(women), and the "final in spring 2027, end of May" framing are
NFF-confirmed; every other round is an estimated window, flagged per round
via `note`, pending NFF publishing the real ones (see each file's header).
Natural next steps from here: European qualifiers for the tournaments this
project doesn't control the scheduling of (issue #31), and cascading
Champions/Europa/Conference League qualifier progression (issue #29).

Also worth doing, now that `refresh-reference-data` exists but has never run
against the real API:
- Run it somewhere with real network access, confirm the `API_LEAGUE_NAMES`
  strings and the field names `client.py` expects (`strTeam`,
  `strStadium`, `intStadiumCapacity`) against TheSportsDB's actual current
  responses, and fold in whatever diffs it turns up.
- Extend the diff beyond stadium name/capacity to coordinates once a
  provider that reliably has them is confirmed reachable — TheSportsDB's
  team records don't carry lat/lon, so `venues.yml`'s coordinates are still
  unverified even once the rest of this closes out.
