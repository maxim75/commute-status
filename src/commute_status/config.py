"""Settings, read from .env and the environment."""

from __future__ import annotations

import os
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
STOP_ID = os.environ.get("COMMUTE_STOP_ID")  # skip the name lookup if set
TOWARDS = os.environ.get("COMMUTE_TOWARDS", "Town Hall")  # only trains calling here later

WAIT_FETCH_EVERY = 60  # seconds between server calls while waiting
TRAVEL_FETCH_EVERY = 30  # ... and while on a train
SYDNEY = ZoneInfo("Australia/Sydney")


def short(station: str) -> str:
    """'Sutherland Station, Platform 1' -> 'Sutherland'."""
    return station.split(",")[0].removesuffix(" Station")
