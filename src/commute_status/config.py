"""Settings, read from .env and the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import find_dotenv, load_dotenv

def _user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "commute-status"


# Settings come from, highest priority first (load_dotenv never overrides):
#   1. real environment variables
#   2. .env in the current directory or a parent
#   3. .env in the project root (when run from a checkout, e.g. `uv run`)
#   4. ~/.config/commute-status/.env (works for uvx / `uv tool install`, whose
#      code lives in uv's cache where 3 can't find anything)
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
USER_ENV = _user_config_dir() / ".env"
load_dotenv(USER_ENV)


TFNSW_API_KEY = os.environ.get("TFNSW_API_KEY")
MCP_URL = os.environ.get("TFNSW_MCP_URL", "https://tfnsw-trip-planner.cool.imaxim.org/mcp")
STATION = os.environ.get("COMMUTE_STATION", "Engadine Station")
TOWARDS = os.environ.get("COMMUTE_TOWARDS", "Town Hall")
# Where to start mapping the line, upstream of STATION, to place trains still on
# their way to it. Empty = only map from STATION onwards.
LINE_START = os.environ.get("COMMUTE_LINE_START", "Waterfall Station")
# From this time of day the trip is reversed (TOWARDS -> STATION). Empty = never.
RETURN_AFTER = os.environ.get("COMMUTE_RETURN_AFTER", "14:00")
RETURN_LINE_START = os.environ.get("COMMUTE_RETURN_LINE_START", "Bondi Junction Station")
# GTFS direction digit of the morning trip in the GPS feed's route IDs
# ("ESI_2a"): on T4 and South Coast, 2 runs towards the city / Bondi Junction.
FEED_DIRECTION = os.environ.get("COMMUTE_FEED_DIRECTION", "2")
DEPARTED_WINDOW_MIN = 60  # look this far back for trains still on their way
UPCOMING_COUNT = 6  # departures to list

TITLE_STYLE = os.environ.get("COMMUTE_TITLE", "auto")  # auto | full | compact | minimal

WAIT_FETCH_EVERY = 60  # seconds between server calls while waiting
TRAVEL_FETCH_EVERY = 30  # ... and while on a train
SYDNEY = ZoneInfo("Australia/Sydney")


def short(station: str) -> str:
    """'Sutherland Station, Platform 1' -> 'Sutherland'."""
    return station.split(",")[0].removesuffix(" Station")


@dataclass(frozen=True)
class Direction:
    """One way of the commute."""

    station: str  # where you board
    towards: str  # where you're going
    line_start: str  # upstream of `station`, to place trains not there yet
    feed_direction: str  # GTFS direction digit of these trains in the GPS feed

    @property
    def key(self) -> str:
        return f"{self.station}|{self.towards}|{self.line_start}"

    @property
    def heading(self) -> str:
        return f"{short(self.station)} → {short(self.towards)}"


MORNING = Direction(STATION, TOWARDS, LINE_START, FEED_DIRECTION)
RETURN = Direction(TOWARDS, STATION, RETURN_LINE_START, {"1": "2", "2": "1"}.get(FEED_DIRECTION, ""))


def _parse_hhmm(value: str) -> time | None:
    try:
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    except ValueError:
        return None


_RETURN_AT = _parse_hhmm(RETURN_AFTER) if RETURN_AFTER else None


def direction_at(now: datetime) -> Direction:
    """Morning trip until RETURN_AFTER, the return trip from then until midnight."""
    if _RETURN_AT and now.astimezone(SYDNEY).time() >= _RETURN_AT:
        return RETURN
    return MORNING
