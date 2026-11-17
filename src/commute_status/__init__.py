"""macOS menu bar countdown to the next train, via the tfnsw MCP server.

Where you are (from Home Assistant) picks what is shown:
  * at home        -> next train from Engadine towards Town Hall
  * in a named zone -> next trip from that zone back to Engadine
  * anywhere else  -> a placeholder, no countdown
Without Home Assistant configured it always behaves as if you are at home.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import rumps
from dotenv import find_dotenv, load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# .env from the current directory (or a parent), then the project root.
# Real environment variables win over both.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MCP_URL = os.environ.get("TFNSW_MCP_URL", "https://tfnsw-trip-planner.cool.imaxim.org/mcp")
STATION = os.environ.get("COMMUTE_STATION", "Engadine Station")
STOP_ID = os.environ.get("COMMUTE_STOP_ID")  # skip the name lookup if set
TOWARDS = os.environ.get("COMMUTE_TOWARDS", "Town Hall")  # only trains calling here later
def _hass_base_url(url: str) -> str:
    """Accept the REST or WebSocket form: ws://host:8123/api/websocket -> http://host:8123."""
    url = url.strip().rstrip("/").removesuffix("/api/websocket").removesuffix("/api")
    if url.startswith("ws://"):
        return "http://" + url.removeprefix("ws://")
    if url.startswith("wss://"):
        return "https://" + url.removeprefix("wss://")
    return url


HASS_URL = _hass_base_url(os.environ.get("HASS_URL") or "")
HASS_TOKEN = os.environ.get("HASS_TOKEN")
HASS_PERSON = os.environ.get("HASS_PERSON")  # e.g. person.maksym; default: first person
FETCH_EVERY = 60  # seconds between server calls
SYDNEY = ZoneInfo("Australia/Sydney")


@dataclass
class Row:
    time: datetime  # when to leave
    label: str
    live: bool = True


@dataclass
class Board:
    icon: str  # menu bar prefix: 🚆 to town, 🏠 heading home
    heading: str  # first dropdown line, e.g. "Engadine → Town Hall"
    rows: list[Row] = field(default_factory=list)
    countdown: bool = True  # False = away, show no timer


# --- Home Assistant -------------------------------------------------------


NO_LOCATION = ("unknown", "unavailable")  # tracker isn't reporting: not the same as away


def _hass_get(path: str) -> object:
    req = urllib.request.Request(
        f"{HASS_URL}{path}", headers={"Authorization": f"Bearer {HASS_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# /api/states is every entity in the house (megabytes on a big install), so it
# is read at most once per run: to pick a default person, and to map zone names
# to coordinates. Each poll after that reads just the one person entity.
_person_id: str | None = HASS_PERSON
_zones: dict[str, tuple[float | None, float | None]] | None = None


def _load_all() -> None:
    global _person_id, _zones
    states = _hass_get("/api/states")
    _zones = {
        s["attributes"].get("friendly_name", "").lower(): (
            s["attributes"].get("latitude"),
            s["attributes"].get("longitude"),
        )
        for s in states
        if s["entity_id"].startswith("zone.")
    }
    if _person_id is None:
        # Prefer a person backed by a login and a tracker over integration accounts.
        people = [s for s in states if s["entity_id"].startswith("person.")]
        real = [p for p in people if p["attributes"].get("device_trackers")]
        if not (real or people):
            raise RuntimeError("Home Assistant has no person entity")
        _person_id = (real or people)[0]["entity_id"]


def hass_location() -> tuple[str, float | None, float | None]:
    """Return (zone state, zone latitude, zone longitude) for the person.

    The person's state is the friendly name of the zone they are in ("home",
    "Work", ...) or "not_home". The zone's own coordinates are used rather than
    the phone's GPS, so the trip home always starts from the same place.
    """
    if _person_id is None:
        _load_all()
    try:
        person = _hass_get(f"/api/states/{_person_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Home Assistant has no {_person_id}") from None
        raise
    state = person["state"]
    if state == "home" or state == "not_home" or state in NO_LOCATION:
        return state, None, None
    if _zones is None or state.lower() not in _zones:
        _load_all()  # first non-home zone, or a zone added since we last looked
    lat, lon = _zones.get(state.lower(), (None, None))
    return state, lat, lon


# --- tfnsw MCP server -----------------------------------------------------


async def _call(session: ClientSession, tool: str, args: dict) -> dict:
    result = await session.call_tool(tool, args)
    if result.is_error:
        raise RuntimeError(result.content[0].text if result.content else f"{tool} failed")
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


async def _resolve_stop(session: ClientSession, stop_id: str | None) -> str:
    if stop_id:
        return stop_id
    found = await _call(session, "find_stop", {"query": STATION, "location_type": "stop", "limit": 1})
    stops = found.get("locations") or []
    if not stops:
        raise RuntimeError(f"No stop found for {STATION!r}")
    return str(stops[0]["id"])


async def fetch_board(
    api_key: str, stop_id: str | None, zone: str, lat: float | None, lon: float | None
) -> tuple[str, Board]:
    """Return (resolved home stop id, what to display) for the given zone."""
    http = create_mcp_http_client(headers={"X-API-Key": api_key})
    async with http, streamable_http_client(MCP_URL, http_client=http) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            stop_id = await _resolve_stop(session, stop_id)

            if zone == "home" or zone in NO_LOCATION:
                # concise=False: we need destination and onward stops to pick a direction.
                data = await _call(session, "get_departures", {"stop_id": stop_id, "concise": False})
                heading = f"{_short(STATION)} → {TOWARDS}"
                if zone in NO_LOCATION:
                    heading += "  (location unknown)"
                return stop_id, Board("🚆", heading, _towards_rows(data))

            if lat is not None and lon is not None:
                data = await _call(
                    session,
                    "plan_trip_from_coordinate",
                    {
                        "latitude": lat,
                        "longitude": lon,
                        "destination_id": stop_id,
                        "detail": "answer",
                        "max_results": 5,
                    },
                )
                return stop_id, Board("🏠", f"{zone} → {_short(STATION)}", _journey_rows(data))

            return stop_id, Board("🚆", f"Away ({zone.replace('_', ' ')})", countdown=False)


def _short(station: str) -> str:
    return station.removesuffix(" Station")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=SYDNEY)


def _departure_time(dep: dict) -> datetime | None:
    return _parse_time(dep.get("departure_estimated")) or _parse_time(dep.get("departure_planned"))


def _goes_towards(dep: dict, place: str = TOWARDS) -> bool:
    """True if the service calls at *place* after this stop.

    Uses the onward stop list when the API provides it; otherwise falls back to
    the destination name (e.g. "Bondi Junction" trains run via Town Hall).
    """
    target = place.lower()
    onwards = dep.get("onwards_locations") or []
    if onwards:
        return target in json.dumps(onwards).lower()
    transport = dep.get("transportation") or {}
    destination = (transport.get("destination_name") or "").lower()
    return target in destination or "bondi junction" in destination


def _towards_rows(data: dict) -> list[Row]:
    rows = []
    for dep in data.get("departures", []):
        t = _departure_time(dep)
        if t is None or not _goes_towards(dep):
            continue
        transport = dep.get("transportation") or {}
        line = transport.get("disassembled_name") or transport.get("number") or "Train"
        dest = transport.get("destination_name")
        label = f"{line} → {dest}" if dest else line
        rows.append(Row(t, label, dep.get("departure_estimated") is not None))
    return sorted(rows, key=lambda r: r.time)


def _journey_rows(data: dict) -> list[Row]:
    rows = []
    for j in data.get("journeys", []):
        leave, arrive = _parse_time(j.get("departure")), _parse_time(j.get("arrival"))
        if leave is None:
            continue
        label = f"arrive {arrive.strftime('%H:%M')}" if arrive else "trip"
        if j.get("changes"):
            label += f", {j['changes']} change{'s' if j['changes'] > 1 else ''}"
        rows.append(Row(leave, label))
    return sorted(rows, key=lambda r: r.time)


# --- Menu bar app ---------------------------------------------------------


class CommuteApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Commute", title="🚆 …", quit_button="Quit")
        self.api_key = os.environ.get("TFNSW_API_KEY")
        self.stop_id = STOP_ID
        self.board: Board | None = None
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._fetching = False
        self.menu = [rumps.MenuItem("Refresh now", callback=lambda _: self._start_fetch()), None]

    # Network work runs off the main thread so the menu bar never freezes.
    def _start_fetch(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        errors: list[str] = []
        zone, lat, lon = "home", None, None
        if HASS_URL and HASS_TOKEN:
            try:
                zone, lat, lon = hass_location()
            except Exception as exc:  # fall back to the home view
                errors.append(f"Home Assistant: {exc or type(exc).__name__}")
        try:
            if not self.api_key:
                raise RuntimeError("Set TFNSW_API_KEY")
            stop_id, board = asyncio.run(fetch_board(self.api_key, self.stop_id, zone, lat, lon))
            with self._lock:
                self.stop_id, self.board = stop_id, board
        except Exception as exc:  # show it in the menu rather than crash
            errors.append(str(exc) or type(exc).__name__)
        finally:
            with self._lock:
                self.errors = errors
            self._fetching = False

    @rumps.timer(FETCH_EVERY)
    def periodic_fetch(self, _) -> None:
        self._start_fetch()

    @rumps.timer(5)
    def redraw(self, _) -> None:
        """Recompute the countdown on the main thread from the last fetch."""
        now = datetime.now(SYDNEY)
        with self._lock:
            board, errors = self.board, list(self.errors)
        upcoming = [r for r in board.rows if r.time >= now] if board else []

        if board and not board.countdown:
            self.title = f"{board.icon} –"
        elif upcoming:
            mins = int((upcoming[0].time - now).total_seconds() // 60)
            self.title = f"{board.icon} now" if mins <= 0 else f"{board.icon} {mins} min"
        elif errors:
            self.title = "🚆 ⚠︎"
        else:
            self.title = f"{board.icon if board else '🚆'} …"

        # Rebuild the dropdown: heading, next few departures, then any errors.
        for key in list(self.menu.keys())[2:]:
            if key != "Quit":
                del self.menu[key]
        items = [board.heading] if board else []
        items += [
            f"{r.time.strftime('%H:%M')}  {r.label}{'' if r.live else '  (scheduled)'}"
            for r in upcoming[:6]
        ]
        if board and board.countdown and not upcoming:
            items.append("No departures")
        items += [f"⚠︎ {e[:80]}" for e in errors]
        for text in items or ["Loading…"]:
            self.menu.insert_before("Quit", rumps.MenuItem(text))


def main() -> None:
    app = CommuteApp()
    app._start_fetch()
    app.run()
