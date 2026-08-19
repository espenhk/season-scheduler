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
python cli.py refresh-travel-times                    # fetch real ground travel times
python scripts/refresh_baselines.py                   # re-score the committed real-schedule baselines
python -m pytest ..                                   # -m pytest tests/ from the repo root
```

`generate` defaults to the local-search solver (no extra dependencies).
`--solver cpsat` uses OR-Tools for a near-optimal search — install it with
`poetry install --with football-scheduler-cpsat` (or `pip install ortools`).

`validate`, `generate` and `score` never touch the network — everything
about them is exactly as reproducible as the YAML in `data/`.
`refresh-reference-data` and `refresh-travel-times` are the two commands that
do, and both only read: `refresh-reference-data` prints a diff against
`data/*.yml` and never rewrites it; `refresh-travel-times` writes to the
gitignored `data/.refdata_cache/`, never to `data/*.yml`. See
"Reference-data refresh" and "Travel time" below.

## What's here

- **`data/`** — the source of truth: venues, clubs/teams, competitions,
  season calendar (blackouts, fixed requirements), and a small file of
  curated travel-time overrides for the rare pair a routing API gets wrong.
  All YAML, hand-editable. `venues.yml`, `clubs.yml` and
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
  is correct by construction); `cup_schedule.py`, the cup's own one-shot
  resolve from declared forced dates/windows to actual per-team dates; and
  `european_schedule.py`, the UEFA qualifying cascade's equivalent — see
  "European qualifying cascade" below.
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
- **`terminliste/baseline.py`** + **`baselines/`** — the durable version of
  that: the real published fixture lists, committed with their provenance,
  re-scored on demand so there is always a real-world number to read a
  generated schedule against. See "Baselines" below and
  `baselines/README.md`.
- **`scripts/fetch_real_schedule.py`** — converts a fixture-API response
  (TheSportsDB or API-Football) into a baseline-ready CSV, matching the
  API's team names against `data/clubs.yml`. See
  `baselines/SOURCING_FIXTURES.md`.
- **`terminliste/refdata/`** — an optional, explicit-opt-in fetch/cache
  layer for team and venue reference data. See "Reference-data refresh"
  below.

## Data model

```
Venue     id, name, city, lat/lon, capacity, surface
Team      id, gender, level (senior|second|youth), home_venue, club_id
Club      id, name, teams[]                    # 1+ teams; "dual clubs" have 2+ senior teams
Competition  id, season, gender, format (league|cup|european), movable, teams[],
             start/end (optional), preferred_weekday, weights{}
             cup_rounds[]                       # cup only: id, name, note, and either a
                                                 # forced_date or a window_start/window_end
             european_rounds[]                  # european only: id, name, note,
                                                 # first_leg/second_leg (each its own
                                                 # forced_date/window), ties[],
                                                 # drop_to_competition/_round
Season    id, year, window, competitions[], cup_competitions[], european_competitions[],
          global_blackouts[], venue_blackouts[], global_blackout_ranges[],
          venue_blackout_ranges[], fixed_requirements[]
Fixture   an unscheduled pairing (home, away, leg, round)         # league only
Match     a Fixture placed on a date at a venue                    # league only
CupSchedule  a cup's rounds resolved to dates (see below)
EuropeanCommitmentDate  one resolved leg date a team is committed to,
             from the reachable part of its UEFA qualifying cascade (see below)
```

`Competition.movable` (issue #31) is explicit rather than inferred from
`format`, even though in practice every non-`league` competition sets it
`False`: a league's fixtures are the solver's to place, a cup's or European
competition's are UEFA's or NFF's, and the solver must schedule its own
fixtures around them, never propose moving them.

A `Competition`'s `start`/`end` are optional and default to the season's own
window — the common case, when every competition in a season shares one
calendar. 2026 is the exception the fields exist for: Eliteserien and
Toppserien don't actually run the same length of season (Toppserien
finishes almost a month before Eliteserien), so `toppserien_2026.yml` sets
its own, narrower window rather than letting the solver believe it has a
month it doesn't. `Season.start`/`.end` become the outer envelope across
every competition sharing that calendar — global blackouts, discouraged
dates, venue blackouts and both range flavours below still apply
season-wide (global) or at the one named venue (venue-scoped).

`global_blackout_ranges` and `venue_blackout_ranges` (issue #33) are
labelled, multi-day generalisations of `global_blackouts` and
`venue_blackouts` respectively: a `start`/`end` span — a holiday period, a
FIFA international break, a multi-day concert teardown — with a `reason`
for the overview, rather than one entry per date. `Season.blacked_out_dates`
and `Season.venue_blacked_out_dates` (both cached, since `BlackoutDates`
evaluates inside the annealer's inner loop) expand every range day by day
and fold the result into the same date→reason mappings the single-date
fields feed, so a range is honoured everywhere its single-day counterpart
already is. The two stay strictly separate the way `global_blackouts` and
`venue_blackouts` already do: a global entry reaches every consumer — the
candidate calendar (`build_calendar`), the `BlackoutDates` hard constraint,
and cup/European round resolution — while a venue-scoped one only reaches
the venue-aware ones (`build_calendar`'s per-venue blocks, `BlackoutDates`'s
venue check); cups and European qualifiers don't book venues, so
`venue_blackout_ranges` never reaches `schedule_cups` or
`resolve_european_commitments`. Both range models share an `end < start`
schema guard. The season report's "Season exclusions" section lists all
four sources together, sorted by start date.

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
still alive through the final. Home/away is resolved per round too, since
who the opponent is doesn't change which side of the tie an entered team is
on: away for each team's first three rounds (an Eliteserien/Toppserien side
is assumed to draw and travel to a lower-division opponent), then
alternating from the fourth round on, except the final, always at neutral
ground (Ullevål stadion) — see `_venue_type` in `cup_schedule.py`. A season lists which competitions of each
kind it holds under `competitions` (leagues) and `cup_competitions` (cups)
respectively; the two are kept separate because only the former is fed to
the round-robin/solver pipeline — a cup is resolved once, up front, and the
league solver treats the result as fixed input. `CupRoundConflict` then keeps league fixtures at least `min_rest_days` full
rest days clear of each team's own resolved cup dates; CP-SAT additionally excludes a
conflicting date from a fixture's candidate set outright, rather than
modelling it as a constraint to satisfy after the fact, since its fixtures
each have a small, fixed candidate window around a round anchor chosen
without cup awareness and would otherwise have no way to route around a
conflict discovered too late.

### European qualifying cascade

Issues #29, #30, #32: the Champions/Europa/Conference League qualifying
rounds Norway's Eliteserien clubs enter (`format: european`, `movable:
false`) resolve differently from a domestic cup round, for three reasons a
cup doesn't have. First, not every listed team enters every round —
2026-27's Champions League runner-up (Bodø/Glimt) enters the third
qualifying round while the champion (Viking) enters straight at the
play-off round — so each round declares its own `ties` rather than
inheriting the whole competition's team list (`EuropeanRound.entrants` is
the plain team-id list derived from it). Second, a round is *two* legs, not
one date or window covering both — `first_leg`/`second_leg`, each its own
`EuropeanLeg` (the same forced-date-XOR-window shape `CupRound` uses,
factored out as `_DateSpec` so both share it). Third, and the reason for
issue #29's cascade and issue #32's conditional fixtures: a leg's opponent
is often known well ahead of the tie (`EuropeanTie.opponent`), but whether
a team is still in *this* round at all — rather than having lost the one
before and dropped into a different UEFA competition's equivalent round —
often isn't.

Modelling a round as two separate legs rather than one span covering both
is what makes a normal European week actually schedulable: a club can
legitimately play Thursday (a leg) – Sunday (a league match) – Thursday
(the next leg), and blocking only each leg's own date (plus
`min_rest_days`) leaves that Sunday free, where blocking the whole span
between the legs would rule it out by construction — the mistake an
earlier version of this feature made, and that Thu-Sun-Thu week is common
once a club is through to a second continental round.

`terminliste/rounds/european_schedule.py` doesn't guess which branch of the
cascade is real. `resolve_all_legs` first resolves every declared round's
two legs to actual dates — a `forced_date` honoured exactly, a window
resolved to its earliest non-blackout day, the same idea as
`rounds/cup_schedule.py`'s own resolution, just with no round-to-round
ordering to derive since each `EuropeanRound`'s dates come from real,
independently-sourced UEFA scheduling data rather than being inferred from
where the previous round landed. Then, starting from a team's entry round,
`resolve_team_cascade` walks every reachable round — the next round in the
same competition if the team survives (implicit: no pointer needed, just
listed as that round's own entrant too), and whatever
`drop_to_competition`/`drop_to_round` names if it doesn't — collecting both
legs' resolved dates from every round reached, across every cascade branch
still open, as a flat list of `EuropeanCommitmentDate`. `EuropeanCommitmentConflict`
(`scoring/hard.py`) is `CupRoundConflict`'s counterpart for these — point-date-
plus-`min_rest_days`, the same arithmetic as `cup_conflict`, not a range.

Resolving a real result is a data edit, not a re-model: once a tie is
decided, delete the `EuropeanRound` on the branch not taken (or turn a
leg's window into a `forced_date` once UEFA confirms it) and regenerate —
issue #32's "harder case" (a conditional fixture whose date, once
triggered, is fixed and non-reschedulable, forcing other fixtures to move)
needs no new mechanism beyond `forced_date` and a normal solver re-run. See
`data/competitions/champions_league_2026.yml`, `europa_league_2026.yml` and
`conference_league_2026.yml` for the real 2026-27 entrants, dates and (where
UEFA has confirmed them) opponents and home/away — Champions League is
modelled end to end; Europa League's third-qualifying-round-to-Conference-
League-play-off hop is the one cascade wired between two competitions on
real data, demonstrating the mechanism, while every other branch that ends
in a competition's league phase (out of this project's scope, which stops
at qualifying) is intentionally left without a `drop_to_*` — see those
files' headers for exactly which hops are wired and why the rest are a
documented gap. European rounds also show up in the generated report
(`_european_views`/`_european_entries` in `report/render.py`, mirroring the
cup pair): the real opponent where UEFA has named one, "TBD" where the tie
is still conditional. A written plan for all of this lives in
`docs/european_qualifiers_plan.md`.

### Main tournaments (Champions/Europa/Conference League)

Issue #79 lifts the "qualifying only" boundary above for the three UEFA
competitions' *main* tournaments — league phase plus knockout rounds —
modelled as their own `Competition` entries (`champions_league_main_2026.yml`
and friends), separate from the qualifying ones: `is_main_tournament: true`,
`league_phase_matchdays` (a single date per matchday, like a `CupRound`, not
two legs — the whole league phase plays across shared matchdays) and
`knockout_rounds` (`MainTournamentRound`: two legs like a qualifying round,
except the final, whose `second_leg` is omitted and whose `venue_name` names
a venue fixed years in advance and almost always outside Norway — freeform
text rather than a `Venue.id` reference, since `world.venues` only models
Norwegian grounds).

A main tournament has no static `teams` list — which of several possible
tournaments a qualifying entrant ends up in isn't known until results are
in — so `reachable_from` names the *qualifying* competitions whose entrants
(every team listed in any of their rounds) are reachable here, and every one
of them is assumed to reach the final, the same "assume it goes all the way"
simplification `rounds/cup_schedule.py` makes for NM Cupen's final. This is
coarser than UEFA's real round-by-round drop rule — which round a team is
eliminated from decides whether it can fall as far as the next competition
at all, something this project doesn't attempt to model precisely (see
`Competition.reachable_from`'s docstring in `schema.py`).

`reachable_from` only crosses into another competition where the qualifying
data itself already wires that hop via `drop_to_competition`/
`drop_to_round` — `europa_league_main_2026.yml` lists `europa_league_2026`
plus (via the one real wired hop) `conference_league_2026`'s entrants
reaching it; `champions_league_2026.yml` deliberately wires no
`drop_to_competition` at all, so no main tournament's `reachable_from` lists
it as a source for anyone but `champions_league_main_2026` itself. That
restriction isn't just for consistency: an earlier version listed
`champions_league_2026` under every main tournament's `reachable_from`
(matching the issue's own "both CL, EL and UECL are reachable" framing
literally), which meant a Champions League qualifying entrant blocked its
own dates across all three main tournaments' calendars simultaneously —
`cli.py generate` found zero feasible schedules even at the full 180s
default budget, with `european_commitment_conflict` overwhelmingly the most
common dead end (86,236 of 86,236 investigated scenarios hit one, more than
`min_rest_days` or any other hard rule). Since a team is only ever in *one*
main tournament's league phase at a time, not all three at once, unioning
every reachable tournament's whole calendar for one team is a real
over-approximation, not just a documented simplification.

Narrowing `reachable_from` to what the qualifying cascade already
corroborates (above) measurably helps — the best candidate at 180s goes from
3-6 hard violations down to 2 — but used to fall short of full feasibility
for a further reason: Tromsø IL is reachable for both
`europa_league_main_2026` and `conference_league_main_2026` (the one real
wired cascade hop), and until issue #93 both blocks were unconditional hard
exclusions, as if Tromsø were certain to reach *both* main tournaments —
when winning Europa League Q3 (reaching the first) and losing it (the only
way to reach the second, via the Conference League drop) are mutually
exclusive outcomes.

Issue #93 fixes that: `resolve_team_cascade` now tags every commitment
`EuropeanCommitmentDate.certain` — true only for the entry round and
anything reached without ever passing through a fork (a round offering both
win-progression and a `drop_to_competition`/`drop_to_round`), false for
everything reachable only via one side of a fork, including all of it
transitively once a branch has forked. `resolve_main_tournament_commitments`
reads the same flag to decide each reachable team's block: certain only if
at least one named `reachable_from` source is fully certain for that team.
Tromsø's route to each main tournament runs through the Q3 fork, so both
blocks are now soft (`EuropeanCommitmentSoftConflict`, `scoring/soft.py`)
rather than hard — still steering the solver away from the date when a
cheaper alternative exists, but no longer able to rule out an otherwise
workable schedule over a branch that never materializes. The same certain/
uncertain split applies to `EuropeanCommitmentConflict` itself for the
qualifying cascade, and to `solvers/cpsat.py`'s candidate-domain pruning
(`_european_clear` now only excludes a date over a *certain* commitment) —
which is what let a 120s `cli.py generate --solver cpsat` run stop naming
`european_commitment_conflict` among its infeasibility culprits at all,
where it used to dominate. Closing the *remaining* gap on the real 2026 data
(Bodø/Glimt's and Viking's dense, non-branching Champions League qualifying
calendar — see the companion issue) is unrelated to cascade branching and
not attempted here.

`resolve_main_tournament_commitments` (`rounds/european_schedule.py`)
resolves each main tournament's own dates once — no branching to walk, since
every reachable team blocks the same list — and merges them into the same
`EuropeanCommitmentDate` list `resolve_european_commitments` already builds
from the qualifying cascade, so `EuropeanCommitmentConflict` and both
solvers' cup/European candidate-date pruning need no changes at all to cover
main tournaments too. The one genuinely new piece of resolution logic is
`preferred_weekday`-biased date selection: `resolve_leg_date` now prefers the
first occurrence of a competition's `preferred_weekday` inside a window
(falling back to the plain earliest day when the window doesn't contain
one), so Champions League's dates lean Thursday and Europa/Conference
League's lean Tuesday/Wednesday, per the issue.

Because main tournament competitions are `movable: false` and resolved
before the domestic solver ever runs (same as qualifying), "European games
should be placed first, and specific dates shouldn't move" falls out of the
existing architecture for free rather than needing new placement-order code.

**Report display.** `report/render.py`'s `_european_views`/`_european_entries`
only know how to read a competition's `european_rounds` — a main tournament
has none, since its dates live in `league_phase_matchdays`/`knockout_rounds`
instead. Rather than teach those two functions a second shape,
`build_main_tournament_rounds_for_display` (`rounds/european_schedule.py`)
adapts a main tournament into the same `EuropeanRound`/`ResolvedLegs` shape
`resolve_all_legs` already produces for qualifying — every reachable team
becomes a synthetic `EuropeanTie` (opponent "TBD", `home_leg` unset, since
pairings this far ahead genuinely aren't known) — and `cli.py`'s
`_european_report_data` swaps in a display-only copy of the competition
(`Competition.model_copy(update={"european_rounds": ...})`, which doesn't
re-validate) carrying that adapted round list. The rendering code itself
needed no changes at all beyond one label branch (`is_main_tournament` in
the by-competition card's summary text) and folding a knockout round's
`venue_name` into its displayed `note`, since neither `_european_venue_type`
nor `_european_venue_label` distinguish a synthetic tie's ground from a
real one's — both read "unknown"/"not yet confirmed" for every main
tournament fixture, which is honest but doesn't call out a final's actual,
known venue on its own fixture rows the way the round-level note does.

## Constraints implemented

**Hard** (`terminliste/scoring/hard.py`) — a schedule with any of these
present is not feasible, full stop:
`min_rest_days`, `blackout_dates`, `venue_double_booking`, `club_home_clash`,
`leg_ordering`, `fixed_requirement`, `one_match_per_team_per_day`,
`cup_round_conflict`, `european_commitment_conflict` (a team's league
matches stay clear of its resolved UEFA qualifying leg dates — see
"European qualifying cascade" above), `final_round_same_slot` (every league's final round
shares one date and kickoff time — `Competition.final_round_kickoff_time`,
enforced by pinning the round's fixtures to one candidate date up front in
both solver backends, see `solvers/greedy.py::resolve_round_pins`),
`full_round_on_date` (every team in a competition has a match on a named
date — `Season.full_round_requirements`; May 16 in Eliteserien).

**Soft** (`terminliste/scoring/soft.py`) — scored, signed (reward positive,
penalty negative), and shown ranked in the report:
`preferred_weekday`, `consecutive_home_days`, `consecutive_away_days` (scaled
by travel time, capped at an 8h default), `home_away_breaks`,
`home_away_balance`, `rest_comfort`, `soft_venue_preference`,
`grass_away_round_one` (grass-pitch clubs reward playing away in round 1),
`late_kickoff_long_travel` (penalises a late Sunday kickoff for an away team
with a long trip home — tunable `late_from`/`long_travel_hours`),
`rivalry_fixture_on_date` (a fixed annual pairing, home side alternating by
year — `Season.rivalry_fixtures`; Bodø/Glimt vs Tromsø IL on May 16), and
`tv_time_spread` (issue #76: a round's matches on the preferred weekday
following a TV-broadcast shape — most at a primary kickoff time, one shifted
early, one shifted late — plus a penalty for colliding, at the same date and
kickoff time, with a match from a different competition).

Kickoff time itself isn't a search variable the way date and venue are —
`rounds/kickoff.py::assign_kickoff_times` fills in `Match.kickoff_time` once
a schedule's dates are fixed: the final round gets its competition's forced
slot, a match an explicit `FixedRequirement.kickoff_time` names (Tromsø's
Midnight Sun Match) gets that, a competition opted into
`Competition.tv_time_spread` gets its preferred-weekday round matches shaped
as described above, and everything else gets one of `Competition.kickoff_slots`
chosen deterministically per fixture. Nothing in either solver backend
currently searches over kickoff choice to *improve* `late_kickoff_long_travel`'s
or `tv_time_spread`'s score — same kind of known gap as CP-SAT's
`home_away_breaks` handling below, not something to be surprised by in a
generated schedule.

Run `python cli.py score <file> --season 2026` on any schedule to see every
rule's contribution, with named examples for anything that fired.

## Baselines

A score is only meaningful against something. `baselines/` keeps the real,
published fixture lists in-tree — each as a CSV plus a provenance sidecar
saying where it came from, when, and whether anyone has verified it — and
`scripts/refresh_baselines.py` re-scores them through the same constraint
registry the solver uses, writing a committed JSON + Markdown report per
baseline.

```bash
python scripts/refresh_baselines.py           # rewrite baselines/reports/
python scripts/refresh_baselines.py --check    # non-zero exit if stale
```

Re-run it after any change that could move the number — a new constraint, a
re-weighted old one, a correction to `data/` — and commit the report diff
with the change. That diff is the artefact worth having: it says in points
what the edit did to a schedule nobody made up. `tests/test_baselines.py`
fails if the committed reports are stale, so this cannot be forgotten
quietly.

What's committed today is round 1 of both 2026 leagues, not the full season:
every fixture source is blocked by this sandbox's egress policy, and search
gave trustworthy pairings only for the opening rounds. `baselines/README.md`
has the full account and the rules for reading a partial baseline's
hard-violation count; `baselines/SOURCING_FIXTURES.md` and
`scripts/fetch_real_schedule.py` are the recommended path (TheSportsDB, with
an API-Football fallback) to fetching and converting the rest from a machine
with real network access.

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

## Travel time

The `consecutive_away_days` soft rule (below) only rewards a back-to-back
away pairing if the squad can plausibly get from one venue to the other, so
it needs a real travel-time number, not a straight-line guess. There's no
self-rolled distance-over-average-speed model for roads: `python cli.py
refresh-travel-times` fetches an actual driving route (time and distance)
for every venue pair from [OSRM](https://project-osrm.org)'s free public
routing API (`terminliste/refdata/travel_client.py`), and caches the result
for 30 days in `data/.refdata_cache/travel.json`
(`terminliste/refdata/travel_refresh.py`) — gitignored, so it starts empty
in a fresh clone, same as the `refresh-reference-data` cache.
`ApiTravelModel` (`terminliste/model/travel.py`) only ever reads that cache;
it never calls the network itself, keeping `validate`/`generate`/`score`
fully offline.

Great-circle distance is a bad proxy for a road (Norwegian roads follow
fjords and go around mountains), but it's *exactly* what a flight covers —
so it's still used, just honestly, to estimate air time: cruise speed plus a
fixed door-to-door overhead for check-in/security/boarding. Air time is
checked whenever ground travel is missing (no cached route yet) or slow, and
the cheaper of the two wins. A pair where neither gets under
`UNTRAVELABLE_THRESHOLD_HOURS` (8h by default) is flagged unreachable.
`travel_overrides.yml` still wins over both, for the rare pair the API (or
the flight estimate) gets wrong — mountain crossings with no direct route,
or the far north where nothing short of flying is realistic.

Like `refresh-reference-data`, this fails soft: the same sandboxed dev
environment that blocks TheSportsDB also blocks OSRM, so
`refresh-travel-times` here will report every pair as failed and leave the
cache empty — `ApiTravelModel` falls straight through to the flight
estimate for every pair until it's run somewhere with real network access.

## Testing

```bash
python -m pytest tests/ -v
```

- `test_round_robin.py` — exhaustive n=2..20 checks on the pairing generator,
  plus generic triple-round-robin (`rounds_per_pairing=3`) checks: every
  pairing meets three times with a 2-1 split, and every team's season-total
  home/away count is within 1 of even. Both leagues this project actually
  schedules are double round-robins; the generator supports any
  `rounds_per_pairing`, and these tests are what back that generality.
- `test_hard_constraints.py` / `test_soft_constraints.py` — each rule at its
  boundary, hand-built 4-team schedules, plus a check that every hard rule is
  silent on a clean schedule.
- `test_calendar.py`, `test_travel.py`, `test_loader.py` — the supporting
  model layer, including the ground-cache/air-estimate/override precedence
  in `ApiTravelModel`, referential-integrity errors, and a competition's own
  (optionally narrower) start/end window.
- `test_cup_schedule.py` — resolving a cup's forced dates/windows to real
  per-team dates: round ordering enforced and rejected when infeasible,
  teams spread within a round's window, and the blackout-fallback warning.
- `test_european_schedule.py` — resolving a team's UEFA qualifying cascade:
  entry-round detection, win-progression to the next round, following
  `drop_to_competition`/`drop_to_round`, merging same-depth branches into
  one window, and the error paths (unknown entrant, dangling cascade
  pointer).
- `test_refdata.py` — the reference-data client, cache and diff logic behind
  `cli.py refresh-reference-data`, entirely mocked at the HTTP layer.
- `test_travel_refresh.py` — the OSRM client and cache-refresh logic behind
  `cli.py refresh-travel-times`, also entirely mocked at the HTTP layer.
- `test_external_schedule.py` — CSV/JSON parsing, leg inference, coverage
  warnings, and a schedule with deliberate flaws scoring the right hard
  violations.
- `test_baselines.py` — the committed baselines load and score, every
  baseline has a report, the reports are not stale, and no baseline breaks a
  hard rule its sidecar hasn't declared.
- `test_fetch_real_schedule.py` — team-name matching (diacritics, club-type
  tokens, gender disambiguation, near-miss fuzzy matches) and both source
  JSON schemas, entirely offline via `--from-json`.
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
The Norwegian Cup is in (`cup_men_2027.yml` / `cup_women_2027.yml`,
`terminliste/rounds/cup_schedule.py`, `CupRoundConflict`) — only the
top-flight clubs' rounds are tracked, and only Round 1 (men), Round 2
(women), and the "final in spring 2027, end of May" framing are
NFF-confirmed; every other round is an estimated window, flagged per round
via `note`, pending NFF publishing the real ones (see each file's header).

European qualifiers (issue #31) and the Champions/Europa/Conference League
cascade (issue #29) are in too — see "European qualifying cascade" above
and `docs/european_qualifiers_plan.md`. Champions League is modelled end to
end for both Norwegian entrants; Europa League and Conference League carry
their own real rounds and entrants but only one cascade hop between them is
wired (Europa League Q3 -> Conference League play-off, the one real
UEFA rule that lands on another *qualifying* round rather than a league
phase this project doesn't track).

That league-phase boundary is now crossed — issue #79 adds each
competition's *main* tournament (league phase plus knockout rounds) as its
own competition, see "Main tournaments" above. Its `reachable_from`
simplification (every entrant of a listed qualifying competition is assumed
able to reach a main tournament's final) is coarser than the boundary it
replaces and a natural next step in its own right: modelling UEFA's real
round-by-round drop rule precisely (which round a team is eliminated from
decides whether it can fall as far as the next competition at all) would
tighten `reachable_from` from "every entrant of this whole qualifying
competition" down to "every entrant still alive from this specific round
onward," at the cost of the same kind of round-to-round wiring
`drop_to_competition`/`drop_to_round` already does for the qualifying
cascade.

Both solvers already prune candidate dates that conflict with a resolved
European commitment up front — `solvers/greedy.py`'s cost function
(`_EUROPEAN_CONFLICT`) and `solvers/cpsat.py`'s candidate-set construction
(`_european_clear`, composed with the existing `_cup_clear`) — mirroring
how each already treats `cup_conflict`. European commitments block only
each leg's own resolved date plus `min_rest_days`, not the whole span
between a tie's two legs — an earlier version of this modelled a round as
one range covering both legs, which ruled out a normal Thu-Sun-Thu week by
construction; that's fixed. Even so, `cli.py generate`'s default
`--time-budget` is 180s, not the 30-60s a season with no European
commitments needs: measured across the same seeds the window-based model
struggled on, the per-leg model reaches a feasible top option at 60s on
4 of 6, needs up to 120s on a 5th and up to 180s on the 6th — real headroom
over the no-Europe case, just far less than the old model's need for a
full 300s on nearly every seed. 180s is that measured ceiling, not extra
margin on top of it — the scheduler runs once a season, so a slower seed
or slower hardware pushing past it occasionally is an acceptable trade for
not defaulting every run to a longer wait than usually needed.

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

Same story for `refresh-travel-times` (issue #27): run it somewhere with
real network access to actually populate `data/.refdata_cache/travel.json`
from OSRM — in this sandbox every pair falls through to the flight-time
estimate, since the egress policy blocks `router.project-osrm.org` too.
