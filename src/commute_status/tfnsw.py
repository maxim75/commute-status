"""Calls to the tfnsw MCP server, shaped into small records."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

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


def goes_towards(dep: dict, place: str = config.TOWARDS) -> bool:
    """True if the service calls at *place* after this stop.

    Uses the onward stop list when the API provides it; otherwise the
    destination name (e.g. "Bondi Junction" trains run via Town Hall).
    """
    target = place.lower()
    onwards = dep.get("onwards_locations") or []
    if onwards:
        return target in json.dumps(onwards).lower()
    destination = ((dep.get("transportation") or {}).get("destination_name") or "").lower()
    return target in destination or "bondi junction" in destination


async def departures_towards(s: ClientSession, stop_id: str) -> list[Departure]:
    # concise=False: we need the destination to pick a direction.
    data = await call(s, "get_departures", {"stop_id": stop_id, "concise": False})
    out = []
    for dep in data.get("departures", []):
        est, planned = parse_time(dep.get("departure_estimated")), parse_time(dep.get("departure_planned"))
        if not (est or planned) or not goes_towards(dep):
            continue
        t = dep.get("transportation") or {}
        out.append(
            Departure(
                time=est or planned,
                route=t.get("number") or t.get("name") or "",
                line=t.get("disassembled_name") or t.get("number") or "Train",
                destination=t.get("destination_name") or "",
                live=est is not None,
            )
        )
    return sorted(out, key=lambda d: d.time)


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


async def plan_from(s: ClientSession, origin: LatLon, destination_id: str, n: int = 5) -> list[Journey]:
    data = await call(
        s,
        "plan_trip_from_coordinate",
        {
            "latitude": origin[0],
            "longitude": origin[1],
            "destination_id": destination_id,
            "detail": "answer",
            "max_results": n,
        },
    )
    return _journeys(data)


async def train_positions(s: ClientSession) -> list[dict]:
    data = await call(s, "get_vehicle_positions", {"mode": "sydneytrains", "max_results": 5000})
    return data.get("vehicles", [])
