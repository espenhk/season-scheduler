"""The data the report page's client-side renderer draws from.

Every fixture is encoded once, here, and the page builds each view from that
list in the browser (`templates/report.js`) instead of the server repeating the
same fixture as markup per view per option (#42).

Fixtures share one id space per option, `uid`: cup and European entries (the
same in every option) come first, then the option's own league matches. Every
view refers to fixtures by `uid` only.

Wire shapes, one per fixture kind (short keys: league matches dominate the size):

    league    {d, t?, c, h, a, v?, p?}   date, kickoff, competition, home/away team,
                                         venue if not the home team's own ground,
                                         p=1 for a back-to-back-home-day fixture
    cup       {k: "cup", d, c, n, t, vt, vl}
    european  {k: "eu",  d, c, n, t, vt, vl, o}
              round name, team, venue type (home/away/neutral/unknown), venue label,
              opponent
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..model.loader import World


def css_id(raw: str) -> str:
    """An id made safe to use inside a CSS class name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)


def club_class(club_id: str) -> str:
    return f"k-{css_id(club_id)}"


def competition_class(competition_id: str) -> str:
    return f"c-{css_id(competition_id)}"


def json_for_script(value: Any) -> str:
    """JSON safe to inline in a `<script>` element.

    `<`, `>` and `&` are written as unicode escapes, so no data string can
    close the element (`</script>`) or open a comment (`<!--`); U+2028/2029
    are escaped for older JS engines. The escapes are plain JSON, so
    `JSON.parse` reads the original text back.
    """
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    for char, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(char, escape)
    return text


def palette_css(club_colors: dict[str, str], competitions: dict[str, dict]) -> str:
    """One rule per club and competition setting its colour custom properties;
    the stylesheet's marker/tag/dot rules read them, so no element carries an
    inline colour."""
    rules = [
        f".{club_class(club_id)}{{--club:{color}}}"
        for club_id, color in sorted(club_colors.items())
        if color
    ]
    rules += [
        f".{competition_class(cid)}{{--comp-fg:{c['fg']};--comp-bg:{c['bg']}}}"
        for cid, c in sorted(competitions.items())
    ]
    return "\n".join(rules)


def competition_table(entries: list[dict], extra: list[dict] | None = None) -> dict[str, dict]:
    """Display data per competition id, collected from the entries (and any
    `extra` rows, e.g. the calendar legend) so a competition the world does
    not declare still gets its colours."""
    table: dict[str, dict] = {}
    for entry in entries:
        table.setdefault(
            entry["competition_id"],
            {
                "name": entry["competition_name"],
                "short": entry["competition_short"],
                "fg": entry["comp_fg"],
                "bg": entry["comp_bg"],
            },
        )
    for row in extra or []:
        table.setdefault(
            row["id"],
            {"name": row["name"], "short": row["short"], "fg": row["color"], "bg": row["bg"]},
        )
    return table


def team_table(world: World) -> dict[str, dict]:
    return {
        team.id: {
            "label": world.team_label(team.id),
            "short": world.team_short_label(team.id),
            "club": team.club_id,
            "venue": team.home_venue,
        }
        for team in world.teams.values()
    }


def shared_tables(world: World, club_colors: dict[str, str], competitions: dict[str, dict]) -> dict:
    return {
        "clubs": {club_id: club_class(club_id) for club_id in club_colors},
        "competitions": {
            cid: {"name": c["name"], "short": c["short"], "cls": competition_class(cid)}
            for cid, c in competitions.items()
        },
        "teams": team_table(world),
        "venues": {venue.id: venue.name for venue in world.venues.values()},
    }


def shared_entry_wire(entry: dict) -> dict:
    """A cup or European entry, which every option shares."""
    wire = {
        "k": "eu" if entry.get("is_european") else "cup",
        "d": entry["date"].isoformat(),
        "c": entry["competition_id"],
        "n": entry["round_name"],
        "t": entry["team_id"],
        "vt": entry["venue_type"],
        "vl": entry["venue_label"],
    }
    if entry.get("is_european"):
        wire["o"] = entry["opponent"]
    return wire


def league_entry_wire(entry: dict, teams: dict[str, dict]) -> dict:
    wire: dict[str, Any] = {
        "d": entry["date"].isoformat(),
        "c": entry["competition_id"],
        "h": entry["home_team"],
        "a": entry["away_team"],
    }
    if entry.get("kickoff_time"):
        wire["t"] = entry["kickoff_time"]
    if entry["venue_id"] != teams[entry["home_team"]]["venue"]:
        wire["v"] = entry["venue_id"]
    if entry["paired"]:
        wire["p"] = 1
    return wire


def _uids(entries: list[dict]) -> list[int]:
    return [entry["uid"] for entry in entries]


def by_competition_wire(views: list[dict]) -> list[dict]:
    """The "By competition" view: each competition's rounds as heading text
    plus the `uid`s of the fixtures in it."""
    wire = []
    for view in views:
        wire.append(
            {
                "id": view["id"],
                "name": view["name"],
                "meta": view["meta"],
                "rounds": [
                    {
                        "title": round_["title"],
                        "text": round_["text"],
                        **({"note": round_["note"]} if round_.get("note") else {}),
                        "ids": _uids(round_["fixtures"] if "fixtures" in round_ else round_["matches"]),
                    }
                    for round_ in view["rounds"]
                ],
            }
        )
    return wire


def option_wire(
    *,
    entries: list[dict],
    teams: dict[str, dict],
    headlines: list[dict],
    club_headlines: dict[str, list[dict]],
    league_views: list[dict],
    combined_calendar: list[dict],
    combined_list: list[dict],
    month_calendar: list[dict],
    month_calendar_details: list[dict],
) -> dict:
    """Everything one option's views need on top of the shared tables and
    shared entries."""
    return {
        "matches": [league_entry_wire(entry, teams) for entry in entries],
        "headlines": headlines,
        "clubHeadlines": club_headlines,
        "byCompetition": by_competition_wire(league_views),
        "weeks": [
            {"label": week["label"], "dates": week["dates"], "ids": _uids(week["matches"])}
            for week in combined_calendar
        ],
        "list": _uids(combined_list),
        "months": [
            {
                "label": month["label"],
                "key": month["key"],
                "weeks": month["weeks"],
            }
            for month in month_calendar
        ],
        "days": [
            {
                "date": day["date_iso"],
                "label": day["label"],
                "ids": _uids(day["matches"]),
                "comps": day["competition_ids"],
            }
            for day in month_calendar_details
        ],
    }
