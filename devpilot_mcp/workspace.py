"""Workspace sandboxing.

Every filesystem access made by a tool must go through `Workspace.resolve`,
which guarantees the final path (after resolving `..` and symlinks) stays
inside the configured workspace root.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# Matches Windows drive prefixes such as "C:" or "D:\" on any platform.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class WorkspaceError(Exception):
    """Base class for errors caused by a request against the workspace."""


class PathOutsideWorkspaceError(WorkspaceError):
    """The requested path is absolute or escapes the workspace root."""


class PathNotFoundError(WorkspaceError):
    """The requested path does not exist inside the workspace."""


class Workspace:
    """A directory tree that tools are allowed to read from."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, path: str) -> Path:
        """Turn a workspace-relative path into a safe absolute path.

        `""` and `"."` refer to the workspace root. Both `/` and `\\` are
        accepted as separators.

        Raises:
            PathOutsideWorkspaceError: If `path` is absolute, carries a drive
                letter, contains a null byte, or resolves outside the root.
        """
        path = (path or "").strip()
        if "\x00" in path:
            raise PathOutsideWorkspaceError("Path contains a null byte.")

        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or _DRIVE_PREFIX.match(normalized):
            raise PathOutsideWorkspaceError(
                f"Absolute paths are not allowed: '{path}'. Use a path relative to the workspace root."
            )

        # resolve() collapses '..' and follows symlinks, so the containment
        # check below also catches symlinks that point outside the workspace.
        candidate = (self.root / PurePosixPath(normalized)).resolve()
        if not candidate.is_relative_to(self.root):
            raise PathOutsideWorkspaceError(f"Path escapes the workspace root: '{path}'.")
        return candidate

    def contains(self, path: Path) -> bool:
        """Return True if `path` (after resolving symlinks) lies inside the root."""
        return path.resolve().is_relative_to(self.root)

    def relative(self, path: Path) -> str:
        """Return `path` relative to the workspace root, using `/` separators."""
        rel = path.relative_to(self.root).as_posix()
        return "." if rel in ("", ".") else rel
