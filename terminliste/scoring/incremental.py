"""Incremental scoring for local search (#11).

A move touches a handful of matches, yet a full evaluation re-scores the whole
season. Most rules are sums over independent units — a team's match list, a
venue-day, a club, one match — so a move only changes the units it touches.

A constraint opts in by declaring `delta_scope`, the unit its total decomposes
over: evaluating it on a view holding just one unit returns exactly that
unit's contribution. `DeltaScorer` caches each unit's contribution, re-scores
only the touched units per move, and keeps the schedule index up to date
incrementally. A constraint without a scope is re-evaluated in full every
move, so a rule that cannot decompose stays correct and merely costs more.
A wrong scope is only caught by `tests/scoring/test_incremental.py`, which
replays random moves against full evaluation — run it when adding a rule.

Scopes:
    team         by_team / date-set views of the given teams
    club         the home/away date views of one dual club's teams
    venue_date   one (venue, date) cell
    match        a single match (its by_team view holds just that match)
    competition  all matches of one competition
    round        all matches of one competition round
    kickoff      depends on kickoff times, which search never changes; constant
                 while every kickoff is unset, otherwise evaluated in full
    contingency  reads the fallback plan; `ContingencyTracker` keeps that plan
                 current, replanning only when a move can change it
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date

from ..contingency import (
    ContingencyAnalysis,
    ContingencyPlanner,
    PlanFootprint,
    ReasonCache,
)
from .base import HARD_PENALTY, Constraint, EvalContext, ScheduleIndex

_Fields = tuple[str, str, str, date]
_ZERO = (0.0, 0)
_MISSING = object()
_EMPTY: dict = {}


def _fields_of(match) -> _Fields:
    return (match.home_team, match.away_team, match.venue, match.date)


class LiveIndex(ScheduleIndex):
    """A `ScheduleIndex` that follows in-place match mutation.

    Same contents as a freshly built `ScheduleIndex` (lists inside one cell may
    be ordered differently, which no rule reads), so a rule evaluated in full
    against it sees what it would see at rest.
    """

    def __init__(self, matches: Sequence) -> None:
        self._avbd: dict | None = None
        super().__init__(matches)  # type: ignore[arg-type]
        self._fields: list[_Fields] = [_fields_of(m) for m in matches]
        self.team_idx: dict[str, set[int]] = {}
        for i, match in enumerate(matches):
            self.team_idx.setdefault(match.home_team, set()).add(i)
            self.team_idx.setdefault(match.away_team, set()).add(i)
        # Per team, so one edit per rebuild.
        self.away_venues: dict[str, dict[date, str]] = {}
        for match in matches:
            self.away_venues.setdefault(match.away_team, {})[match.date] = match.venue
        # Leg is fixed per match, so these partitions never change.
        self.by_leg: dict[tuple[str, int], list] = {}
        for match in matches:
            self.by_leg.setdefault((match.competition_id, match.leg), []).append(match)
        self._log: list[tuple] = []

    @property
    def away_venue_by_team_date(self) -> dict:
        """Built on demand: only a rule scored in full against this index reads
        it, the scoped ones get `away_venues` through their view."""
        if "away_venues" not in self.__dict__:
            return {}  # `ScheduleIndex.__init__` fills it while building
        if self._avbd is None:
            self._avbd = {
                (team, day): venue
                for team, venues in self.away_venues.items()
                for day, venue in venues.items()
            }
        return self._avbd

    @away_venue_by_team_date.setter
    def away_venue_by_team_date(self, value) -> None:
        pass  # `ScheduleIndex.__init__` assigns it; this class derives it instead

    def sync(self, changed: Iterable[int]) -> None:
        """Bring the index in line with the changed matches' current fields.
        The edits are logged so `undo` can put the index back without
        recomputing anything."""
        matches = self.matches
        self._log = log = []
        self._avbd = None
        touched: set[str] = set()
        for i in changed:
            match = matches[i]
            old = self._fields[i]
            new = _fields_of(match)
            if old == new:
                continue
            old_home, old_away, old_venue, old_day = old
            if (old_venue, old_day) != (new[2], new[3]):
                self._edit(
                    self.by_venue_date,
                    (old_venue, old_day),
                    _without(self.by_venue_date.get((old_venue, old_day), []), match),
                )
                self._edit(
                    self.by_venue_date,
                    (new[2], new[3]),
                    [*self.by_venue_date.get((new[2], new[3]), []), match],
                )
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
        self._avbd = None
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
        away_matches = [m for m in mine if m.home_team != team]
        self._edit(self.away_venues, team, {m.date: m.venue for m in away_matches})
        self._edit(self.home_dates_by_team, team, {m.date for m in mine if m.home_team == team})
        self._edit(self.away_dates_by_team, team, {m.date for m in away_matches})


def _without(matches: list, match) -> list:
    return [m for m in matches if m is not match]


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
        "by_leg",
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
        by_leg=_EMPTY,
    ) -> None:
        self.matches = matches
        self.by_team = by_team
        self.by_venue_date = by_venue_date
        self.by_date = _EMPTY
        self.by_competition = by_competition
        self.home_dates_by_team = home_dates_by_team
        self.away_dates_by_team = away_dates_by_team
        self.away_venue_by_team_date = away_venue_by_team_date
        self.by_leg = by_leg

    def team_matches(self, team_id: str) -> list:
        return self.by_team.get(team_id, [])


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


class ContingencyTracker:
    """The fallback plan of a mutable schedule, replanned only when a move can
    change it.

    The plan is a pure function of (a) the exposed matches — those a contingent
    commitment sits too close to — and (b) what `ContingencyPlanner.footprint`
    says it reads of the rest of the schedule. A move that touches no exposed
    match, before or after, and no part of that footprint leaves the plan
    exactly as it was, so the cached analysis stands. Anything else replans from
    the cached exposure list, which skips the scan over every match, and
    re-derives only the validity of the fallback dates whose own footprint the
    move touched (`ReasonCache`).
    """

    def __init__(self, planner: ContingencyPlanner, live: LiveIndex) -> None:
        self.planner = planner
        self.live = live
        matches = live.matches
        self._triggers: list[tuple[str, ...]] = [planner.triggers_of(m) for m in matches]
        self._exposed: set[int] = {i for i, t in enumerate(self._triggers) if t}
        self._memo: dict[int, dict] = {}
        self._xfp: dict[int, PlanFootprint] = {}
        self._bounds = planner.leg_bounds_of(live)
        self.analysis = self._replan(self._exposed, self._triggers, {}, ReasonCache(self._memo))
        self._pending: tuple | None = None
        self._legs: set[tuple[str, int]] | None = None
        # Proposals that needed a new plan, for the benchmark and its test.
        self.replans = 0
        self._build_footprint(set(self._exposed))

    def _replan(
        self,
        exposed_idx: set[int],
        triggers: list[tuple[str, ...]],
        changed_triggers: dict[int, tuple[str, ...]],
        cache: ReasonCache,
        changed_legs: set[tuple[str, int]] | None = None,
    ) -> ContingencyAnalysis:
        matches = self.live.matches
        exposed = [(matches[i], changed_triggers.get(i, triggers[i])) for i in exposed_idx]
        exposed.sort(key=lambda item: (item[0].date, item[0].key))
        bounds = self._bounds_with(changed_legs)
        plan = self.planner.plan(self.live, exposed, bounds, cache)
        analysis = self.planner.check(self.live, plan, bounds, cache)
        analysis.triggers = {m.key: t for m, t in exposed}
        analysis.exposed = exposed
        return analysis

    def _bounds_with(
        self, legs: set[tuple[str, int]] | None
    ) -> dict[str, dict[int, tuple[date, date]]]:
        """The first and last day of every leg as the live matches stand now:
        the committed ones, with just the legs a move touched recomputed.
        `legs` None recomputes everything."""
        if legs is None:
            return self.planner.leg_bounds_of(self.live)
        bounds = dict(self._bounds)
        for competition_id, leg in legs:
            per_leg = dict(bounds.get(competition_id, ()))
            dates = [m.date for m in self.live.by_leg.get((competition_id, leg), ())]
            if dates:
                per_leg[leg] = (min(dates), max(dates))
            else:
                per_leg.pop(leg, None)
            bounds[competition_id] = per_leg
        return bounds

    def _build_footprint(self, rebuilt: set[int]) -> None:
        """Always from the committed state: `_edges` are what a proposal's leg
        extremes are compared against. `rebuilt` are the exposed matches whose
        own footprint is new."""
        matches = self.live.matches
        current = {id(matches[i]): i for i in self._exposed}
        xfp = {}
        for mid, i in current.items():
            known = self._xfp.get(mid)
            if known is None or i in rebuilt:
                known = self.planner.footprint([(matches[i], self._triggers[i])])
            xfp[mid] = known
        self._xfp = xfp
        for mid in [mid for mid in self._memo if mid not in current]:
            del self._memo[mid]
        self._footprint: PlanFootprint = PlanFootprint.union(list(xfp.values()))
        self._edges = {edge: self._edge_value(edge) for edge in self._footprint.leg_edges}

    def _edge_value(self, edge: tuple[str, int, str]) -> date | None:
        competition_id, leg, which = edge
        leg_matches = self.live.by_leg.get((competition_id, leg))
        if not leg_matches:
            return None
        dates = [m.date for m in leg_matches]
        return max(dates) if which == "end" else min(dates)

    def propose(self, changed: Sequence[int], old_fields: list[_Fields]) -> ContingencyAnalysis:
        """The analysis of the live index as it now stands (the matches are
        already synced); `old_fields` are the changed matches' fields before."""
        live = self.live
        matches = live.matches
        planner = self.planner
        new_triggers = {i: planner.triggers_of(matches[i]) for i in changed}
        # Leg first/last days are kept for every leg, whether or not this move
        # needs a new plan, so a later plan never reads a stale one.
        legs = {(matches[i].competition_id, matches[i].leg) for i in changed}
        self._legs = legs

        exposed_changed = any(self._triggers[i] or new_triggers[i] for i in changed)
        touched = self._touched(changed, old_fields)
        if not exposed_changed and not self._hits(self._footprint, touched):
            self._pending = None
            return self.analysis

        self.replans += 1
        exposed_idx = set(self._exposed)
        for i, triggers in new_triggers.items():
            if triggers:
                exposed_idx.add(i)
            else:
                exposed_idx.discard(i)
        # A match is stale when the move touched its own footprint: what was
        # worked out for it before no longer holds.
        stale = {id(matches[i]) for i in changed}
        stale.update(mid for mid, fp in self._xfp.items() if self._hits(fp, touched))
        cache = ReasonCache(self._memo, frozenset(stale))
        analysis = self._replan(exposed_idx, self._triggers, new_triggers, cache, legs)
        self._pending = (new_triggers, exposed_idx, analysis, exposed_changed, cache)
        return analysis

    def _touched(
        self, changed: Sequence[int], old_fields: list[_Fields]
    ) -> tuple[list[_Fields], set[str], set[tuple[str, int, str]]]:
        """What the move touched: the (home, away, venue, day) of every changed
        match before and after, their keys, and the footprint leg edges whose
        first or last day moved."""
        live = self.live
        matches = live.matches
        states: list[_Fields] = []
        keys: set[str] = set()
        legs: set[tuple[str, int]] = set()
        for i, before in zip(changed, old_fields, strict=True):
            match = matches[i]
            states.append(before)
            states.append(live._fields[i])
            keys.add(match.key)
            keys.add(_key_of(before, match))
            legs.add((match.competition_id, match.leg))
        moved = {
            edge
            for edge, value in self._edges.items()
            if edge[:2] in legs and self._edge_value(edge) != value
        }
        return states, keys, moved

    @staticmethod
    def _hits(footprint: PlanFootprint, touched) -> bool:
        states, keys, moved_edges = touched
        team_days = footprint.team_days
        venue_days = footprint.venue_days
        home_days = footprint.home_days
        for home, away, venue, day in states:
            if (venue, day) in venue_days or (home, day) in home_days:
                return True
            days = team_days.get(home)
            if days is not None and day in days:
                return True
            days = team_days.get(away)
            if days is not None and day in days:
                return True
        return (
            not keys.isdisjoint(footprint.keys)
            or not moved_edges.isdisjoint(footprint.leg_edges)
        )

    def accept(self) -> None:
        if self._legs:
            self._bounds = self._bounds_with(self._legs)
            self._legs = None
        if self._pending is None:
            return
        new_triggers, exposed_idx, analysis, exposed_changed, cache = self._pending
        for i, triggers in new_triggers.items():
            self._triggers[i] = triggers
        self._exposed = exposed_idx
        self.analysis = analysis
        self._memo.update(cache.scratch)
        self._build_footprint(set(new_triggers))
        self._pending = None

    def reject(self) -> None:
        self._pending = None
        self._legs = None


def _key_of(fields: _Fields, match) -> str:
    """`match.key` as it was with `fields` (the key embeds the orientation)."""
    return f"{match.competition_id}:{match.round_index}:{fields[0]}-{fields[1]}"


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
        planners = {
            id(c.planner): c.planner
            for c in constraints
            if getattr(c, "delta_scope", None) == "contingency" and hasattr(c, "planner")
        }
        # Rules over two different planners cannot share one tracker; they are
        # scored in full instead.
        self._planner = next(iter(planners.values())) if len(planners) == 1 else None
        self.tracker: ContingencyTracker | None = None

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
            elif scope == "contingency" and self._planner is None:
                scope = None
            self._scopes.append(scope)

        self.tracker = (
            ContingencyTracker(self._planner, self.live) if self._planner is not None else None
        )
        if self.tracker is not None:
            self.live.contingency = (self._planner, self.tracker.analysis)

        self._cache: list[dict] = [{} for _ in self.constraints]
        if not hasattr(self, "_pure"):
            # A match-scoped rule reads nothing but the match's own fields, so
            # its value is looked up by them: the search revisits the same few
            # dates of each fixture constantly.
            self._pure: list[dict] = [{} for _ in self.constraints]
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
        # Hard rules first so a move that breaks one can skip the soft ones.
        self._hard_steps = [self._step(ci) for ci in self._hard]
        self._soft_steps = [
            self._step(ci) for ci, c in enumerate(self.constraints) if c.kind != "hard"
        ]

        for ci, constraint in enumerate(self.constraints):
            scope = self._scopes[ci]
            if scope in (None, "static", "contingency"):
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
        self._pending_cost = self.cost

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
            legs = {k: v for k, v in live.by_leg.items() if k[0] in units}
            return (
                _View(
                    [m for ms in grouped.values() for m in ms],
                    by_competition=grouped,
                    by_leg=legs,
                ),
                self.ctx,
            )
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

    def _step(self, ci: int) -> tuple:
        """What `propose` needs per rule, looked up once: (index, rule, scope,
        sign of its cost, cached unit values, match-field memo, order-only)."""
        constraint = self.constraints[ci]
        return (
            ci,
            constraint,
            self._scopes[ci],
            HARD_PENALTY if constraint.kind == "hard" else -1.0,
            self._cache[ci],
            self._pure[ci],
            self._order_only[ci],
        )

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
        changed = list(dict.fromkeys(changed))
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
        units: dict[str, object] = {"team": teams, "venue_date": venue_dates}
        if "club" in used:
            units["club"] = {self._club_of[t] for t in teams if t in self._club_of}
        if "competition" in used:
            units["competition"] = {matches[i].competition_id for i in changed}
        if "round" in used:
            units["round"] = {(matches[i].competition_id, matches[i].round_index) for i in changed}
        if self._has_order_only:
            units["team_order"] = self._teams_reordered(changed, old_fields)

        analysis_moved = False
        if self.tracker is not None:
            analysis = self.tracker.propose(changed, old_fields)
            analysis_moved = analysis is not self.tracker.analysis
            live.contingency = (self._planner, analysis)

        pending_cache: list[tuple[int, tuple | None, dict | None]] = []
        totals = list(self._total)
        counts = list(self._count)
        views: dict[tuple[str, bool], tuple[_View, EvalContext]] = {}
        run = (changed, units, views, totals, counts, pending_cache, analysis_moved)
        self._pending = (pending_cache, totals, counts)
        cost = self.cost + self._score(self._hard_steps, run)
        if prune and cost >= self.cost + HARD_PENALTY / 2:
            self._pending = None
            return float("inf"), self._broken(counts)
        cost += self._score(self._soft_steps, run)
        self._pending_cost = cost
        return cost, self._broken(counts)

    def _score(self, steps: list[tuple], run: tuple) -> float:
        """Re-score `steps` for the move in `run`, updating its totals and
        counts; returns the change in search cost."""
        changed, units, views, totals, counts, pending_cache, analysis_moved = run
        matches = self.matches
        live = self.live
        ctx = self.ctx
        delta = 0.0
        for ci, constraint, scope, sign, cache, pure, order_only in steps:
            total_before, count_before = totals[ci], counts[ci]
            if scope == "static":
                continue
            if scope == "contingency":
                if not analysis_moved:
                    continue
                result = constraint.evaluate(live, ctx)
                totals[ci], counts[ci] = result.total, result.count
            elif scope is None:
                result = constraint.evaluate(live, ctx)
                totals[ci], counts[ci] = result.total, result.count
            elif scope == "match":
                values = {}
                for i in changed:
                    m = matches[i]
                    fields = (
                        m.competition_id, m.leg, m.round_index, m.home_team, m.away_team,
                        m.venue, m.date, m.kickoff_time,
                    )
                    value = pure.get(fields)
                    if value is None:
                        value = pure[fields] = self._unit_value(constraint, scope, i)
                    before = cache.get(i, _ZERO)
                    totals[ci] += value[0] - before[0]
                    counts[ci] += value[1] - before[1]
                    values[i] = value
                pending_cache.append((ci, None, values))
            else:
                touched = units["team_order" if order_only else scope]
                if not touched:
                    continue
                # The touched units are scored as one view, shared by the rules
                # of a scope; their individual values are only needed if the
                # move is kept, so `accept` recomputes them.
                shared = views.get((scope, order_only))
                if shared is None:
                    shared = views[(scope, order_only)] = self._view(scope, touched)
                result = constraint.evaluate(shared[0], shared[1])  # type: ignore[arg-type]
                for unit in touched:
                    before = cache.get(unit, _ZERO)
                    totals[ci] -= before[0]
                    counts[ci] -= before[1]
                totals[ci] += result.total
                counts[ci] += result.count
                pending_cache.append((ci, tuple(touched), None))
            if sign > 0:
                delta += (counts[ci] - count_before) * sign
            else:
                delta += (totals[ci] - total_before) * sign
        return delta

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
        for ci, units, values in pending_cache:
            scope = self._scopes[ci]
            if values is not None:
                self._cache[ci].update(values)
                continue
            for unit in units:
                self._store(ci, scope, unit, self._unit_value(self.constraints[ci], scope, unit))
        self._total, self._count = totals, counts
        self.cost = self._pending_cost
        if self.tracker is not None:
            self.tracker.accept()
        self._pending = None

    def _store(self, ci: int, scope: str, unit, value: tuple[float, int]) -> None:
        if value == (0.0, 0) and scope == "venue_date":
            self._cache[ci].pop(unit, None)
        else:
            self._cache[ci][unit] = value

    def reject(self) -> None:
        """Call after the matches have been restored."""
        self.live.undo()
        if self.tracker is not None:
            self.tracker.reject()
            self.live.contingency = (self._planner, self.tracker.analysis)
        self._pending = None
