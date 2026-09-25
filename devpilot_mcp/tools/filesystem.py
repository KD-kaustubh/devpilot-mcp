"""Read-only filesystem tools: list_directory, read_file, search_files.

The plain functions hold the logic and raise `WorkspaceError` subclasses;
`register` exposes them as MCP tools and turns those errors into `ToolError`s
so the client sees a clear message.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

MAX_READ_BYTES = 1_000_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 100
MAX_LINE_PREVIEW_CHARS = 200
BINARY_SNIFF_BYTES = 8192

# Directories that are almost never useful to search and can be very large.
SKIPPED_SEARCH_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox"}
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


class NotATextFileError(WorkspaceError):
    """The file is binary, not valid UTF-8, or too large to return."""


# --- Result models (these also become the tools' output schemas) -------------


class DirectoryEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory", "other"]
    size_bytes: int | None = None


class DirectoryListing(BaseModel):
    path: str
    entries: list[DirectoryEntry]
    total_entries: int
    truncated: bool


class FileContent(BaseModel):
    path: str
    size_bytes: int
    line_count: int
    content: str


class SearchMatch(BaseModel):
    path: str
    line_number: int
    line: str


class SearchResults(BaseModel):
    query: str
    matches: list[SearchMatch]
    files_with_matches: list[str]
    files_searched: int
    truncated: bool


# --- Helpers -----------------------------------------------------------------


def _looks_binary(sample: bytes) -> bool:
    """Heuristic used by git and grep: a NUL byte means binary."""
    return b"\x00" in sample


def _read_text(path: Path) -> str:
    """Read a file as UTF-8 text, raising NotATextFileError if it isn't text."""
    data = path.read_bytes()
    if _looks_binary(data[:BINARY_SNIFF_BYTES]):
        raise NotATextFileError("File appears to be binary.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NotATextFileError("File is not valid UTF-8 text.") from exc


def _entry_type(path: Path) -> Literal["file", "directory", "other"]:
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


# --- Tool logic --------------------------------------------------------------


def list_directory(workspace: Workspace, path: str = ".") -> DirectoryListing:
    """List the immediate children of a workspace directory (directories first)."""
    target = workspace.resolve(path)
    if not target.exists():
        raise PathNotFoundError(f"Directory not found: '{path}'.")
    if not target.is_dir():
        raise WorkspaceError(f"Not a directory: '{path}'. Use read_file to read files.")

    entries: list[DirectoryEntry] = []
    for child in target.iterdir():
        # Hide symlinks that point outside the workspace rather than leak their targets.
        if child.is_symlink() and not workspace.contains(child):
            continue
        kind = _entry_type(child)
        entries.append(
            DirectoryEntry(
                name=child.name,
                path=workspace.relative(target / child.name),
                type=kind,
                size_bytes=child.stat().st_size if kind == "file" else None,
            )
        )

    entries.sort(key=lambda e: (e.type != "directory", e.name.lower()))
    return DirectoryListing(
        path=workspace.relative(target),
        entries=entries[:MAX_LIST_ENTRIES],
        total_entries=len(entries),
        truncated=len(entries) > MAX_LIST_ENTRIES,
    )


def read_file(workspace: Workspace, path: str) -> FileContent:
    """Read a UTF-8 text file from the workspace."""
    if not (path or "").strip() or path.strip() == ".":
        raise WorkspaceError("A file path is required.")
    target = workspace.resolve(path)
    if not target.exists():
        raise PathNotFoundError(f"File not found: '{path}'.")
    if not target.is_file():
        raise WorkspaceError(f"Not a file: '{path}'. Use list_directory to browse directories.")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise NotATextFileError(f"File is too large to read ({size:,} bytes; limit is {MAX_READ_BYTES:,}).")

    content = _read_text(target)
    return FileContent(
        path=workspace.relative(target),
        size_bytes=size,
        line_count=len(content.splitlines()),
        content=content,
    )


def search_files(workspace: Workspace, query: str) -> SearchResults:
    """Case-insensitive substring search across all text files in the workspace.

    Binary files, files larger than MAX_SEARCH_FILE_BYTES, and common
    dependency/VCS directories are skipped. Symlinks are not followed.
    """
    if not query or not query.strip():
        raise WorkspaceError("Search query must not be empty.")
    needle = query.lower()

    matches: list[SearchMatch] = []
    files_with_matches: list[str] = []
    files_searched = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(workspace.root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_SEARCH_DIRS)
        for filename in sorted(filenames):
            file_path = Path(dirpath) / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = _read_text(file_path)
            except (OSError, NotATextFileError):
                continue

            files_searched += 1
            rel_path = workspace.relative(file_path)
            found_in_file = False
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle not in line.lower():
                    continue
                if len(matches) >= MAX_SEARCH_MATCHES:
                    truncated = True
                    break
                found_in_file = True
                matches.append(
                    SearchMatch(path=rel_path, line_number=line_number, line=line.strip()[:MAX_LINE_PREVIEW_CHARS])
                )
            if found_in_file:
                files_with_matches.append(rel_path)
            if truncated:
                break
        if truncated:
            break

    return SearchResults(
        query=query,
        matches=matches,
        files_with_matches=files_with_matches,
        files_searched=files_searched,
        truncated=truncated,
    )


# --- MCP registration --------------------------------------------------------


def _as_tool_error(exc: Exception) -> ToolError:
    """Convert an anticipated failure into a message safe to show the client."""
    if isinstance(exc, WorkspaceError):
        return ToolError(str(exc))
    # OSError messages can include absolute paths; only expose the reason.
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "unknown error"
    return ToolError(f"Filesystem error: {reason}.")


def register(server: MCPServer, workspace: Workspace) -> None:
    """Expose the filesystem tools on `server`, sandboxed to `workspace`."""

    @server.tool(name="list_directory", annotations=READ_ONLY)
    def list_directory_tool(path: str = ".") -> DirectoryListing:
        """List files and directories at a path in the workspace.

        Args:
            path: Directory path relative to the workspace root. Use "." or "" for the root.
        """
        try:
            return list_directory(workspace, path)
        except (WorkspaceError, OSError) as exc:
            raise _as_tool_error(exc) from exc

    @server.tool(name="read_file", annotations=READ_ONLY)
    def read_file_tool(path: str) -> FileContent:
        """Read a UTF-8 text file from the workspace. Binary and very large files are rejected.

        Args:
            path: File path relative to the workspace root, e.g. "src/app.py".
        """
        try:
            return read_file(workspace, path)
        except (WorkspaceError, OSError) as exc:
            raise _as_tool_error(exc) from exc

    @server.tool(name="search_files", annotations=READ_ONLY)
    def search_files_tool(query: str) -> SearchResults:
        """Search all text files in the workspace for a case-insensitive substring.

        Returns each matching line with its file path and line number.

        Args:
            query: Text to search for.
        """
        try:
            return search_files(workspace, query)
        except (WorkspaceError, OSError) as exc:
            raise _as_tool_error(exc) from exc
