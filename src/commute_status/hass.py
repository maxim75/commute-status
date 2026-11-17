"""Which zone you are in, from Home Assistant's REST API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config

NO_LOCATION = ("unknown", "unavailable")  # tracker isn't reporting: not the same as away


def enabled() -> bool:
    return bool(config.HASS_URL and config.HASS_TOKEN)


def _get(path: str) -> object:
    req = urllib.request.Request(
        f"{config.HASS_URL}{path}", headers={"Authorization": f"Bearer {config.HASS_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# /api/states is every entity in the house (megabytes on a big install), so it
# is read at most once per run: to pick a default person, and to map zone names
# to coordinates. Each poll after that reads just the one person entity.
_person_id: str | None = config.HASS_PERSON
_zones: dict[str, tuple[float | None, float | None]] | None = None


def _load_all() -> None:
    global _person_id, _zones
    states = _get("/api/states")
    _zones = {
        s["attributes"].get("friendly_name", "").lower(): (
            s["attributes"].get("latitude"),
            s["attributes"].get("longitude"),
        )
        for s in states
        if s["entity_id"].startswith("zone.")
    }
    if _person_id is None:
        # Prefer a person backed by a tracker over integration accounts.
        people = [s for s in states if s["entity_id"].startswith("person.")]
        real = [p for p in people if p["attributes"].get("device_trackers")]
        if not (real or people):
            raise RuntimeError("Home Assistant has no person entity")
        _person_id = (real or people)[0]["entity_id"]


def location() -> tuple[str, float | None, float | None]:
    """Return (zone state, zone latitude, zone longitude) for the person.

    The state is the friendly name of the zone ("home", "Work", ...) or
    "not_home". The zone's own coordinates are used rather than the phone's
    GPS, so a trip home always starts from the same place.
    """
    if _person_id is None:
        _load_all()
    try:
        person = _get(f"/api/states/{_person_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Home Assistant has no {_person_id}") from None
        raise
    state = person["state"]
    if state in ("home", "not_home") or state in NO_LOCATION:
        return state, None, None
    if _zones is None or state.lower() not in _zones:
        _load_all()  # first non-home zone, or a zone added since we last looked
    lat, lon = _zones.get(state.lower(), (None, None))
    return state, lat, lon
