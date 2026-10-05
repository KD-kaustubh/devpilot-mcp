"""`devpilot-ui`: start DevPilot Studio on http://127.0.0.1:<port> and open it in the browser."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from devpilot_ui.config import HOST, ConfigError, load_ui_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="devpilot-ui", description="DevPilot Studio: a local web UI for DevPilot MCP.")
    parser.add_argument("--port", type=int, help="port on 127.0.0.1 (default 8765 or DEVPILOT_UI_PORT)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args()

    try:
        settings = load_ui_settings(args.port)
    except ConfigError as exc:
        print(f"devpilot-ui: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        import uvicorn

        from devpilot_ui.app import create_app
    except ImportError:
        print('devpilot-ui: the UI extras are missing. Install them with: pip install "devpilot-mcp[ui]"', file=sys.stderr)
        sys.exit(1)

    url = f"http://{HOST}:{settings.port}"
    print(f"DevPilot Studio  ->  {url}")
    print(f"Workspace: {settings.workspace_root}")
    print(f"AI chat: {'on (' + settings.model + ')' if settings.ai_configured else 'off (set AIPIPE_TOKEN); quick actions still work'}")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(settings), host=HOST, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
