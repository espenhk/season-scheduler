"""Reader-facing formatting shared by warning builders, so a note says
"Sat 22 Aug" and a team's name rather than an ISO date and a raw id."""

from __future__ import annotations

from datetime import date


def short_date(day: date, season_year: int | None = None) -> str:
    """"Sat 22 Aug"; the year is added when it differs from `season_year`, as
    for a cup round that runs into the next calendar year."""
    text = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
    if season_year is not None and day.year != season_year:
        text += f" {day.year}"
    return text
