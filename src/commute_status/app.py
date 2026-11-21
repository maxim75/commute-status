"""The menu bar app: wait mode and travelling mode.

Wait mode (the default) shows minutes until the next train from the station
towards the destination: Engadine -> Town Hall in the morning, and the other
way round from COMMUTE_RETURN_AFTER (14:00) until midnight.

Click a departure in the dropdown to pick that train. Until it leaves, the
countdown follows it; once it departs the app switches to travelling mode:
minutes to the destination and the next station. It goes back to wait mode a
couple of minutes after arrival, or when you choose "Stop travelling".

Nothing is requested from TfNSW until you choose "Activate" in the dropdown;
the app switches itself off again after COMMUTE_ACTIVE_FOR minutes (1 hour).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import rumps
from AppKit import NSApplication, NSFloatingWindowLevel, NSScreen
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

from . import config, line, tfnsw, travel
from .config import SYDNEY, short

END_AFTER_ARRIVAL = timedelta(minutes=2)
TITLE_LEVELS = {"full": 0, "compact": 1, "minimal": 2}  # COMMUTE_TITLE; anything else = auto
AUTOSAVE_NAME = "commute-status"  # macOS remembers a ⌘-dragged position under this name
CHANGE_STATIONS = "Change stations…"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # shown in place of 🚆 while data is being requested
SPIN_EVERY = 0.15  # seconds per frame
ACTIVE_ITEM = "active"  # menu key of the Activate / Deactivate item
REFRESH = "Refresh now"
FIXED_ITEMS = 3  # active toggle, refresh, separator; the rest is redrawn


def _duration(minutes: int) -> str:
    """60 -> '1 h', 90 -> '1 h 30 min', 45 -> '45 min'."""
    h, m = divmod(minutes, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "") if p)


def _notify(title: str, message: str) -> None:
    # rumps.notification needs a bundled app; osascript works from plain Python.
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _bring_to_front(window: rumps.Window) -> None:
    """Make a dialog visible and typeable.

    A menu bar app is never the active app, and plain Python starts with the
    "prohibited" activation policy, which can't become active at all, so its
    dialogs open behind every other window. "Accessory" still has no Dock icon
    but may come to the front.
    """
    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    if hasattr(nsapp, "activate"):  # macOS 14+
        nsapp.activate()
    else:
        nsapp.activateIgnoringOtherApps_(True)
    alert_window = window._alert.window()
    alert_window.setLevel_(NSFloatingWindowLevel)  # above other apps even if activation is refused
    alert_window.setInitialFirstResponder_(window._textfield)


@dataclass
class Row:
    time: datetime  # departure from the home station
    label: str
    live: bool = True
    departure: tfnsw.Departure | None = None  # the train this row is, for picking
    position: str | None = None  # e.g. "@Heathcote", "◑ Sydenham"
    arrival: datetime | None = None  # at the destination, for trains already on their way


@dataclass
class Board:
    direction: config.Direction
    rows: list[Row] = field(default_factory=list)  # still to leave the home station
    departed: list[Row] = field(default_factory=list)  # left, not yet at the destination
    warnings: list[str] = field(default_factory=list)

    def carry_positions(self, old: Board | None) -> None:
        """Copy each train's position from an earlier board, matched by time and route."""
        if old is None:
            return
        known = {(r.time, r.departure.route): r.position for r in old.departed + old.rows if r.departure}
        for r in self.departed + self.rows:
            if r.position is None and r.departure:
                r.position = known.get((r.time, r.departure.route))


def _line_name(route: str) -> str:
    """'T4 Eastern Suburbs & Illawarra Line' -> 'T4', 'South Coast Line' -> 'SCO'."""
    m = re.match(r"([A-Z]\d+)\b", route)
    if m:
        return m.group(1)
    return {"South Coast Line": "SCO", "Blue Mountains Line": "BMT", "Central Coast & Newcastle Line": "CCN"}.get(
        route, route
    )


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


class CommuteApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Commute", title="🚆 …", quit_button="Quit")
        self.stop_ids: dict[str, str] = {}  # station name -> stop ID
        self.coords: dict[str, tfnsw.LatLon | None] = {}  # station short name -> coords
        self.lines: dict[str, line.Line] = {}  # Direction.key -> mapped line, for positions
        self.board: Board | None = None
        self.trip: travel.Trip | None = None
        self.errors: list[str] = []
        self.last_fetch = datetime.min.replace(tzinfo=SYDNEY)
        self._lock = threading.Lock()
        self._fetching = False
        self._requests = 0  # requests in flight, for the spinner; several can overlap
        self._spin = 0  # spinner frame
        self._title_parts = ("🚆 …", "🚆 …", "…")
        # Requests to TfNSW are made only while active, until this time.
        self.active_until: datetime | None = None
        self._active_item = rumps.MenuItem(ACTIVE_ITEM, callback=self.toggle_active)
        self.menu = [
            self._active_item,
            rumps.MenuItem(REFRESH, callback=lambda _: self.fetch_now()),
            None,
            rumps.MenuItem(CHANGE_STATIONS, callback=self.change_stations),
        ]
        style = config.TITLE_STYLE
        self._auto_title = style not in TITLE_LEVELS
        self._level = TITLE_LEVELS.get(style, 0)
        self._hidden_streak = 0
        self._hidden_notified = False
        self.quitting = False

    # --- fetching (background thread) -------------------------------------

    def active(self, now: datetime | None = None) -> bool:
        return self.active_until is not None and (now or datetime.now(SYDNEY)) < self.active_until

    def fetch_now(self) -> None:
        if self._fetching or not self.active():
            return
        self._fetching = True
        self.last_fetch = datetime.now(SYDNEY)
        threading.Thread(target=self._fetch, daemon=True).start()

    def _request_started(self) -> None:
        with self._lock:
            self._requests += 1

    def _request_done(self) -> None:
        with self._lock:
            self._requests -= 1

    def _fetch(self) -> None:
        errors: list[str] = []
        self._request_started()
        try:
            with self._lock:
                trip = self.trip
            if trip:
                asyncio.run(self._fetch_trip(trip))
            else:
                asyncio.run(self._fetch_board())
        except Exception as exc:  # show it in the menu rather than crash
            errors.append(str(exc) or type(exc).__name__)
        finally:
            with self._lock:
                if self.active():
                    self.errors = errors
            self._fetching = False
            self._request_done()

    async def _stop_id(self, s, name: str) -> str:
        if name not in self.stop_ids:
            self.stop_ids[name], _ = await tfnsw.find_stop(s, name)
        return self.stop_ids[name]

    async def _fetch_board(self) -> None:
        """Publish the list as soon as it is known, then again with positions."""
        now = datetime.now(SYDNEY)
        direction = config.direction_at(now)
        warnings: list[str] = []
        async with tfnsw.session() as s:
            home, dest = await asyncio.gather(
                self._stop_id(s, direction.station), self._stop_id(s, direction.towards)
            )
            positions = asyncio.ensure_future(self._positions_inputs(s, direction, home, dest))
            # From an hour back, to include trains that left but haven't arrived.
            back = now - timedelta(minutes=config.DEPARTED_WINDOW_MIN)
            trains = await tfnsw.direct_trains(s, home, dest, back, config.UPCOMING_COUNT, now)
            board = self._board(direction, trains, now)
            with self._lock:
                old, self.board = self.board, board
            # Until fresh positions arrive, keep the ones each train had before.
            board.carry_positions(old)
            try:
                vehicles = await positions
            except Exception as exc:  # positions are a bonus; the list still works
                warnings.append(f"Train positions: {exc or type(exc).__name__}")
                vehicles = []

        board = self._board(direction, trains, now, warnings)
        if direction.key in self.lines and vehicles:
            self._place(board, vehicles, datetime.now(SYDNEY))
        with self._lock:
            if self.active():  # not switched off while this was in flight
                self.board = board

    async def _positions_inputs(self, s, direction: config.Direction, home: str, dest: str) -> list[dict]:
        if direction.key not in self.lines:
            mapped = line.load_cached(direction.key)
            if mapped is None:
                mapped = await line.build(s, home, dest, direction.line_start, self.coords)
                line.save_cached(mapped, direction.key)
            self.lines[direction.key] = mapped
            # Travelling mode needs the same stations; don't look them up twice.
            for name, coords in zip(mapped.stops, mapped.coords):
                self.coords.setdefault(name, coords)
        return await tfnsw.train_positions(s)

    def _board(self, direction: config.Direction, trains: list[tfnsw.Journey], now: datetime, warnings=()) -> Board:
        rows, departed = [], []
        for j in trains:
            name = _line_name(j.route)
            dep = tfnsw.Departure(j.departure, j.route, name, j.towards, j.live)
            if j.departure >= now:
                rows.append(Row(j.departure, f"{name} → {j.towards}" if j.towards else name, j.live, dep))
            elif now < j.arrival:
                departed.append(Row(j.departure, name, j.live, dep, arrival=j.arrival))
        return Board(direction, rows[: config.UPCOMING_COUNT], departed, list(warnings))

    def _place(self, board: Board, vehicles: list[dict], now: datetime) -> None:
        mapped = self.lines[board.direction.key]
        trains = board.departed + board.rows
        guesses = [
            (r.departure.route, mapped.guess_departed(r.time, r.arrival, now))
            if r.arrival
            else (r.departure.route, mapped.guess_upcoming((r.time - now).total_seconds() / 60))
            for r in trains
        ]
        found = line.match(mapped, guesses, vehicles, now, board.direction.feed_direction)
        for i, r in enumerate(trains):
            if i in found:
                r.position = mapped.describe(*found[i])

    async def _fetch_trip(self, trip: travel.Trip) -> None:
        async with tfnsw.session() as s:
            origin, dest = await asyncio.gather(self._stop_id(s, trip.origin), self._stop_id(s, trip.dest))
            journeys = await tfnsw.plan(s, origin, dest, when=trip.picked - timedelta(minutes=2), n=4)
            journey = travel.best_journey(trip, journeys)
            stops = journey.stops if journey else trip.stops
            await line.lookup_coords(s, [short(n) for n in stops], self.coords)
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

    def activate(self) -> None:
        """Start requesting data, for config.ACTIVE_FOR_MIN from now."""
        self.active_until = datetime.now(SYDNEY) + timedelta(minutes=config.ACTIVE_FOR_MIN)
        self.fetch_now()

    def deactivate(self) -> None:
        """Stop requesting data and forget what was shown; it would go stale."""
        with self._lock:
            self.active_until, self.trip, self.board, self.errors = None, None, None, []
        self._fetching = False  # a fetch in flight is ignored when it lands

    def toggle_active(self, _=None) -> None:
        if self.active():
            self.deactivate()
        else:
            self.activate()
        self.redraw(None)

    def pick(self, dep: tfnsw.Departure, direction: config.Direction) -> None:
        trip = travel.Trip(
            route=dep.route,
            line=dep.line,
            picked=dep.time,
            departure=dep.time,
            arrival=dep.time + timedelta(hours=1),  # until the planner answers
            live=dep.live,
            stops=[],
            origin=direction.station,
            dest=direction.towards,
            feed_direction=direction.feed_direction,
        )
        with self._lock:
            self.trip = trip
        # Following a train is a fresh request for data: a full active period from now.
        self.active_until = datetime.now(SYDNEY) + timedelta(minutes=config.ACTIVE_FOR_MIN)
        self._fetching = False  # a wait-mode fetch in flight must not block this one
        self.fetch_now()
        self.redraw(None)

    def change_stations(self, _=None) -> None:
        """Ask for start and end stations, check them with TfNSW, then switch."""
        current = config.MORNING

        def ask(message: str, default: str) -> str | None:
            window = rumps.Window(
                message=message,
                title="Commute stations",
                default_text=short(default),
                ok="Next" if "start" in message else "Save",
                cancel="Cancel",
                dimensions=(260, 22),
            )
            _bring_to_front(window)
            response = window.run()
            if self.quitting:  # Ctrl-C closed the dialog
                rumps.quit_application()
                return None
            text = response.text.strip()
            return text if response.clicked and text else None

        station = ask("Station you start from in the morning (e.g. Engadine):", current.station)
        if not station:
            return
        towards = ask(
            f"Station you travel to from {station} (e.g. Town Hall).\n"
            f"After {config.RETURN_AFTER or 'never'} the app shows the trip back.",
            current.towards,
        )
        if not towards:
            return
        threading.Thread(target=self._apply_stations, args=(station, towards), daemon=True).start()

    def _apply_stations(self, station: str, towards: str) -> None:
        async def check():
            async with tfnsw.session() as s:
                return await tfnsw.check_stations(s, station, towards)

        self._request_started()
        try:
            station, towards = asyncio.run(check())
        except Exception as exc:
            _notify("Stations not changed", str(exc) or type(exc).__name__)
            return
        finally:
            self._request_done()
        config.set_stations(station, towards)
        with self._lock:
            self.board = None
        _notify("Stations changed", f"{short(station)} ⇄ {short(towards)}")
        self._fetching = False  # a fetch for the old stations must not block this one
        self.fetch_now()

    def stop_travelling(self, _=None) -> None:
        with self._lock:
            self.trip = None
            self.board = None
        self.fetch_now()
        self.redraw(None)

    # --- drawing (main thread) --------------------------------------------

    @rumps.timer(10)
    def tick(self, _) -> None:
        now = datetime.now(SYDNEY)
        every = config.TRAVEL_FETCH_EVERY if self.trip else config.WAIT_FETCH_EVERY
        board = self.board
        switched = not self.trip and board is not None and board.direction != config.direction_at(now)
        if switched or now - self.last_fetch >= timedelta(seconds=every):
            self.fetch_now()

    # --- staying visible in a crowded menu bar -----------------------------
    #
    # When the menu bar is full, macOS puts new items behind the notch, where
    # nobody can see or click them, without saying so. The app checks where its
    # item ended up, steps down to shorter titles while it is hidden, and if
    # even the shortest one doesn't fit, says so once in a notification.

    def _set_title(self, full: str, compact: str, minimal: str) -> None:
        self._title_parts = (full, compact, minimal)
        self._show_title()

    def _show_title(self) -> None:
        """The title, with a spinner in place of 🚆 while a request is in flight."""
        title = self._title_parts[self._level]
        if self._requests > 0:
            frame = SPINNER[self._spin % len(SPINNER)]
            title = frame + title.removeprefix("🚆") if title.startswith("🚆") else frame + title
        if title != self.title:
            self.title = title

    def _animate(self) -> None:
        """Next spinner frame; once requests finish, put the 🚆 back."""
        if self._requests > 0:
            self._spin += 1
        self._show_title()

    def _countdown(self, mins: int) -> None:
        if mins == 0:
            self._set_title("🚆 now", "🚆 now", "now")
        else:
            self._set_title(f"🚆 {mins}m", f"🚆 {mins}m", f"{mins}m")

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
        if self.active_until is not None and not self.active(now):
            self.deactivate()  # time's up
        active = self.active(now)
        self._active_item.title = (
            f"Deactivate (active until {self.active_until:%H:%M})"
            if active
            else f"Activate for {_duration(config.ACTIVE_FOR_MIN)}"
        )
        self.menu[REFRESH].set_callback((lambda _: self.fetch_now()) if active else None)
        with self._lock:
            trip, board, errors = self.trip, self.board, list(self.errors)
            if trip and trip.planned and now >= trip.arrival + END_AFTER_ARRIVAL:
                self.trip, trip, self.board = None, None, None  # back to wait mode
            if not active:
                items = self._inactive_items()
            else:
                items = self._trip_items(trip, now) if trip else self._wait_items(board, errors, now)
        items += [rumps.MenuItem(f"⚠︎ {e[:80]}") for e in errors]

        for key in list(self.menu.keys())[FIXED_ITEMS:]:
            if key not in (CHANGE_STATIONS, "Quit"):
                del self.menu[key]
        for item in items:
            self.menu.insert_before(CHANGE_STATIONS, item)

    def _inactive_items(self) -> list[rumps.MenuItem]:
        self._set_title("🚆 off", "🚆 off", "off")
        return [rumps.MenuItem("Not requesting train data until activated")]

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

        d = board.direction

        def item(text: str, r: Row) -> rumps.MenuItem:
            return rumps.MenuItem(text, callback=lambda _, dep=r.departure: self.pick(dep, d))

        items = [rumps.MenuItem(d.heading)]
        on_the_way = [r for r in board.departed if r.arrival and r.arrival > now]
        if on_the_way:
            items.append(rumps.MenuItem("Already left:"))
            for r in on_the_way:
                where = f" · {r.position}" if r.position else ""
                items.append(item(f"{r.time:%H:%M}  {r.label}{where} · {short(d.towards)} {r.arrival:%H:%M}", r))
            items.append(rumps.MenuItem("Next from " + short(d.station) + ":"))
        for r in upcoming[:6]:
            where = f" · {r.position}" if r.position else ""
            items.append(item(f"{r.time:%H:%M}  {r.label}{'' if r.live else ' (scheduled)'}{where}", r))
        if not upcoming:
            items.append(rumps.MenuItem("No departures"))
        if upcoming or on_the_way:
            items.append(rumps.MenuItem("Click a train to follow it"))
        items += [rumps.MenuItem(f"⚠︎ {w[:80]}") for w in board.warnings]
        return items

    def _trip_items(self, trip: travel.Trip, now: datetime) -> list[rumps.MenuItem]:
        dest = trip.destination or short(trip.dest)
        live = "" if trip.live else "  (scheduled)"
        items: list[rumps.MenuItem] = []

        if not trip.departed(now):
            mins = _minutes(trip.departure - now)
            self._countdown(mins)
            items.append(rumps.MenuItem(f"Your train: {trip.departure:%H:%M} {trip.line}{live}"))
            if trip.planned:
                items.append(rumps.MenuItem(f"Arrives {dest} {trip.arrival:%H:%M}"))
        elif not trip.planned:
            # Departed, but the planner hasn't answered yet: no real arrival or stops.
            self._set_title("🚆 …", "🚆 …", "…")
            items.append(rumps.MenuItem(f"Finding your {trip.departure:%H:%M} {trip.line}…"))
        elif trip.arrived(now):
            self._set_title(f"🚆 {short(dest)}", "🚆 ✓", "✓")
            items.append(rumps.MenuItem(f"Arrived at {dest}"))
        else:
            mins = _minutes(trip.arrival - now)
            kind, station = trip.where()
            pie = trip.progress()  # ◔ ◑ ◕ on the way to the next station, GPS only
            if kind == "at":  # stopped there
                full = compact = f"@{station}"
            else:
                full, compact = f"{pie or kind} {station}", f"{pie} {station}" if pie else station
            self._set_title(
                f"🚆 {mins}m · {full}" if station else f"🚆 {mins}m",
                f"🚆 {mins}m · {compact}" if station else f"🚆 {mins}m",
                f"{mins}m",
            )
            items.append(rumps.MenuItem(f"{trip.line} to {dest}: arrive {trip.arrival:%H:%M}{live}"))
            if trip.between() and kind == "next":
                items.append(rumps.MenuItem(f"Between {trip.between()}{'  ' + pie if pie else ''}"))
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

    @rumps.events.before_start.register
    def _ctrl_c_quits() -> None:
        _quit_on_ctrl_c(app)

    @rumps.events.before_start.register
    def _spinner() -> None:
        # In all run loop modes, so it keeps turning while the menu is open
        # (rumps timers stop then).
        timer = NSTimer.timerWithTimeInterval_repeats_block_(SPIN_EVERY, True, lambda _t: app._animate())
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        app._spin_timer = timer  # keep a reference

    app.redraw(None)  # starts inactive: show it, request nothing
    app.run()


def _quit_on_ctrl_c(app: CommuteApp) -> None:
    """Make Ctrl-C in the terminal quit the app, even with a dialog open.

    rumps' own Ctrl-C handling only runs in the run loop's default mode, so it
    is ignored while a dialog (which runs its own modal loop) or the menu is
    open. Python only runs a signal handler between bytecodes on the main
    thread, so a tick in every run loop mode gives it the chance to.
    """

    def on_ctrl_c(signum, frame) -> None:
        # `uv run` forwards Ctrl-C to the app, which also gets it from the
        # terminal: act on the first one only.
        if app.quitting:
            return
        app.quitting = True
        # If a clean quit gets stuck, leave anyway.
        threading.Timer(2.0, os._exit, args=(130,)).start()
        nsapp = NSApplication.sharedApplication()
        if nsapp.modalWindow() is not None:
            nsapp.abortModal()  # change_stations sees app.quitting and quits
        else:
            rumps.quit_application()

    signal.signal(signal.SIGINT, on_ctrl_c)
    tick = NSTimer.timerWithTimeInterval_repeats_block_(0.3, True, lambda _timer: None)
    NSRunLoop.currentRunLoop().addTimer_forMode_(tick, NSRunLoopCommonModes)
    app._ctrl_c_tick = tick  # keep a reference
