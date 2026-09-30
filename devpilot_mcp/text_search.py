"""Line-by-line text scanning shared by the search tools.

Callers resolve their starting path through `Workspace.resolve` first; this
module only walks *down* from that point, never follows symlinks or Windows
junctions, never returns the content of secret files, and reports paths
relative to the workspace root.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from devpilot_mcp.sensitive import is_secret_file_name, is_sensitive_path
from devpilot_mcp.workspace import Workspace, WorkspaceError

_JUNCTION_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)

BINARY_SNIFF_BYTES = 8192
MAX_LINE_PREVIEW_CHARS = 200

# Directories that are almost never useful to search and can be very large.
# Entries are fnmatch patterns matched against the directory name.
SKIPPED_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox"}
)

# Build output and tool caches, skipped on top of SKIPPED_DIRS by the source-oriented scans
# (search_code, analyze_repository, investigate_repository, syntax validation).
GENERATED_DIRS = frozenset(
    {"build", "dist", "target", ".next", ".nuxt", "coverage", "htmlcov", ".ruff_cache", ".gradle", ".eggs", "*.egg-info"}
)


class NotATextFileError(WorkspaceError):
    """The file is binary, not valid UTF-8, or too large to return."""


class SensitivePathError(WorkspaceError):
    """The path is inside .git or is a likely secret file; its content is never returned."""


def ensure_not_sensitive(workspace: Workspace, target: Path, requested: str) -> None:
    """Refuse a read or search target that is, or resolves to, a .git or secret-file path."""
    if is_sensitive_path(requested) or is_sensitive_path(workspace.relative(target)):
        raise SensitivePathError(
            f"'{requested}' is protected (.git internals, or an environment, key or credential file); "
            "DevPilot never returns its contents."
        )


def is_link(path: Path) -> bool:
    """True for a symlink or a Windows directory junction (which `is_symlink` does not report)."""
    if path.is_symlink():
        return True
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) == _JUNCTION_TAG
    except OSError:
        return False


def read_text(path: Path) -> str:
    """Read a file as UTF-8 text, raising NotATextFileError if it isn't text.

    A NUL byte in the first few KB marks a file as binary (the heuristic git
    and grep use).
    """
    data = path.read_bytes()
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise NotATextFileError("File appears to be binary.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NotATextFileError("File is not valid UTF-8 text.") from exc


def iter_files(start: Path, skipped_dirs: Iterable[str] = SKIPPED_DIRS) -> Iterator[Path]:
    """Yield files under `start` in a stable order, pruning `skipped_dirs`.

    If `start` is itself a file, only that file is yielded. The skip list
    applies to subdirectories only, so explicitly searching inside e.g.
    `node_modules/pkg` still works. Symlinked and junction directories are
    never entered (`os.walk` alone would enter junctions).
    """
    if start.is_file():
        yield start
        return
    patterns = tuple(skipped_dirs)
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if not any(fnmatch(d, p) for p in patterns) and not is_link(Path(dirpath) / d)
        )
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


@dataclass
class LineMatch:
    path: str
    line_number: int
    line: str


@dataclass
class ScanResult:
    matches: list[LineMatch] = field(default_factory=list)
    files_with_matches: list[str] = field(default_factory=list)
    files_searched: int = 0
    files_skipped: int = 0
    truncated: bool = False


def scan_files(
    workspace: Workspace,
    files: Iterable[Path],
    line_matches: Callable[[str], bool],
    *,
    max_matches: int,
    max_file_bytes: int,
) -> ScanResult:
    """Collect lines for which `line_matches` is true, up to `max_matches`.

    Symlinks are ignored. Likely secret files (see `sensitive.py`) are never
    read. They are counted in `files_skipped`, as are files that are too large,
    binary, not UTF-8 or unreadable. When a match beyond `max_matches` exists,
    scanning stops and `truncated` is set.
    """
    result = ScanResult()
    for file_path in files:
        if file_path.is_symlink() or not file_path.is_file():
            continue
        if is_secret_file_name(file_path.name):
            result.files_skipped += 1
            continue
        try:
            if file_path.stat().st_size > max_file_bytes:
                result.files_skipped += 1
                continue
            text = read_text(file_path)
        except (OSError, NotATextFileError):
            result.files_skipped += 1
            continue

        result.files_searched += 1
        rel_path = workspace.relative(file_path)
        found_in_file = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line_matches(line):
                continue
            if len(result.matches) >= max_matches:
                result.truncated = True
                break
            found_in_file = True
            result.matches.append(LineMatch(rel_path, line_number, line.strip()[:MAX_LINE_PREVIEW_CHARS]))
        if found_in_file:
            result.files_with_matches.append(rel_path)
        if result.truncated:
            break
    return result
