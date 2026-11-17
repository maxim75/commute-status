"""Following the train you are on: where it is, and when it gets in.

The departure board carries no trip ID, so the train is followed two ways:
  * the trip planner, asked for the same journey, gives the stop list and a
    live arrival time;
  * the Sydney Trains GPS feed gives where it physically is. The vehicle is
    picked once, as the city-bound train on this line nearest to where the
    timetable says ours should be, then followed by its vehicle ID.
If the GPS feed has nothing fresh, position falls back to the timetable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import short
from .tfnsw import Journey, LatLon, parse_time

GPS_STALE = timedelta(minutes=3)
AT_STATION_M = 250  # within this of a station counts as "at" it

# Route-name prefix -> GTFS route_id prefix in the GPS feed. In these IDs the
# digit after the underscore is the direction; "2" was city-bound on every T4
# train checked against movement south of Town Hall.
_FEED_PREFIX = {"T4": "ESI_", "South Coast": "SCO_"}
CITY_BOUND = "2"


@dataclass
class Trip:
    route: str
    line: str
    picked: datetime  # departure time the user clicked, to find it again
    departure: datetime
    arrival: datetime
    live: bool
    stops: list[str]
    coords: list[LatLon | None] = field(default_factory=list)
    vehicle_id: str | None = None
    pos: float | None = None  # index along stops; 2.5 = halfway from stop 2 to 3
    off_m: float | None = None
    pos_time: datetime | None = None
    pos_source: str = "timetable"
    planned: bool = False  # arrival/stops filled in from the trip planner

    @property
    def destination(self) -> str:
        return short(self.stops[-1]) if self.stops else ""

    def departed(self, now: datetime) -> bool:
        return now >= self.departure

    def arrived(self, now: datetime) -> bool:
        if not self.planned:
            return False
        at_end = self.pos is not None and self.pos >= len(self.stops) - 1.02
        return now >= self.arrival or at_end

    def update_from(self, journey: Journey) -> None:
        self.departure, self.arrival, self.live = journey.departure, journey.arrival, journey.live
        if journey.stops:
            self.stops = journey.stops
        self.planned = True

    def where(self) -> tuple[str, str | None]:
        """('at'|'next', station name) for the menu bar."""
        if self.pos is None or not self.stops:
            return "next", None
        i = round(self.pos)
        if abs(self.pos - i) < 0.08 and (self.off_m is None or self.off_m < AT_STATION_M):
            return "at", short(self.stops[i])
        return "next", short(self.stops[min(math.ceil(self.pos), len(self.stops) - 1)])

    def between(self) -> str | None:
        if self.pos is None or not self.stops:
            return None
        i = min(int(self.pos), len(self.stops) - 2)
        return f"{short(self.stops[i])} → {short(self.stops[i + 1])}"


def best_journey(trip: Trip, journeys: list[Journey]) -> Journey | None:
    """The planner journey that is the train the user picked."""
    same = [j for j in journeys if j.route == trip.route and j.changes == 0]
    if not same:
        return None
    j = min(same, key=lambda j: abs((j.departure - trip.departure).total_seconds()))
    return j if abs((j.departure - trip.departure).total_seconds()) <= 15 * 60 else None


# --- geometry --------------------------------------------------------------


def _xy(p: LatLon) -> tuple[float, float]:  # local metres; fine across Sydney
    return p[1] * 92_300, p[0] * 111_000


def project(coords: list[LatLon | None], point: LatLon) -> tuple[float, float] | None:
    """(position along the stops, metres off the line) for a GPS point."""
    best = None
    px, py = _xy(point)
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        if a is None or b is None:
            continue
        (ax, ay), (bx, by) = _xy(a), _xy(b)
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
        off = math.hypot(px - ax - t * dx, py - ay - t * dy)
        if best is None or off < best[1]:
            best = (i + t, off)
    return best


def timetable_pos(trip: Trip, now: datetime) -> float:
    span = (trip.arrival - trip.departure).total_seconds()
    if span <= 0:
        return 0.0
    frac = max(0.0, min(1.0, (now - trip.departure).total_seconds() / span))
    return frac * (len(trip.stops) - 1)


def locate(trip: Trip, vehicles: list[dict], now: datetime) -> None:
    """Update trip.pos from the GPS feed, or from the timetable if it can't."""
    expected = timetable_pos(trip, now)
    prefix = next((p for k, p in _FEED_PREFIX.items() if k in trip.route), None)

    def placed(v: dict) -> tuple[float, float] | None:
        return project(trip.coords, (v["latitude"], v["longitude"]))

    chosen = None
    if trip.vehicle_id:
        chosen = next((v for v in vehicles if v.get("vehicle_id") == trip.vehicle_id), None)
        if chosen:
            p = placed(chosen)
            # Went backwards or left the line: it was the wrong train.
            if p is None or p[1] > 1500 or (trip.pos is not None and p[0] < trip.pos - 0.5):
                trip.vehicle_id, chosen = None, None

    if chosen is None:
        candidates = []
        for v in vehicles:
            rid = v.get("route_id") or ""
            if prefix and not (rid.startswith(prefix) and rid[len(prefix):][:1] == CITY_BOUND):
                continue
            p = placed(v)
            if p and p[1] < 800 and abs(p[0] - expected) < 3:
                candidates.append((abs(p[0] - expected), v))
        if candidates:
            chosen = min(candidates, key=lambda c: c[0])[1]
            trip.vehicle_id = chosen.get("vehicle_id")

    if chosen is not None:
        seen = parse_time(chosen.get("timestamp")) or now
        if now - seen <= GPS_STALE:
            pos, off = placed(chosen)
            # Never show the train going backwards from GPS jitter.
            trip.pos = max(pos, trip.pos or 0.0) if trip.pos_source == "gps" else pos
            trip.off_m, trip.pos_time, trip.pos_source = off, seen, "gps"
            return

    trip.pos, trip.off_m, trip.pos_time, trip.pos_source = expected, None, now, "timetable"
