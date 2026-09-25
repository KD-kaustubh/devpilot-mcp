"""Configuration loading for DevPilot MCP.

Settings come from environment variables, optionally loaded from a `.env`
file in the project root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ENV_VAR = "DEVPILOT_WORKSPACE"
DEFAULT_WORKSPACE = "./workspace"


class ConfigError(Exception):
    """Raised when the server configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the MCP server."""

    workspace_root: Path


def load_settings() -> Settings:
    """Load settings from the environment (and `.env`, if present).

    A relative `DEVPILOT_WORKSPACE` is resolved against the project root rather
    than the current working directory, so the server behaves the same no matter
    which directory a client (such as MCP Inspector) launches it from.

    Raises:
        ConfigError: If the workspace root does not exist or is not a directory.
    """
    load_dotenv(PROJECT_ROOT / ".env")

    raw_root = os.environ.get(WORKSPACE_ENV_VAR, "").strip() or DEFAULT_WORKSPACE
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    root = root.resolve()

    if not root.is_dir():
        raise ConfigError(
            f"Workspace root '{root}' does not exist or is not a directory. "
            f"Set {WORKSPACE_ENV_VAR} to an existing directory."
        )
    return Settings(workspace_root=root)
