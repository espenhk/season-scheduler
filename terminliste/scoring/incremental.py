"""Incremental scoring for local search (#11).

A move touches a handful of matches, yet a full evaluation re-scores the whole
season. Most rules are sums over independent units — a team's match list, a
venue-day, a club, one match — so a move only changes the units it touches.

A constraint opts in by declaring `delta_scope`, the unit its total decomposes
over: evaluating it on a view holding just one unit returns exactly that
unit's contribution. `DeltaScorer` caches each unit's contribution, re-scores
only the touched units per move, and keeps the schedule index up to date
incrementally. A constraint without a scope is re-evaluated in full every
move, so a rule that cannot decompose (or that is expensive to decompose,
such as one that re-plans fallbacks per evaluation) stays correct and merely
costs more.

Scopes:
    team         by_team / date-set views of the given teams
    club         the home/away date views of one dual club's teams
    venue_date   one (venue, date) cell
    match        a single match (its by_team view holds just that match)
    competition  all matches of one competition
    round        all matches of one competition round
    kickoff      depends on kickoff times, which search never changes; constant
                 while every kickoff is unset, otherwise evaluated in full
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date

from .base import HARD_PENALTY, Constraint, EvalContext, ScheduleIndex

_Fields = tuple[str, str, str, date]


class LiveIndex(ScheduleIndex):
    """A `ScheduleIndex` that follows in-place match mutation.

    Same contents and ordering as a freshly built `ScheduleIndex`, so a rule
    that is evaluated in full against it sees what it would see at rest.
    """

    def __init__(self, matches: Sequence) -> None:
        super().__init__(matches)  # type: ignore[arg-type]
        self._fields: list[_Fields] = [_fields_of(m) for m in matches]
        self.team_idx: dict[str, set[int]] = {}
        for i, match in enumerate(matches):
            self.team_idx.setdefault(match.home_team, set()).add(i)
            self.team_idx.setdefault(match.away_team, set()).add(i)
        self._away_dates: dict[str, set[date]] = {
            t: set(dates) for t, dates in self.away_dates_by_team.items()
        }
        # Per team, so one edit per rebuild. `away_venue_by_team_date` is
        # not kept current between resyncs; the club views read this instead.
        self.away_venues: dict[str, dict[date, str]] = {}
        for (team, day), venue in self.away_venue_by_team_date.items():
            self.away_venues.setdefault(team, {})[day] = venue
        self._log: list[tuple] = []

    def sync(self, changed: Iterable[int]) -> None:
        """Bring the index in line with the changed matches' current fields.
        The edits are logged so `undo` can put the index back without
        recomputing anything."""
        matches = self.matches
        self._log = log = []
        touched: set[str] = set()
        for i in changed:
            match = matches[i]
            old = self._fields[i]
            new = _fields_of(match)
            if old == new:
                continue
            old_home, old_away, old_venue, old_day = old
            if (old_venue, old_day) != (new[2], new[3]):
                self._edit(self.by_venue_date, (old_venue, old_day), _without(
                    self.by_venue_date.get((old_venue, old_day), []), match))
                self._edit(self.by_venue_date, (new[2], new[3]),
                           [*self.by_venue_date.get((new[2], new[3]), []), match])
            if old_day != new[3]:
                self._edit(self.by_date, old_day, _without(self.by_date.get(old_day, []), match))
                self._edit(self.by_date, new[3], [*self.by_date.get(new[3], []), match])
            for team in (old_home, old_away):
                if team not in (new[0], new[1]):
                    self._set_idx(team, self.team_idx[team] - {i})
            for team in (new[0], new[1]):
                if i not in self.team_idx.get(team, ()):
                    self._set_idx(team, self.team_idx.get(team, set()) | {i})
            touched.update((old_home, old_away, new[0], new[1]))
            log.append((self._fields, i, old))
            self._fields[i] = new

        for team in touched:
            self._rebuild_team(team)

    def undo(self) -> None:
        """Revert the last `sync` (the matches themselves are restored by the
        caller)."""
        for store, key, old in reversed(self._log):
            if old is _MISSING:
                del store[key]
            else:
                store[key] = old
        self._log = []

    def _edit(self, store: dict, key, value) -> None:
        """Set (or, for an empty list/set, delete) `store[key]`, logging the
        previous value. Empty entries are dropped so the index matches a
        freshly built one."""
        self._log.append((store, key, store.get(key, _MISSING)))
        if value:
            store[key] = value
        else:
            store.pop(key, None)

    def _set_idx(self, team: str, members: set[int]) -> None:
        self._log.append((self.team_idx, team, self.team_idx.get(team, _MISSING)))
        self.team_idx[team] = members

    def _rebuild_team(self, team: str) -> None:
        matches = self.matches
        order = sorted(self.team_idx.get(team, ()))
        order.sort(key=lambda j: matches[j].date)
        mine = [matches[j] for j in order]
        self._edit(self.by_team, team, mine)
        home = {m.date for m in mine if m.home_team == team}
        away_matches = [m for m in mine if m.home_team != team]
        away = {m.date for m in away_matches}
        self._edit(self.away_venues, team, {m.date: m.venue for m in away_matches})
        self._edit(self.home_dates_by_team, team, home)
        self._edit(self.away_dates_by_team, team, away)
        self._edit(self._away_dates, team, away)


_MISSING = object()


def _without(matches: list, match) -> list:
    return [m for m in matches if m is not match]


def _fields_of(match) -> _Fields:
    return (match.home_team, match.away_team, match.venue, match.date)


_EMPTY: dict = {}


class _View:
    """What a constraint reads of a `ScheduleIndex`, restricted to one unit."""

    __slots__ = (
        "matches",
        "by_team",
        "by_venue_date",
        "by_date",
        "by_competition",
        "home_dates_by_team",
        "away_dates_by_team",
        "away_venue_by_team_date",
    )

    def __init__(
        self,
        matches=(),
        by_team=_EMPTY,
        by_venue_date=_EMPTY,
        by_competition=_EMPTY,
        home_dates_by_team=_EMPTY,
        away_dates_by_team=_EMPTY,
        away_venue_by_team_date=_EMPTY,
    ) -> None:
        self.matches = matches
        self.by_team = by_team
        self.by_venue_date = by_venue_date
        self.by_date = _EMPTY
        self.by_competition = by_competition
        self.home_dates_by_team = home_dates_by_team
        self.away_dates_by_team = away_dates_by_team
        self.away_venue_by_team_date = away_venue_by_team_date

    def team_matches(self, team_id: str) -> list:
        return self.by_team.get(team_id, [])


# Beyond this many touched units a rule is scored once over all of them
# rather than unit by unit.
_BATCH_FROM = 3


class _ClubWorld:
    """The world as one dual club's rules see it: `dual_clubs()` lists just
    that club, so a club-scoped rule loops over one club rather than all."""

    def __init__(self, world, clubs) -> None:
        self._world = world
        self._club = clubs

    def dual_clubs(self) -> list:
        return self._club

    def __getattr__(self, name: str):
        return getattr(self._world, name)


class DeltaScorer:
    """Search cost of a mutable schedule, kept current one move at a time.

    Usage per proposed move: mutate the matches, `propose(changed)`, then
    either `accept()`, or restore the matches and `reject()`.
    """

    def __init__(
        self, matches: Sequence, constraints: list[Constraint], ctx: EvalContext
    ) -> None:
        self.matches = matches
        self.constraints = constraints
        self.ctx = ctx
        world = ctx.world

        self._club_teams: dict[str, tuple[str, ...]] = {
            club.id: tuple(t.id for t in club.teams) for club in world.dual_clubs()
        }
        self._clubs = {club.id: club for club in world.dual_clubs()}
        self._club_ctx: dict[frozenset, EvalContext] = {}
        self._club_of: dict[str, str] = {
            team: club for club, teams in self._club_teams.items() for team in teams
        }
        self._round_matches: dict[tuple[str, int], list] = {}
        for match in matches:
            self._round_matches.setdefault((match.competition_id, match.round_index), []).append(
                match
            )

        self.resync()

    # -- full (re)computation ------------------------------------------------

    def resync(self) -> None:
        """Rebuild the index and every cached contribution from the matches."""
        matches = self.matches
        # Rebuilt in place: the move set holds a reference to this index.
        if hasattr(self, "live"):
            self.live.__init__(matches)
        else:
            self.live = LiveIndex(matches)
        kickoff_idle = all(m.kickoff_time is None for m in matches)

        self._scopes: list[str | None] = []
        for constraint in self.constraints:
            scope = getattr(constraint, "delta_scope", None)
            if scope == "kickoff":
                scope = "static" if kickoff_idle else None
            self._scopes.append(scope)

        self._cache: list[dict] = [{} for _ in self.constraints]
        self._total: list[float] = [0.0] * len(self.constraints)
        self._count: list[int] = [0] * len(self.constraints)
        self._hard = [i for i, c in enumerate(self.constraints) if c.kind == "hard"]
        # Rules that read only a team's home/away sequence, not its dates.
        self._order_only = [
            bool(getattr(c, "delta_order_only", False)) and self._scopes[i] == "team"
            for i, c in enumerate(self.constraints)
        ]
        self._has_order_only = any(self._order_only)
        self._scope_set = set(self._scopes)
        self._order = self._hard + [i for i, c in enumerate(self.constraints) if c.kind != "hard"]
        self._first_soft = len(self._hard)

        for ci, constraint in enumerate(self.constraints):
            scope = self._scopes[ci]
            if scope in (None, "static"):
                result = constraint.evaluate(self.live, self.ctx)
                self._total[ci], self._count[ci] = result.total, result.count
                continue
            total = 0.0
            count = 0
            for unit in self._all_units(scope):
                value = self._unit_value(constraint, scope, unit)
                self._store(ci, scope, unit, value)
                total += value[0]
                count += value[1]
            self._total[ci], self._count[ci] = total, count

        self.cost = self._cost_of(self._total, self._count)
        self._pending: tuple | None = None
        self._changed: list[int] = []

    def _cost_of(self, totals: list[float], counts: list[int]) -> float:
        cost = 0.0
        for ci, constraint in enumerate(self.constraints):
            if constraint.kind == "hard":
                cost += counts[ci] * HARD_PENALTY
            else:
                cost -= totals[ci]
        return cost

    def _all_units(self, scope: str) -> Iterable:
        live = self.live
        if scope == "team":
            return list(live.by_team)
        if scope == "club":
            return list(self._club_teams)
        if scope == "venue_date":
            return list(live.by_venue_date)
        if scope == "match":
            return range(len(self.matches))
        if scope == "competition":
            return list(live.by_competition)
        return list(self._round_matches)

    # -- unit evaluation -----------------------------------------------------

    def _view(self, scope: str, units) -> tuple[_View, EvalContext]:
        """A view holding exactly `units` (of one scope), and the context to
        evaluate it with. Evaluating a rule on it gives the sum of those
        units' contributions."""
        live = self.live
        if scope == "team":
            return _View(by_team={t: live.by_team[t] for t in units if t in live.by_team}), self.ctx
        if scope == "club":
            teams = [t for club in units for t in self._club_teams[club]]
            home = {t: live.home_dates_by_team[t] for t in teams if t in live.home_dates_by_team}
            away = {t: live.away_dates_by_team[t] for t in teams if t in live.away_dates_by_team}
            venues = {(t, d): v for t in away for d, v in live.away_venues.get(t, {}).items()}
            by_team = {t: live.by_team[t] for t in teams if t in live.by_team}
            view = _View(
                by_team=by_team,
                home_dates_by_team=home,
                away_dates_by_team=away,
                away_venue_by_team_date=venues,
            )
            key = frozenset(units)
            ctx = self._club_ctx.get(key)
            if ctx is None:
                world = _ClubWorld(self.ctx.world, [self._clubs[c] for c in sorted(units)])
                ctx = self._club_ctx[key] = replace(self.ctx, world=world)
            return view, ctx
        if scope == "venue_date":
            return (
                _View(by_venue_date={u: live.by_venue_date[u] for u in units if u in live.by_venue_date}),
                self.ctx,
            )
        if scope == "match":
            chosen = [self.matches[u] for u in units]
            grouped: dict[str, list] = {}
            by_team: dict[str, list] = {}
            for match in chosen:
                grouped.setdefault(match.competition_id, []).append(match)
                by_team.setdefault(match.home_team, []).append(match)
                by_team.setdefault(match.away_team, []).append(match)
            return _View(chosen, by_team=by_team, by_competition=grouped), self.ctx
        if scope == "competition":
            grouped = {u: live.by_competition[u] for u in units}
        else:
            grouped = {}
            for competition_id, round_index in units:
                grouped.setdefault(competition_id, []).extend(
                    self._round_matches[(competition_id, round_index)]
                )
        return _View([m for ms in grouped.values() for m in ms], by_competition=grouped), self.ctx

    def _unit_value(self, constraint: Constraint, scope: str, unit) -> tuple[float, int]:
        view, ctx = self._view(scope, (unit,))
        result = constraint.evaluate(view, ctx)  # type: ignore[arg-type]
        return (result.total, result.count)

    # -- per-move protocol ---------------------------------------------------

    def propose(
        self, changed: Sequence[int], prune: bool = False
    ) -> tuple[float, tuple[str, ...]]:
        """Score the matches' current state; returns (cost, ids of hard rules
        broken). Call `accept` to keep it or `reject` after restoring.

        With `prune`, a move that adds a hard violation returns an infinite
        cost without scoring the soft rules: its cost rises by a whole
        `HARD_PENALTY`, which no soft gain offsets, so it is rejected whatever
        they say.
        """
        live = self.live
        matches = self.matches
        old_fields = [live._fields[i] for i in changed]
        live.sync(changed)

        teams: set[str] = set()
        venue_dates: set[tuple[str, date]] = set()
        for i, before in zip(changed, old_fields, strict=True):
            after = live._fields[i]
            teams.update((before[0], before[1], after[0], after[1]))
            venue_dates.add((before[2], before[3]))
            venue_dates.add((after[2], after[3]))
        used = self._scope_set
        units: dict[str, object] = {"team": teams, "venue_date": venue_dates, "match": changed}
        if "club" in used:
            units["club"] = {self._club_of[t] for t in teams if t in self._club_of}
        if "competition" in used:
            units["competition"] = {matches[i].competition_id for i in changed}
        if "round" in used:
            units["round"] = {(matches[i].competition_id, matches[i].round_index) for i in changed}
        if self._has_order_only:
            units["team_order"] = self._teams_reordered(changed, old_fields)

        pending_cache: list[tuple[int, object, tuple[float, int] | None]] = []
        totals = list(self._total)
        counts = list(self._count)
        self._changed = list(changed)
        self._pending = (pending_cache, totals, counts)
        hard_before = sum(self._count[ci] for ci in self._hard)
        for position, ci in enumerate(self._order):
            if prune and position == self._first_soft:
                if sum(counts[hi] for hi in self._hard) > hard_before:
                    return float("inf"), self._broken(counts)
            constraint = self.constraints[ci]
            scope = self._scopes[ci]
            if scope == "static":
                continue
            if scope is None:
                result = constraint.evaluate(live, self.ctx)
                totals[ci], counts[ci] = result.total, result.count
                continue
            cache = self._cache[ci]
            touched = units["team_order" if self._order_only[ci] else scope]
            if len(touched) > _BATCH_FROM:
                # Many units: score them as one view. Their individual values
                # are only needed if the move is kept, so `accept` recomputes.
                view, ctx = self._view(scope, touched)
                result = constraint.evaluate(view, ctx)  # type: ignore[arg-type]
                for unit in touched:
                    before = cache.get(unit, (0.0, 0))
                    totals[ci] -= before[0]
                    counts[ci] -= before[1]
                totals[ci] += result.total
                counts[ci] += result.count
                pending_cache.append((ci, tuple(touched), None))
                continue
            for unit in touched:
                value = self._unit_value(constraint, scope, unit)
                before = cache.get(unit, (0.0, 0))
                totals[ci] += value[0] - before[0]
                counts[ci] += value[1] - before[1]
                pending_cache.append((ci, unit, value))

        return self._cost_of(totals, counts), self._broken(counts)

    def _teams_reordered(self, changed: Sequence[int], old_fields: list[_Fields]) -> set[str]:
        """Teams whose home/away sequence may differ from before the move: a
        different match order or membership, or a match flipped. A date shift
        inside a slot leaves it alone."""
        live = self.live
        moved: set[str] = set()
        for fields_before, i in zip(old_fields, changed, strict=True):
            fields_after = live._fields[i]
            for team in (fields_before[0], fields_before[1], fields_after[0], fields_after[1]):
                if (fields_before[0] == team) != (fields_after[0] == team):
                    moved.add(team)
        for store, team, before in live._log:
            if store is not live.by_team or team in moved:
                continue
            after = live.by_team.get(team, ())
            before = () if before is _MISSING else before
            if len(before) != len(after) or any(a is not b for a, b in zip(before, after, strict=True)):
                moved.add(team)
        return moved

    def _broken(self, counts: list[int]) -> tuple[str, ...]:
        return tuple(self.constraints[ci].id for ci in self._hard if counts[ci] > 0)

    def accept(self) -> None:
        if self._pending is None:
            return
        pending_cache, totals, counts = self._pending
        for ci, unit, value in pending_cache:
            scope = self._scopes[ci]
            if value is None:
                for member in unit:
                    self._store(ci, scope, member, self._unit_value(self.constraints[ci], scope, member))
            else:
                self._store(ci, scope, unit, value)
        self._total, self._count = totals, counts
        self.cost = self._cost_of(totals, counts)
        self._pending = None

    def _store(self, ci: int, scope: str, unit, value: tuple[float, int]) -> None:
        if value == (0.0, 0) and scope == "venue_date":
            self._cache[ci].pop(unit, None)
        else:
            self._cache[ci][unit] = value

    def reject(self) -> None:
        """Call after the matches have been restored."""
        self.live.undo()
        self._pending = None
