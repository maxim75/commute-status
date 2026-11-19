
macOS menu bar countdown to the next train from Engadine towards Town Hall,
using the tfnsw MCP server.

Put your key in `.env` in the project root (see `.env.example`):

```
TFNSW_API_KEY=your-key
```

Then run:

```bash
uv run commute-status
```

Optional settings, in `.env` or the environment: `COMMUTE_STATION` (default
`Engadine Station`), `COMMUTE_TOWARDS` (default `Town Hall`),
`COMMUTE_RETURN_AFTER` (default `14:00`), `TFNSW_MCP_URL`.


### Wait and travelling modes

- **Stations:** choose **Change stations…** in the dropdown and type the
  station you start from and the one you travel to. They're checked with
  TfNSW (the station must exist and have direct trains between them), saved to
  `~/.config/commute-status/stations.json` and used straight away. Without a
  saved choice, `COMMUTE_STATION` / `COMMUTE_TOWARDS` from `.env` apply.
- **Direction by time of day:** Engadine → Town Hall until 14:00, then
  Town Hall → Engadine until midnight (`COMMUTE_RETURN_AFTER`, empty to
  disable). A train you are following keeps its direction across the switch.
- **Wait mode** (default): `🚆 11 min` counts down to the next train. The
  dropdown lists trains that already left but haven't reached Town Hall yet
  (with their arrival time), then the next departures, each with where the
  train is now (`at Heathcote`, or `◑ Sydenham` when about halfway there from
  the previous stop; ◔ ◑ ◕ are rough quarters) from the Sydney Trains GPS feed.
  The line is mapped once per direction, from the terminus the trains come
  from (worked out automatically, e.g. Waterfall for Engadine → Town Hall; set
  `COMMUTE_LINE_START` / `COMMUTE_RETURN_LINE_START` to override) to the
  destination, and cached in `~/.cache/commute-status/line.json`.
- **Pick a train**: click a departure in the dropdown. Until it leaves, the
  countdown follows that train.
- **Travelling mode** (from departure): `🚆 23 min · ◑ Sutherland` shows
  minutes to the destination and the next station (or `at Sutherland` while
  stopped). The pie shows roughly how far the train is from the previous stop
  (◔ ◑ ◕), handy on long express runs. Position comes from the train's GPS in
  the Sydney Trains feed, falling back to a timetable estimate, which shows
  `next Sutherland` without a pie; the arrival time is live from the trip
  planner. The dropdown adds the arrival time, where the train is and the
  stops ahead.
- It returns to wait mode two minutes after arrival, or via **Stop travelling**.

Code layout: `app.py` (menu bar), `tfnsw.py` (MCP calls), `travel.py`
(following a train), `line.py` (train positions on the list), `config.py` (settings).

### Running with uvx or as an installed tool

No checkout needed; uvx fetches the code (and Python 3.15 if missing) from GitHub:

```bash
uvx --from git+https://github.com/maxim75/commute-status commute-status
```

or install it once with
`uv tool install git+https://github.com/maxim75/commute-status`, then run
`commute-status`. From a local checkout, use its path instead of the `git+` URL.

Run that way, the code lives in uv's cache, so put your settings in
`~/.config/commute-status/.env` (or `$XDG_CONFIG_HOME/commute-status/.env`):

```bash
mkdir -p ~/.config/commute-status && echo 'TFNSW_API_KEY=your-key' > ~/.config/commute-status/.env
```

To pick up new commits, add `--refresh` to the uvx command, or run
`uv tool upgrade commute-status` for the installed tool.
Settings are read in this order, first wins: environment variables, `.env` in
the current folder, `.env` in the project folder, `~/.config/commute-status/.env`.
uvx rebuilds automatically when files under `src/` change (see `cache-keys` in
`pyproject.toml`).

### Crowded menu bar

When the menu bar is full, macOS hides new items behind the notch without
saying so. The app checks where its item ended up: while hidden it switches to
shorter titles (`🚆 7 min` → `🚆 7m` → `7m`), and if even that doesn't fit it
shows a notification once. To make room, hold ⌘ and drag other icons out of the
menu bar, or ⌘-drag the 🚆 item further right; that position is remembered
across restarts. Set `COMMUTE_TITLE=full|compact|minimal` to fix the title
length instead of letting it adjust (default `auto`).
