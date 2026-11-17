"""macOS menu bar countdown to the next train, via the tfnsw MCP server.

See app.py for how wait mode and travelling mode behave.
"""


def main() -> None:
    from .app import run

    run()
