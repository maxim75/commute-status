
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

Optional settings, in `.env` or the environment: `COMMUTE_STATION` (default `Engadine Station`),
`COMMUTE_STOP_ID` (skip name lookup), `COMMUTE_TOWARDS` (default `Town Hall`),
`TFNSW_MCP_URL`.

### Home Assistant location

Set `HASS_URL` and `HASS_TOKEN` (a long-lived access token) and the app picks
what to show from the zone your `person` entity is in:

- `home`: next train from Engadine towards Town Hall (🚆)
- another zone, e.g. `Work`: next trip from that zone back to Engadine (🏠)
- anywhere else: no countdown (🚆 –)

`HASS_PERSON` picks the person entity; it defaults to the first one.

### Wait and travelling modes

- **Wait mode** (default): `🚆 11 min` counts down to the next train. The
  dropdown lists the next departures.
- **Pick a train**: click a departure in the dropdown. Until it leaves, the
  countdown follows that train.
- **Travelling mode** (from departure): `🚆 23 min · next Sutherland` shows
  minutes to the destination and the next station (or `at Sutherland` while
  stopped). Position comes from the train's GPS in the Sydney Trains feed,
  falling back to a timetable estimate; the arrival time is live from the trip
  planner. The dropdown adds the arrival time, where the train is and the
  stops ahead.
- It returns to wait mode two minutes after arrival, or via **Stop travelling**.

Code layout: `app.py` (menu bar), `tfnsw.py` (MCP calls), `travel.py`
(following a train), `hass.py` (Home Assistant zone), `config.py` (settings).

### Running with uvx or as an installed tool

```bash
uvx --from /Users/maksym/code/commute-status commute-status
```

or `uv tool install /Users/maksym/code/commute-status`, then `commute-status`.
Run that way, the code lives in uv's cache, so put your settings in
`~/.config/commute-status/.env` (or `$XDG_CONFIG_HOME/commute-status/.env`).
Settings are read in this order, first wins: environment variables, `.env` in
the current folder, `.env` in the project folder, `~/.config/commute-status/.env`.
uvx rebuilds automatically when files under `src/` change (see `cache-keys` in
`pyproject.toml`).
