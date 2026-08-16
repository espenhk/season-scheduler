"""A local JSON cache for fetched reference data, with a TTL.

Deliberately separate from `client.py`: the cache doesn't know or care what
shape the cached payload is, and the client doesn't know or care where it's
stored. `refresh.py` is the only thing that ties the two together.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TTL_S = 24 * 60 * 60  # a day — team/stadium facts don't change hourly


@dataclass(frozen=True)
class CacheEntry:
    fetched_at: float
    payload: Any

    def is_fresh(self, ttl_s: float) -> bool:
        return (time.time() - self.fetched_at) < ttl_s


def read(path: Path) -> CacheEntry | None:
    """The cached entry, or `None` if there is none or it's unreadable.

    A corrupt or half-written cache file is treated the same as a missing
    one — refresh from the API rather than raising, since the entire point
    of a cache is to be safe to throw away.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return CacheEntry(fetched_at=raw["fetched_at"], payload=raw["payload"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"fetched_at": time.time(), "payload": payload}
    path.write_text(json.dumps(entry, indent=2, default=str))
