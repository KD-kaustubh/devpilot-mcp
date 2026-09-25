"""MCP glue shared by the tool modules."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from devpilot_mcp.workspace import WorkspaceError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def as_tool_error(exc: Exception) -> ToolError:
    """Convert an anticipated failure into a message safe to show the client."""
    if isinstance(exc, WorkspaceError):
        return ToolError(str(exc))
    # OSError messages can include absolute paths; only expose the reason.
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "unknown error"
    return ToolError(f"Filesystem error: {reason}.")
