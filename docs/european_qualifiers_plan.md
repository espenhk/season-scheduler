# European qualifiers: representation plan (issues #29, #30, #31, #32)

This is the written plan issue #29 asks for, covering all four issues
together — #30, #31 and #32 were opened as the general mechanisms this
project needed in order to model #29's actual use case, and the shipped
implementation treats them as one piece of work rather than four unrelated
ones. Where a section says "implemented", the corresponding code and data
already exist and are tested; "sketched" means the mechanism supports it
but the real 2026-27 data doesn't currently exercise it.

## The problem, restated

Champions League, Europa League and Conference League qualifying interact:
a team eliminated from one competition's qualifying round can drop into a
different competition's equivalent round rather than being out of Europe
entirely. From this project's point of view — scheduling Eliteserien and
Toppserien around commitments it doesn't control — three things make this
harder than the Norwegian Cup, which already had a similar-looking
"forced date or vague window" mechanism (`CupRound`) before this work
started:

1. **Not every entrant plays every round.** A cup's entered teams are all
   assumed to reach the final (opponents are unknown, but survival isn't in
   question for scheduling purposes). A European qualifying round's entrant
   list is different per round — Norway's 2026-27 Champions League
   runner-up enters at the third qualifying round, its champion enters
   directly at the play-off round — so "which teams are in this round" has
   to be a property of the round, not the competition.
2. **Whether a team is even in a given round is genuinely unknown.** A cup
   round's *date* might be vague, but the team's presence in it isn't. A
   European qualifying round's date can be just as vague (real dates are
   published a season ahead as target weeks, then confirmed leg by leg),
   *and* whether the team survived to play it is unknown until the previous
   tie is decided.
3. **A loss doesn't mean "out" — it means "somewhere else."** Losing a
   Champions League qualifying round can mean dropping into the Europa
   League's equivalent round, which itself cascades into the Conference
   League. The domestic scheduler needs to treat the team as still
   potentially committed on those dates regardless of which branch is real,
   until a result narrows it down.
4. **A round is two legs, each with its own date — not one span.** A cup
   round is a single date or window. A European qualifying round is a
   two-legged tie, and the two legs can be a week or more apart; blocking
   the whole span between them (rather than each leg on its own) rules out
   a perfectly normal European week — a leg, a domestic league match, the
   next leg — that this project needs to be able to produce.

## Representation (issue #29, #30)

`EuropeanRound` (`terminliste/model/schema.py`) reuses `CupRound`'s
forced-date-XOR-window mechanics — issue #30's ask, generalised — but at
the *leg* level rather than the round level, via a shared `_DateSpec` base
(the bare date-or-window fields and their validator, with no `id`/`name` of
their own) that both `CupRound` (through `_ScheduledRound`, which adds
`id`/`name`/`note`) and `EuropeanLeg` build on. `forced_date` when a leg's
date is confirmed, `window_start`/`window_end` (+ `granularity`) when only
the week, month or quarter is known, and a `forced_date` can always replace
a window later without touching anything downstream — narrowing a vague
date is a data edit, not a re-model, the same guarantee cup rounds already
had.

`EuropeanRound` itself carries:

- `first_leg` / `second_leg: EuropeanLeg` — addressing point 4 above: a
  round's two legs resolve to two independent dates, not one span.
- `ties: list[EuropeanTie]` — which of the competition's teams actually
  play this specific round (`team`), and, where UEFA has confirmed them,
  the `opponent` (defaulting to `"TBD"`) and which leg is the team's home
  leg (`home_leg`, `None` when not yet settled). Addresses point 1 above;
  `EuropeanRound.entrants` is the plain team-id list derived from it.
- `drop_to_competition` / `drop_to_round` — where an entrant lands if
  eliminated *here*, naming a round in a different `Competition`. Both are
  `None` when a loss here has no further qualifying round to track (see
  "Scope boundary: qualifying only" below).

A `Competition` gains `format: "european"` alongside `"league"`/`"cup"`,
and `european_rounds: list[EuropeanRound]` alongside `cup_rounds`. Three
competitions carry this format for 2026-27:
`data/competitions/champions_league_2026.yml`, `europa_league_2026.yml`,
`conference_league_2026.yml` — one per UEFA competition, each declaring
only the rounds that have a live Norwegian entrant.

## Movable vs. fixed (issue #31)

`Competition.movable: bool` is an explicit flag rather than something
inferred from `format`. In the shipped data every `league` competition is
`movable: true` and every `cup`/`european` competition is `movable: false`,
but making it explicit means the data says outright which tournaments this
project schedules and which it only schedules *around* — exactly what issue
#31 asked for as an acceptance criterion, not just a re-statement of
`format`. Mechanically, a movable competition's fixtures are generated and
dated by the solver (`season.competitions`); a non-movable one is resolved
once, up front (`cup_schedule.py` / `european_schedule.py`), and the result
is handed to the solver as a set of dates to schedule around, never as
something either solver backend proposes moving.

## Resolving the cascade (issue #29, #32)

`terminliste/rounds/european_schedule.py` works in two passes. First,
`resolve_all_legs` resolves every declared round's two legs to actual
dates, independently of the cascade: a `forced_date` honoured exactly, a
window resolved to its earliest non-blackout day — the same idea as
`rounds/cup_schedule.py`'s own resolution, just with no round-to-round
ordering to derive, since each `EuropeanRound`'s dates come from real,
independently-sourced UEFA scheduling data rather than being inferred from
where the previous round landed.

Second, `resolve_team_cascade` walks a team's cascade from its entry round
rather than trying to predict a result. Two edges leave any given round for
a given team:

- **Win-progression**: the next round in the same competition's
  `european_rounds` list, if the team is listed among that round's
  `entrants` — no cascade pointer needed, since staying in the same
  competition is the default.
- **Elimination**: `drop_to_competition`/`drop_to_round`, if set — a
  cascade into a different, lower competition's round.

Both are followed unconditionally, because which one is real is exactly the
unknown. Every round reached this way contributes both of its legs'
already-resolved dates to the result — a flat list of
`EuropeanCommitmentDate` (team, date, `min_rest_days`, a label naming the
round and leg), not a single merged range. This was a deliberate range-based
simplification in an earlier version of this design — one blocked span per
cascade depth, on the reasoning that UEFA keeps a qualifying stage within
the same week or two across all three competitions — but a span blocks the
*entire* gap between a tie's two legs along with the legs themselves, which
rules out a normal European week (a leg, a league match, the next leg) by
construction. Resolving each leg to its own point date and blocking only
that date (plus `min_rest_days`) is both more precise and the thing that
actually makes such a week schedulable.

`EuropeanCommitmentConflict` (`terminliste/scoring/hard.py`) is the
constraint that acts on the result: it is `CupRoundConflict`'s counterpart,
keeping a team's league matches at least `min_rest_days` clear of every
date in its resolved cascade — the same point-date-plus-rest arithmetic as
`cup_conflict`, not a range check.

**Issue #32's "harder case"** — a conditional fixture whose date, once
triggered, is fixed and non-reschedulable, forcing other movable fixtures
to move — falls out of this without new machinery. Resolving a real result
is a data edit: delete the `EuropeanRound` on the branch not taken (or turn
the surviving one's window into a `forced_date` once UEFA confirms it), and
regenerate. The solver already treats a `forced_date` on a non-movable
competition as a hard constraint to schedule around; a regenerated schedule
reshuffles whatever movable fixture now conflicts, the same way it already
would for a newly-added blackout date or fixed requirement. No mechanism
this project didn't already have is needed for that half of issue #32 — the
work was building the *conditional* half (issue #29's cascade), which this
document is otherwise about.

## Scope boundary: qualifying only (crossed by issue #79)

This project originally tracked only UEFA *qualifying* rounds, not the
group/league phase that follows. A team eliminated from a competition's
last qualifying round (or, on some paths, its third qualifying round)
doesn't fall out of Europe under the post-2024 CL/EL/UECL format — it drops
straight into the next competition's *league phase*: many matchdays spread
across autumn, not a single round with a date to avoid. Modelling that would
mean tracking a whole second competition structure (a mini-league, not a
knockout cascade) for a team that might end up in any of three different
ones depending on results neither this project nor its data source can know
in advance. `drop_to_competition`/`drop_to_round` is `None` wherever the
real rule sends a losing team into a league phase rather than another
qualifying round — that was a deliberate boundary, not an oversight, and is
still called out in each qualifying data file's header at the specific
round it applies to.

Issue #79 crosses that boundary: `champions_league_main_2026.yml` and its
Europa/Conference League counterparts model each competition's league phase
and knockout rounds as their own `Competition` (`is_main_tournament: true`),
separate from the qualifying one. See "Main tournaments" below for how they
avoid needing the "whole second competition structure" this section used to
rule out.

## What's fully implemented vs. sketched, for 2026-27

- **Champions League — fully implemented.** Both Norwegian entrants
  (Bodø/Glimt at the third qualifying round, Viking at the play-off round)
  are modelled with real dates. Neither round sets `drop_to_*`: League Path
  Q3 losses and play-off losses on either path both go to the Europa
  League league phase directly, which is the scope boundary above, not a
  gap.
- **Europa League — Conference League cascade — implemented, real data.**
  Europa League's third-qualifying-round entry (`q3`) sets
  `drop_to_competition: conference_league_2026`, `drop_to_round: playoff` —
  the one real, sourced UEFA rule ("third-qualifying-round losers enter the
  Conference League play-off round") that lands on another qualifying round
  rather than a league phase, so it's the hop this project actually wires
  between two competitions on live data rather than leaving as a documented
  gap.
- **Conference League — sketched.** Brann's own progression through the
  competition (Q2 -> Q3 -> play-off) is modelled with real dates, including
  receiving Europa League's cascade at the play-off round. What isn't
  wired: the Conference League's own play-off round has no `drop_to_*`
  (its losers enter the Conference League league phase itself, the same
  scope boundary as everywhere else), and this project doesn't model
  Conference League Q1 (no Norwegian entrant this cycle, so it's omitted
  rather than added as an inert empty round).
- **Mechanism generality — proven by tests, not just by the real data.**
  `tests/rounds/test_european_schedule.py` exercises multi-hop cascades, branch
  merging at a shared depth, and the error paths on synthetic data, so the
  code path is verified independent of whether the current season's real
  UEFA rules happen to exercise every hop.

## Main tournaments (issue #79)

Why not just extend `EuropeanRound`? A qualifying round is one two-legged
tie against one named opponent; a league-phase matchday is many teams' games
across one shared date, and a knockout round after the play-off is
two-legged like qualifying except the final, which is a single match at a
venue fixed years in advance. `EuropeanRound.ties` and its `first_leg`/
`second_leg` pair don't fit either shape, so two new models exist instead:
`EuropeanMatchday` (a bare `_ScheduledRound` — the same shape `CupRound`
already uses, since neither cares about opponents, only "when") and
`MainTournamentRound` (`EuropeanRound`'s two-leg shape, minus `ties`, plus an
optional `venue_name` valid only when `second_leg` is omitted).

**Reachability, not cascade-walking.** The qualifying cascade's
`resolve_team_cascade` walks forward from a team's actual entry round,
branch by branch, because *which* round a team is playing in a given week
matters — it decides which two dates get blocked. A main tournament's
matchday calendar doesn't depend on which team is asking: every reachable
team blocks the same list. So instead of a walk, `Competition.reachable_from`
just names which qualifying competitions feed this main tournament, and
`resolve_main_tournament_commitments` takes the union of every entrant
across every round of every named competition. This is deliberately coarser
than the qualifying cascade's `drop_to_competition`/`drop_to_round`, which
does encode *which round* triggers *which* drop. Tightening
`reachable_from` to be round-aware — "only a team eliminated at the
play-off round or later can reach the Europa League league phase," per
UEFA's real rule — is exactly the kind of round-to-round wiring
`drop_to_competition`/`drop_to_round` already does, and is a natural
follow-up once a reliable source for the exact drop rules per round is
reachable from this sandbox (see README's "Sourcing" notes).

Issue #93 narrows a different, non-UEFA-specific corner of that same
coarseness: the union in `_reachable_teams` still doesn't know which
qualifying rounds are mutually exclusive, but `resolve_main_tournament_commitments`
no longer *hard*-blocks a team against every main tournament its qualifying
entrants reach — it reads `resolve_team_cascade`'s per-commitment `certain`
flag (true only for a commitment reached without crossing a cascade fork)
and blocks a team's dates as hard only where at least one named
`reachable_from` source is fully certain for that team; everywhere else the
block is soft (`EuropeanCommitmentSoftConflict`, `scoring/soft.py`). A team
one drop hop from a second main tournament — reachable for both, but never
both at once — no longer has to be treated as certainly in both at the same
time just because the model doesn't track which round it was eliminated
from.

**Desired matchday.** `Competition.preferred_weekday` already existed (the
league soft-preference `PreferredWeekday` in `scoring/soft.py` reads it) but
had never been wired into window resolution. `resolve_leg_date` now takes an
optional `preferred_weekday` and, for a *window* (never a `forced_date`,
which is already exact), prefers the first matching, non-blacked-out day —
falling back to the old earliest-day behaviour when the window doesn't
contain one. Reusing the same field instead of adding a matchday-level one
keeps the "CL Thursday, EL/UECL Tuesday/Wednesday" convention a one-line
data edit per competition, not new schema.

**No new placement machinery.** Main tournament competitions are
`movable: false` and resolved by `resolve_european_commitments` before the
domestic solver ever runs, feeding the same `EuropeanCommitmentDate` list
`EuropeanCommitmentConflict` and both solvers' candidate-pruning already
consume for qualifying commitments — "placed first, and specific dates
don't move" was already true of that whole path, so main tournaments get it
for free rather than needing issue #79 to add anything new there.
