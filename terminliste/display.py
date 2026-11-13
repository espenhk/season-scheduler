"""Reader-facing formatting shared by warning builders, so a note says
"Sat 22 Aug" and a team's name rather than an ISO date and a raw id."""

from __future__ import annotations

from datetime import date


def short_date(day: date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
