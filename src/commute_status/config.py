"""Settings, read from .env and the environment."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import find_dotenv, load_dotenv

# .env from the current directory (or a parent), then the project root.
# Real environment variables win over both.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _hass_base_url(url: str) -> str:
    """Accept the REST or WebSocket form: ws://host:8123/api/websocket -> http://host:8123."""
    url = url.strip().rstrip("/").removesuffix("/api/websocket").removesuffix("/api")
    if url.startswith("ws://"):
        return "http://" + url.removeprefix("ws://")
    if url.startswith("wss://"):
        return "https://" + url.removeprefix("wss://")
    return url


TFNSW_API_KEY = os.environ.get("TFNSW_API_KEY")
MCP_URL = os.environ.get("TFNSW_MCP_URL", "https://tfnsw-trip-planner.cool.imaxim.org/mcp")
STATION = os.environ.get("COMMUTE_STATION", "Engadine Station")
STOP_ID = os.environ.get("COMMUTE_STOP_ID")  # skip the name lookup if set
TOWARDS = os.environ.get("COMMUTE_TOWARDS", "Town Hall")  # only trains calling here later

HASS_URL = _hass_base_url(os.environ.get("HASS_URL") or "")
HASS_TOKEN = os.environ.get("HASS_TOKEN")
HASS_PERSON = os.environ.get("HASS_PERSON")  # e.g. person.max; default: first real person

WAIT_FETCH_EVERY = 60  # seconds between server calls while waiting
TRAVEL_FETCH_EVERY = 30  # ... and while on a train
SYDNEY = ZoneInfo("Australia/Sydney")


def short(station: str) -> str:
    """'Sutherland Station, Platform 1' -> 'Sutherland'."""
    return station.split(",")[0].removesuffix(" Station")
