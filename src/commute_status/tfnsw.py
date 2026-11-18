"""Calls to the tfnsw MCP server, shaped into small records."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from . import config

LatLon = tuple[float, float]


@dataclass
class Departure:
    time: datetime  # estimated if live, else planned
    route: str  # full route name, e.g. "T4 Eastern Suburbs & Illawarra Line"
    line: str  # short name, e.g. "T4"
    destination: str
    live: bool


@dataclass
class Journey:
    departure: datetime
    arrival: datetime
    changes: int
    route: str  # first vehicle leg's route name ("" for walk-only)
    stops: list[str]  # first vehicle leg's stop names, origin to destination
    live: bool
    towards: str = ""  # first vehicle leg's destination, e.g. "Bondi Junction via Wolli Creek"


@asynccontextmanager
async def session():
    if not config.TFNSW_API_KEY:
        raise RuntimeError("Set TFNSW_API_KEY")
    http = create_mcp_http_client(headers={"X-API-Key": config.TFNSW_API_KEY})
    async with http, streamable_http_client(config.MCP_URL, http_client=http) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def call(s: ClientSession, tool: str, args: dict) -> dict:
    result = await s.call_tool(tool, args)
    if result.is_error:
        raise RuntimeError(result.content[0].text if result.content else f"{tool} failed")
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=config.SYDNEY)


async def find_stop(s: ClientSession, name: str) -> tuple[str, LatLon | None]:
    """Stop ID and coordinates for a station name."""
    query = name if "station" in name.lower() else f"{name} Station"
    found = await call(s, "find_stop", {"query": query, "location_type": "stop", "limit": 1})
    stops = found.get("locations") or []
    if not stops:
        raise RuntimeError(f"No stop found for {query!r}")
    coord = stops[0].get("coord") or {}
    lat, lon = coord.get("latitude"), coord.get("longitude")
    return str(stops[0]["id"]), (lat, lon) if lat is not None and lon is not None else None


async def direct_trains(
    s: ClientSession, origin_id: str, dest_id: str, since: datetime, upcoming: int, now: datetime
) -> list[Journey]:
    """Direct trains from origin to destination departing from *since* on.

    The trip planner, unlike the departure board, only returns trains that
    actually stop at the destination, in either direction. It answers about
    four journeys at a time, so it is asked again from the last departure
    until *upcoming* trains leaving after *now* are known.
    """
    found: dict[tuple[datetime, str], Journey] = {}
    when = since
    for _ in range(4):
        journeys = await plan(s, origin_id, dest_id, when=when, n=8)
        for j in journeys:
            if j.changes == 0 and j.route:
                found[(j.departure, j.route)] = j
        if not journeys or sum(j.departure >= now for j in found.values()) >= upcoming:
            break
        when = max(j.departure for j in journeys) + timedelta(minutes=1)
    return sorted(found.values(), key=lambda j: j.departure)


def _journeys(data: dict) -> list[Journey]:
    out = []
    for j in data.get("journeys", []):
        dep, arr = parse_time(j.get("departure")), parse_time(j.get("arrival"))
        if dep is None or arr is None:
            continue
        rides = [leg for leg in j.get("legs", []) if leg.get("route")]
        first = rides[0] if rides else {}
        out.append(
            Journey(
                departure=dep,
                arrival=arr,
                changes=j.get("changes", 0),
                route=first.get("route", ""),
                stops=first.get("stops", []),
                live=bool(first.get("realtime")),
                towards=first.get("towards", ""),
            )
        )
    return out


async def plan(
    s: ClientSession, origin_id: str, destination_id: str, when: datetime | None = None, n: int = 3
) -> list[Journey]:
    args = {"origin_id": origin_id, "destination_id": destination_id, "detail": "stops", "max_results": n}
    if when is not None:
        args["when"] = when.astimezone(config.SYDNEY).isoformat(timespec="minutes")
    return _journeys(await call(s, "plan_trip", args))


async def train_positions(s: ClientSession) -> list[dict]:
    data = await call(s, "get_vehicle_positions", {"mode": "sydneytrains", "max_results": 5000})
    return data.get("vehicles", [])
