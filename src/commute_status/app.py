"""The menu bar app: wait mode and travelling mode.

Wait mode (the default) shows minutes until the next train from the station
towards the destination (Engadine -> Town Hall unless configured otherwise).

Click a departure in the dropdown to pick that train. Until it leaves, the
countdown follows it; once it departs the app switches to travelling mode:
minutes to the destination and the next station. It goes back to wait mode a
couple of minutes after arrival, or when you choose "Stop travelling".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import rumps
from AppKit import NSScreen

from . import config, tfnsw, travel
from .config import SYDNEY, short

END_AFTER_ARRIVAL = timedelta(minutes=2)
TITLE_LEVELS = {"full": 0, "compact": 1, "minimal": 2}  # COMMUTE_TITLE; anything else = auto
AUTOSAVE_NAME = "commute-status"  # macOS remembers a ⌘-dragged position under this name


def _notify(title: str, message: str) -> None:
    # rumps.notification needs a bundled app; osascript works from plain Python.
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


@dataclass
class Row:
    time: datetime  # when to leave
    label: str
    live: bool = True
    departure: tfnsw.Departure | None = None  # the train this row is, for picking


@dataclass
class Board:
    heading: str
    rows: list[Row] = field(default_factory=list)


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


class CommuteApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Commute", title="🚆 …", quit_button="Quit")
        self.home_stop_id: str | None = config.STOP_ID
        self.dest_stop_id: str | None = None
        self.coords: dict[str, tfnsw.LatLon | None] = {}  # station short name -> coords
        self.board: Board | None = None
        self.trip: travel.Trip | None = None
        self.errors: list[str] = []
        self.last_fetch = datetime.min.replace(tzinfo=SYDNEY)
        self._lock = threading.Lock()
        self._fetching = False
        self.menu = [rumps.MenuItem("Refresh now", callback=lambda _: self.fetch_now()), None]
        style = config.TITLE_STYLE
        self._auto_title = style not in TITLE_LEVELS
        self._level = TITLE_LEVELS.get(style, 0)
        self._hidden_streak = 0
        self._hidden_notified = False

    # --- fetching (background thread) -------------------------------------

    def fetch_now(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        self.last_fetch = datetime.now(SYDNEY)
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        errors: list[str] = []
        try:
            with self._lock:
                trip = self.trip
            if trip:
                asyncio.run(self._fetch_trip(trip))
            else:
                board = asyncio.run(self._fetch_board())
                with self._lock:
                    self.board = board
        except Exception as exc:  # show it in the menu rather than crash
            errors.append(str(exc) or type(exc).__name__)
        finally:
            with self._lock:
                self.errors = errors
            self._fetching = False

    async def _home_stop(self, s) -> str:
        if not self.home_stop_id:
            self.home_stop_id, _ = await tfnsw.find_stop(s, config.STATION)
        return self.home_stop_id

    async def _dest_stop(self, s) -> str:
        if not self.dest_stop_id:
            self.dest_stop_id, _ = await tfnsw.find_stop(s, config.TOWARDS)
        return self.dest_stop_id

    async def _fetch_board(self) -> Board:
        async with tfnsw.session() as s:
            deps = await tfnsw.departures_towards(s, await self._home_stop(s))
        rows = [
            Row(d.time, f"{d.line} → {d.destination}" if d.destination else d.line, d.live, d)
            for d in deps
        ]
        return Board(f"{short(config.STATION)} → {config.TOWARDS}", rows)

    async def _fetch_trip(self, trip: travel.Trip) -> None:
        async with tfnsw.session() as s:
            origin, dest = await self._home_stop(s), await self._dest_stop(s)
            journeys = await tfnsw.plan(s, origin, dest, when=trip.picked - timedelta(minutes=2), n=4)
            journey = travel.best_journey(trip, journeys)
            stops = journey.stops if journey else trip.stops
            for name in stops:
                key = short(name)
                if key not in self.coords:
                    _, self.coords[key] = await tfnsw.find_stop(s, key)
            now = datetime.now(SYDNEY)
            vehicles = await tfnsw.train_positions(s) if now >= trip.departure else []

        with self._lock:
            if self.trip is not trip:  # cancelled while we were fetching
                return
            if journey:
                trip.update_from(journey)
            elif not trip.planned:
                raise RuntimeError("Couldn't find that train in the trip planner")
            trip.coords = [self.coords.get(short(n)) for n in trip.stops]
            if trip.departed(now):
                travel.locate(trip, vehicles, now)

    # --- user actions -----------------------------------------------------

    def pick(self, dep: tfnsw.Departure) -> None:
        trip = travel.Trip(
            route=dep.route,
            line=dep.line,
            picked=dep.time,
            departure=dep.time,
            arrival=dep.time + timedelta(hours=1),  # until the planner answers
            live=dep.live,
            stops=[],
        )
        with self._lock:
            self.trip = trip
        self._fetching = False  # a wait-mode fetch in flight must not block this one
        self.fetch_now()
        self.redraw(None)

    def stop_travelling(self, _=None) -> None:
        with self._lock:
            self.trip = None
            self.board = None
        self.fetch_now()
        self.redraw(None)

    # --- drawing (main thread) --------------------------------------------

    @rumps.timer(10)
    def tick(self, _) -> None:
        every = config.TRAVEL_FETCH_EVERY if self.trip else config.WAIT_FETCH_EVERY
        if datetime.now(SYDNEY) - self.last_fetch >= timedelta(seconds=every):
            self.fetch_now()

    # --- staying visible in a crowded menu bar -----------------------------
    #
    # When the menu bar is full, macOS puts new items behind the notch, where
    # nobody can see or click them, without saying so. The app checks where its
    # item ended up, steps down to shorter titles while it is hidden, and if
    # even the shortest one doesn't fit, says so once in a notification.

    def _set_title(self, full: str, compact: str, minimal: str) -> None:
        self.title = (full, compact, minimal)[self._level]

    def _countdown(self, mins: int) -> None:
        if mins == 0:
            self._set_title("🚆 now", "🚆 now", "now")
        else:
            self._set_title(f"🚆 {mins} min", f"🚆 {mins}m", f"{mins}m")

    def _item_hidden(self) -> bool:
        """True if the status item is behind the notch or not placed at all."""
        try:
            win = self._nsapp.nsstatusitem.button().window()
        except AttributeError:  # not created yet
            return False
        if win is None:
            return False
        frame = win.frame()
        if frame.size.height == 0:
            return True
        screen = win.screen() or NSScreen.mainScreen()
        right = screen.auxiliaryTopRightArea() if hasattr(screen, "auxiliaryTopRightArea") else None
        # Displays without a notch report an empty area; nothing to hide behind.
        return bool(right and right.size.width > 0 and frame.origin.x < right.origin.x - 1)

    def _check_visible(self) -> None:
        self._hidden_streak = self._hidden_streak + 1 if self._item_hidden() else 0
        if self._hidden_streak < 2:  # one bad reading can be a relayout in progress
            return
        self._hidden_streak = 0
        if self._auto_title and self._level < 2:
            self._level += 1
        elif not self._hidden_notified:
            self._hidden_notified = True
            _notify(
                "Commute status is hidden behind the notch",
                "The menu bar is full. Hold ⌘ and drag other icons out of the menu bar, "
                "or drag the 🚆 item to the right; its spot is remembered.",
            )

    @rumps.timer(5)
    def redraw(self, _) -> None:
        if _ is not None:  # timer tick, not a redraw after a click
            self._check_visible()
        now = datetime.now(SYDNEY)
        with self._lock:
            trip, board, errors = self.trip, self.board, list(self.errors)
            if trip and trip.planned and now >= trip.arrival + END_AFTER_ARRIVAL:
                self.trip, trip, self.board = None, None, None  # back to wait mode
            items = self._trip_items(trip, now) if trip else self._wait_items(board, errors, now)
        items += [rumps.MenuItem(f"⚠︎ {e[:80]}") for e in errors]

        for key in list(self.menu.keys())[2:]:
            if key != "Quit":
                del self.menu[key]
        for item in items:
            self.menu.insert_before("Quit", item)

    def _wait_items(self, board: Board | None, errors: list[str], now: datetime) -> list[rumps.MenuItem]:
        upcoming = [r for r in board.rows if r.time >= now] if board else []
        if upcoming:
            self._countdown(_minutes(upcoming[0].time - now))
        elif errors:
            self._set_title("🚆 ⚠︎", "🚆 ⚠︎", "⚠︎")
        else:
            self._set_title("🚆 …", "🚆 …", "…")

        if not board:
            return [rumps.MenuItem("Loading…")]
        items = [rumps.MenuItem(board.heading)]
        for r in upcoming[:6]:
            text = f"{r.time:%H:%M}  {r.label}{'' if r.live else '  (scheduled)'}"
            items.append(rumps.MenuItem(text, callback=lambda _, d=r.departure: self.pick(d)))
        if upcoming:
            items.append(rumps.MenuItem("Click a train to follow it"))
        else:
            items.append(rumps.MenuItem("No departures"))
        return items

    def _trip_items(self, trip: travel.Trip, now: datetime) -> list[rumps.MenuItem]:
        dest = trip.destination or config.TOWARDS
        live = "" if trip.live else "  (scheduled)"
        items: list[rumps.MenuItem] = []

        if not trip.departed(now):
            mins = _minutes(trip.departure - now)
            self._countdown(mins)
            items.append(rumps.MenuItem(f"Your train: {trip.departure:%H:%M} {trip.line}{live}"))
            if trip.planned:
                items.append(rumps.MenuItem(f"Arrives {dest} {trip.arrival:%H:%M}"))
        elif trip.arrived(now):
            self._set_title(f"🚆 {short(dest)}", "🚆 ✓", "✓")
            items.append(rumps.MenuItem(f"Arrived at {dest}"))
        else:
            mins = _minutes(trip.arrival - now)
            kind, station = trip.where()
            self._set_title(
                f"🚆 {mins} min · {kind} {station}" if station else f"🚆 {mins} min",
                f"🚆 {mins}m · {station}" if station else f"🚆 {mins}m",
                f"{mins}m",
            )
            items.append(rumps.MenuItem(f"{trip.line} to {dest}: arrive {trip.arrival:%H:%M}{live}"))
            if trip.between() and kind == "next":
                items.append(rumps.MenuItem(f"Between {trip.between()}"))
            if trip.pos_source == "gps" and trip.pos_time:
                items.append(rumps.MenuItem(f"Position from train GPS, {trip.pos_time:%H:%M:%S}"))
            else:
                items.append(rumps.MenuItem("Position estimated from timetable"))
            if trip.pos is not None and trip.stops:
                # +0.08 so a train sitting at a station (pos 1.97) counts as there.
                ahead = [short(n) for n in trip.stops[int(trip.pos + 0.08) + 1 :]]
                if ahead:
                    items.append(rumps.MenuItem("Stops: " + ", ".join(ahead[:6]) + (" …" if len(ahead) > 6 else "")))

        items.append(rumps.MenuItem("Stop travelling", callback=self.stop_travelling))
        return items


def run() -> None:
    app = CommuteApp()

    # Runs right after rumps creates the status item, before the first layout,
    # so a position the user ⌘-dragged it to last time is restored.
    @rumps.events.before_start.register
    def _remember_position() -> None:
        app._nsapp.nsstatusitem.setAutosaveName_(AUTOSAVE_NAME)

    app.fetch_now()
    app.run()
