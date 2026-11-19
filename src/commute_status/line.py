"""Where the trains on the departure list are right now.

The line is mapped once as a list of stations from a start point upstream of
the home station (by default the terminus trains come from, e.g. Waterfall for
Engadine -> Town Hall) through the home station to the destination, with
coordinates. Each listed train gets a timetable guess of
where it should be on that line; city-bound GPS vehicles are then assigned to
the closest guesses, one vehicle per train.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import tfnsw, travel
from .config import short
from .tfnsw import LatLon

MAX_GUESS_ERROR = 3.0  # stops between timetable guess and GPS for a match
MAX_OFF_LINE_M = 800
LOOKUPS_AT_ONCE = 3  # TfNSW allows about 5 requests a second per key
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "commute-status" / "line.json"


@dataclass
class Line:
    stops: list[str]  # short names, upstream start ... home ... destination
    coords: list[LatLon | None]
    home: int  # index of the home station
    dest: int  # index of the destination
    per_stop_min: float = 2.0  # typical running time between stops

    def guess_upcoming(self, minutes_away: float) -> float:
        """Position of a train due at the home station in *minutes_away*."""
        return self.home - minutes_away / self.per_stop_min

    def guess_departed(self, dep: datetime, arr: datetime, now: datetime) -> float:
        """Position of a train that left the home station at *dep*."""
        span = (arr - dep).total_seconds()
        frac = 0.0 if span <= 0 else max(0.0, min(1.0, (now - dep).total_seconds() / span))
        return self.home + frac * (self.dest - self.home)

    def describe(self, pos: float, off_m: float | None) -> str:
        """'@Heathcote' when stopped there, '◑ Heathcote' when on the way to it.

        The pie shows roughly how far it is from the previous stop (◔ ◑ ◕).
        """
        i = round(pos)
        if abs(pos - i) < 0.08 and (off_m is None or off_m < travel.AT_STATION_M):
            return f"@{self.stops[i]}"
        return f"{travel.progress_icon(pos)} {self.stops[min(math.ceil(pos), len(self.stops) - 1)]}"


async def build(
    s, home_id: str, dest_id: str, line_start: str, coords_cache: dict[str, LatLon | None]
) -> Line:
    async def upstream() -> list[str]:
        start = line_start
        if start == "auto":
            start = await terminus_beyond(s, home_id, dest_id)
        if not start:
            return []
        start_id, _ = await tfnsw.find_stop(s, start)
        if start_id == home_id:
            return []
        up = await tfnsw.plan(s, start_id, home_id, n=3)
        up = next((j for j in up if j.changes == 0 and j.stops), None)
        return [short(n) for n in up.stops][:-1] if up else []  # last one is the home station

    down, up_stops = await asyncio.gather(tfnsw.plan(s, home_id, dest_id, n=3), upstream())
    down = next((j for j in down if j.changes == 0 and j.stops), None)
    if down is None:
        raise RuntimeError("Couldn't map the line to the destination")
    down_stops = [short(n) for n in down.stops]

    stops = up_stops + down_stops
    await lookup_coords(s, stops, coords_cache)
    run_min = (down.arrival - down.departure).total_seconds() / 60
    per_stop = run_min / max(1, len(down_stops) - 1)
    return Line(
        stops=stops,
        coords=[coords_cache.get(n) for n in stops],
        home=len(up_stops),
        dest=len(stops) - 1,
        per_stop_min=per_stop if per_stop > 0 else 2.0,
    )


async def terminus_beyond(s, home_id: str, dest_id: str) -> str | None:
    """Where trains come from before reaching the home station.

    Trains running the other way (destination -> home) carry on past the home
    station to a terminus, e.g. Town Hall -> Engadine trains run "to Waterfall
    via Banksia". The most common such terminus is where this direction's
    trains start, so the line is mapped from there.
    """
    back = await tfnsw.plan(s, dest_id, home_id, n=8)
    ends = [j.towards.split(" via ")[0].strip() for j in back if j.changes == 0 and j.towards]
    return Counter(ends).most_common(1)[0][0] if ends else None


async def lookup_coords(s, names: list[str], coords_cache: dict[str, LatLon | None]) -> None:
    """Fill in coordinates for station short names not cached yet, a few at a time."""
    missing = [n for n in dict.fromkeys(names) if n not in coords_cache]
    gate = asyncio.Semaphore(LOOKUPS_AT_ONCE)

    async def one(name: str):
        async with gate:
            return await tfnsw.find_stop(s, name)

    for name, (_, coords) in zip(missing, await asyncio.gather(*(one(n) for n in missing))):
        coords_cache[name] = coords


def match(
    line: Line, guesses: list[tuple[str, float]], vehicles: list[dict], now: datetime, feed_direction: str
) -> dict[int, tuple[float, float]]:
    """Assign GPS vehicles to trains.

    *guesses* is (route name, timetable position) per train. Returns
    {train index: (position, metres off the line)} for the trains matched.
    """
    placed = []
    for v in vehicles:
        if not travel.fresh(v, now):
            continue
        p = travel.project(line.coords, (v["latitude"], v["longitude"]))
        if p and p[1] < MAX_OFF_LINE_M:
            placed.append((v, p))

    pairs = []
    for ti, (route, guess) in enumerate(guesses):
        for vi, (v, (pos, off)) in enumerate(placed):
            if travel.same_direction(route, v, feed_direction) and abs(pos - guess) < MAX_GUESS_ERROR:
                pairs.append((abs(pos - guess), ti, vi))

    out: dict[int, tuple[float, float]] = {}
    used: set[int] = set()
    for _, ti, vi in sorted(pairs):
        if ti in out or vi in used:
            continue
        out[ti] = placed[vi][1]
        used.add(vi)
    return out


# --- cache: the line rarely changes, so map it once and keep it on disk -----


def _read_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data if isinstance(data, dict) and "key" not in data else {}  # old single-line format
    except (OSError, ValueError):
        return {}


def load_cached(key: str) -> Line | None:
    """The mapped line for a direction (config.Direction.key), if cached."""
    try:
        data = dict(_read_cache()[key])
        data["coords"] = [tuple(c) if c else None for c in data["coords"]]
        return Line(**data)
    except (KeyError, TypeError):
        return None


def save_cached(line_: Line, key: str) -> None:
    try:
        cache = _read_cache()
        cache[key] = asdict(line_)
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass  # only a speed-up
