"""MCP glue shared by the tool modules: annotation profiles and error mapping.

Every tool uses exactly one of three annotation profiles, defined only here:

READ_ONLY  - reads the workspace, Git or GitHub; never writes or executes
             repository code. (14 tools)
WRITES_FILES - modifies workspace files from an explicit caller-supplied patch
             (apply_patch, revert_patch).
EXECUTES_CODE - may run the repository's own test code
             (run_tests, validate_repository).

openWorldHint semantics: True means the tool may cause interactions or effects
whose external targets DevPilot does not bound. Only EXECUTES_CODE qualifies,
because test code can reach anything. All network access DevPilot performs
itself goes to one fixed HTTPS host (api.github.com, GET only, three
endpoints), which is bounded, so the GitHub-reading tools and
investigate_repository are openWorldHint=False.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from devpilot_mcp.workspace import WorkspaceError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

# Not read-only: writes files. Destructive: a patch can delete files. Bounded to the workspace.
WRITES_FILES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

# Runs repository test code, which may have side effects and may reach external systems.
EXECUTES_CODE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def as_tool_error(exc: Exception) -> ToolError:
    """Convert an anticipated failure into a message safe to show the client."""
    if isinstance(exc, WorkspaceError):
        return ToolError(str(exc))
    # OSError messages can include absolute paths; only expose the reason.
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "unknown error"
    return ToolError(f"Filesystem error: {reason}.")
