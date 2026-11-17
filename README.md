
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
