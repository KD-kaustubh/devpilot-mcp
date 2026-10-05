"""Settings for DevPilot Studio, from the environment and the project's .env file.

The workspace is resolved exactly as the MCP server resolves it (devpilot_mcp.config).
The AI settings default to AI Pipe's OpenAI-compatible endpoint; any other
OpenAI-compatible provider works by changing the base URL, key and model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from devpilot_mcp.config import ConfigError, load_settings

__all__ = ["ConfigError", "UISettings", "load_ui_settings"]

DEFAULT_BASE_URL = "https://aipipe.org/openai/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # never bound to other interfaces: the UI reads the user's own repository


@dataclass(frozen=True)
class UISettings:
    workspace_root: Path
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    port: int = DEFAULT_PORT
    dev_origin: str | None = None  # the Vite dev server's origin, allowed only when set explicitly
    api_key: str | None = field(default=None, repr=False)  # never printed, logged or sent to the browser

    @property
    def ai_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def allowed_origins(self) -> frozenset[str]:
        origins = {f"http://{HOST}:{self.port}", f"http://localhost:{self.port}"}
        if self.dev_origin:
            origins.add(self.dev_origin)
        return frozenset(origins)


def load_ui_settings(port: int | None = None) -> UISettings:
    """Load settings; raises ConfigError if the workspace does not exist or a value is invalid."""
    workspace_root = load_settings().workspace_root  # also loads the project's .env
    raw_port = os.environ.get("DEVPILOT_UI_PORT", "").strip()
    try:
        resolved_port = port if port is not None else int(raw_port or DEFAULT_PORT)
    except ValueError:
        raise ConfigError("DEVPILOT_UI_PORT must be a number.") from None
    if not 1024 <= resolved_port <= 65535:
        raise ConfigError("The UI port must be between 1024 and 65535.")
    return UISettings(
        workspace_root=workspace_root,
        base_url=os.environ.get("DEVPILOT_UI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        model=os.environ.get("DEVPILOT_UI_MODEL", "").strip() or DEFAULT_MODEL,
        port=resolved_port,
        dev_origin=os.environ.get("DEVPILOT_UI_DEV_ORIGIN", "").strip() or None,
        api_key=(os.environ.get("DEVPILOT_UI_API_KEY") or os.environ.get("AIPIPE_TOKEN") or "").strip() or None,
    )
