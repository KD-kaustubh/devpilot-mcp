"""MCP server construction and entry point."""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

from devpilot_mcp import __version__
from devpilot_mcp.config import ConfigError, load_settings
from devpilot_mcp.tools import code_search, filesystem, git, repository
from devpilot_mcp.workspace import Workspace

SERVER_INSTRUCTIONS = (
    "DevPilot gives read-only access to a single workspace directory. "
    "All paths are relative to the workspace root; absolute paths and '..' escapes are rejected. "
    "Start with analyze_repository() for an overview, list_directory('.') to explore, search_code to locate "
    "code (search_files for any text file), and read_file to inspect it. "
    "git_status, git_log, git_diff and git_branch give read-only Git information when the workspace is a Git repository."
)


def create_server(workspace: Workspace) -> MCPServer:
    """Build an MCP server with all DevPilot tools bound to `workspace`."""
    server = MCPServer(name="DevPilot MCP", version=__version__, instructions=SERVER_INSTRUCTIONS)
    filesystem.register(server, workspace)
    code_search.register(server, workspace)
    repository.register(server, workspace)
    git.register(server, workspace)
    return server


def main() -> None:
    """Run the server over stdio using settings from the environment."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        # stdout carries the MCP protocol, so diagnostics go to stderr.
        print(f"devpilot-mcp: {exc}", file=sys.stderr)
        sys.exit(1)

    create_server(Workspace(settings.workspace_root)).run("stdio")


if __name__ == "__main__":
    main()
