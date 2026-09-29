"""Validate, apply and revert explicit patches inside the workspace.

Pipeline for apply_patch (nothing is written until every step has passed for
every file in the patch):

1. size and format: bounded patch size, strict unified-diff parsing;
2. resource limits: files, hunks, added and deleted lines;
3. every target path: the Workspace boundary plus write-specific rules (no
   links anywhere on the path, normalized components, protected files);
4. expected content: each hunk must match the current file exactly, and
   files must be UTF-8 text within the size limits;
5. commit: all new contents are staged in temporary files next to their
   targets, then swapped in with renames; any failure rolls back everything.

The applied change is recorded in an in-memory registry (original bytes plus
a SHA-256 of each resulting file) so revert_patch can restore it, but only if
no file has been changed since.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from devpilot_mcp.patching.unified_diff import (
    ChangeKind,
    PatchConflictError,
    PatchFormatError,
    apply_hunks,
    join_lines,
    parse_patch,
    split_lines,
)
from devpilot_mcp.text_search import BINARY_SNIFF_BYTES
from devpilot_mcp.tools.investigation import SECRET_FILE_PATTERNS
from devpilot_mcp.workspace import Workspace, WorkspaceError

# --- Limits ------------------------------------------------------------------

MAX_PATCH_BYTES = 256_000
MAX_FILES_PER_PATCH = 20
MAX_HUNKS_PER_PATCH = 100
MAX_ADDITIONS = 2_000
MAX_DELETIONS = 2_000
MAX_FILE_BYTES = 1_000_000  # an existing file the patch reads
MAX_RESULT_FILE_BYTES = 1_000_000  # a file the patch produces
MAX_REGISTERED_CHANGES = 50
MAX_REGISTRY_BYTES = 50_000_000  # original contents kept for reverts

LIMITS = {
    "max_patch_bytes": MAX_PATCH_BYTES,
    "max_files_per_patch": MAX_FILES_PER_PATCH,
    "max_hunks_per_patch": MAX_HUNKS_PER_PATCH,
    "max_additions": MAX_ADDITIONS,
    "max_deletions": MAX_DELETIONS,
    "max_file_bytes": MAX_FILE_BYTES,
    "max_result_file_bytes": MAX_RESULT_FILE_BYTES,
    "max_registered_changes": MAX_REGISTERED_CHANGES,
}

# Same secret-file convention as investigate_repository, but for writes only
# the .env.example template is exempt.
PROTECTED_EXCEPTIONS = (".env.example",)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_UTF8_BOM = b"\xef\xbb\xbf"
_STAGING_PREFIX = ".devpilot-staging-"
CHANGE_ID_PATTERN = r"^chg_[0-9a-f]{16}$"
_CHANGE_ID = re.compile(CHANGE_ID_PATTERN)


class PatchLimitError(WorkspaceError):
    """The patch exceeds a resource limit."""


class PatchPathError(WorkspaceError):
    """A patch target path is not an acceptable write location."""


class ProtectedPathError(WorkspaceError):
    """A patch targets a protected file (.git, secrets, keys, credentials)."""


class PatchWriteError(WorkspaceError):
    """Writing failed; the message says whether everything was rolled back."""


class UnknownChangeError(WorkspaceError):
    """No active change with the given change_id."""


class RevertConflictError(WorkspaceError):
    """A file changed after the patch was applied, so the revert was refused."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Target validation ---------------------------------------------------------


def is_protected(rel: str) -> bool:
    """True for anything inside .git and for likely secret, key or credential files."""
    parts = [part.lower() for part in rel.split("/")]
    if ".git" in parts:
        return True
    name = parts[-1]
    return name not in PROTECTED_EXCEPTIONS and any(fnmatchcase(name, pattern) for pattern in SECRET_FILE_PATTERNS)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def resolve_target(workspace: Workspace, rel: str) -> Path:
    """Validate a patch target and return its absolute path.

    On top of Workspace.resolve (absolute, drive, UNC, null byte and escape
    checks), writes require a normalized '/'-separated path, no link anywhere
    on the way, and a path that is not protected.
    """
    workspace.resolve(rel)
    if "\\" in rel or ":" in rel or any(ord(c) < 32 for c in rel):
        raise PatchPathError("Patch paths must use '/' separators and must not contain ':' or control characters.")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise PatchPathError(f"Patch path '{rel}' must be a normalized relative path (no '.', '..' or '//').")
        if part != part.rstrip(". ") or part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise PatchPathError(f"Patch path '{rel}' contains a name Windows cannot store safely.")
    if is_protected(rel):
        raise ProtectedPathError(f"'{rel}' is protected (.git, environment, key or credential file) and cannot be patched.")

    target = workspace.root.joinpath(*parts)
    current = workspace.root
    for part in parts:
        current = current / part
        if _is_link(current):
            raise PatchPathError(f"'{rel}' goes through a symbolic link or junction; patches never follow links.")
    if workspace.resolve(rel) != target:
        raise PatchPathError(f"'{rel}' does not resolve to its literal location inside the workspace.")
    return target


def _check_parents(workspace: Workspace, target: Path, rel: str) -> None:
    """For a new file, every existing ancestor directory must be a real directory."""
    parent = target.parent
    while parent != workspace.root and not parent.exists():
        parent = parent.parent
    if not parent.is_dir():
        raise PatchConflictError(f"Cannot create '{rel}': a parent path exists and is not a directory.")


def _load_text(path: Path, rel: str) -> tuple[bytes, list, bool]:
    """Current bytes, lines and BOM flag of an existing UTF-8 text file."""
    if not path.is_file():
        raise PatchConflictError(f"'{rel}' does not exist as a regular file, so the patch cannot modify or delete it.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise PatchLimitError(f"'{rel}' is larger than {MAX_FILE_BYTES:,} bytes and cannot be patched.")
    data = path.read_bytes()
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise PatchFormatError(f"'{rel}' is a binary file; only text files can be patched.")
    bom = data.startswith(_UTF8_BOM)
    try:
        text = data[len(_UTF8_BOM) if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError:
        raise PatchFormatError(f"'{rel}' is not valid UTF-8 text; only UTF-8 files can be patched.") from None
    return data, split_lines(text), bom


# --- Preparation (validation only; nothing is written) ------------------------


@dataclass(frozen=True)
class PlannedFile:
    rel: str
    path: Path
    kind: ChangeKind
    original: bytes | None  # None for a created file
    result: bytes | None  # None for a deleted file
    additions: int
    deletions: int


def prepare_patch(workspace: Workspace, patch: object) -> tuple[list[PlannedFile], str]:
    """Validate the whole patch and compute every resulting file in memory."""
    if not isinstance(patch, str):
        raise PatchFormatError("patch must be a string.")
    if not patch.strip():
        raise PatchFormatError("The patch is empty.")
    size = len(patch.encode("utf-8"))
    if size > MAX_PATCH_BYTES:
        raise PatchLimitError(f"The patch is {size:,} bytes; the limit is {MAX_PATCH_BYTES:,}.")
    if "\x00" in patch:
        raise PatchFormatError("The patch contains a null byte.")

    files = parse_patch(patch)
    hunks = sum(len(f.hunks) for f in files)
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)
    for name, value, limit in (
        ("files", len(files), MAX_FILES_PER_PATCH),
        ("hunks", hunks, MAX_HUNKS_PER_PATCH),
        ("added lines", additions, MAX_ADDITIONS),
        ("deleted lines", deletions, MAX_DELETIONS),
    ):
        if value > limit:
            raise PatchLimitError(f"The patch has {value:,} {name}; the limit is {limit:,}.")

    planned: list[PlannedFile] = []
    seen: set[str] = set()
    for file_patch in files:
        rel = file_patch.path
        target = resolve_target(workspace, rel)
        key = os.path.normcase(str(target))
        if key in seen:
            raise PatchFormatError(f"'{rel}' appears more than once in the patch.")
        seen.add(key)

        if file_patch.kind == "created":
            if os.path.lexists(target):
                raise PatchConflictError(f"Cannot create '{rel}': it already exists.")
            _check_parents(workspace, target, rel)
            original, lines, bom = None, [], False
        else:
            original, lines, bom = _load_text(target, rel)

        new_lines = apply_hunks(file_patch, lines)
        if file_patch.kind == "deleted":
            if new_lines:
                raise PatchConflictError(
                    f"The deletion of '{rel}' does not remove every line of the current file; the patch is stale."
                )
            result = None
        else:
            result = (_UTF8_BOM if bom else b"") + join_lines(new_lines).encode("utf-8")
            if len(result) > MAX_RESULT_FILE_BYTES:
                raise PatchLimitError(f"The patched '{rel}' would exceed {MAX_RESULT_FILE_BYTES:,} bytes.")
        planned.append(
            PlannedFile(rel, target, file_patch.kind, original, result, file_patch.additions, file_patch.deletions)
        )
    return planned, sha256(patch.encode("utf-8"))


# --- Atomic commit -------------------------------------------------------------


@dataclass(frozen=True)
class _Write:
    rel: str
    path: Path
    before: bytes | None  # what must be there again after a rollback (None: no file)
    after: bytes | None  # what to write (None: delete)


def _reserve_temp(directory: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".tmp", dir=directory)
    os.close(fd)
    return Path(name)


def _write_temp(directory: Path, content: bytes, mode_from: Path | None) -> Path:
    fd, name = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode_from is not None and mode_from.exists():
            shutil.copymode(mode_from, name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _make_parents(directory: Path) -> list[Path]:
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir()
    return list(reversed(missing))  # outermost first


def _remove_empty_dirs(directories: list[Path]) -> None:
    for directory in reversed(directories):  # innermost first
        try:
            if directory.is_dir() and not _is_link(directory) and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def _commit(writes: list[_Write]) -> list[Path]:
    """Apply every write or none of them. Returns the directories that had to be created.

    Stage: each new content is fully written (and fsynced) to a temporary file
    beside its target. Swap: targets are replaced by renames, and deleted files
    are renamed aside rather than removed. If anything fails, completed steps
    are undone in reverse order and temporary files are removed.
    """
    created_dirs: list[Path] = []
    staged: dict[int, Path] = {}
    moved_aside: dict[int, Path] = {}
    done: list[int] = []
    try:
        for index, write in enumerate(writes):
            if write.after is not None:
                created_dirs += _make_parents(write.path.parent)
                staged[index] = _write_temp(write.path.parent, write.after, mode_from=write.path)
        for index, write in enumerate(writes):
            if write.after is None:
                moved_aside[index] = _reserve_temp(write.path.parent)
                os.replace(write.path, moved_aside[index])
            else:
                os.replace(staged[index], write.path)
                del staged[index]  # only once the rename succeeded, so a failure still cleans it up
            done.append(index)
    except OSError as exc:
        failed = _rollback(writes, done, moved_aside)
        for leftover in [*staged.values(), *moved_aside.values()]:
            leftover.unlink(missing_ok=True)
        _remove_empty_dirs(created_dirs)
        reason = type(exc).__name__
        if failed:
            raise PatchWriteError(
                f"Writing failed ({reason}) and these files could not be restored: {', '.join(failed)}. "
                "Check them before continuing."
            ) from None
        raise PatchWriteError(f"Writing failed ({reason}); every file was restored, so nothing changed.") from None

    for aside in moved_aside.values():
        aside.unlink(missing_ok=True)
    return created_dirs


def _rollback(writes: list[_Write], done: list[int], moved_aside: dict[int, Path]) -> list[str]:
    failed = []
    for index in reversed(done):
        write = writes[index]
        try:
            if write.after is None:  # was deleted: move it back
                os.replace(moved_aside.pop(index), write.path)
            elif write.before is None:  # was created: remove it
                write.path.unlink()
            else:  # was modified: write the previous bytes back
                os.replace(_write_temp(write.path.parent, write.before, mode_from=write.path), write.path)
        except OSError:
            failed.append(write.rel)
    return failed


# --- Change registry -----------------------------------------------------------


@dataclass(frozen=True)
class RecordedFile:
    rel: str
    kind: ChangeKind
    original: bytes | None
    result_sha256: str | None
    result_size: int | None
    additions: int
    deletions: int


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    patch_sha256: str
    applied_at: str
    files: tuple[RecordedFile, ...]
    created_dirs: tuple[str, ...]

    @property
    def stored_bytes(self) -> int:
        return sum(len(f.original or b"") for f in self.files)


class ChangeRegistry:
    """In-memory record of applied changes for one server process (lost on restart).

    Bounded by MAX_REGISTERED_CHANGES and MAX_REGISTRY_BYTES; the oldest
    changes are forgotten first, after which they can no longer be reverted.
    `lock` serializes every apply and revert.
    """

    def __init__(self, max_changes: int = MAX_REGISTERED_CHANGES, max_bytes: int = MAX_REGISTRY_BYTES) -> None:
        self.lock = threading.Lock()
        self._changes: OrderedDict[str, ChangeRecord] = OrderedDict()
        self._max_changes = max_changes
        self._max_bytes = max_bytes

    def new_id(self) -> str:
        while True:
            change_id = f"chg_{secrets.token_hex(8)}"
            if change_id not in self._changes:
                return change_id

    def add(self, record: ChangeRecord) -> tuple[bool, list[str]]:
        """Store a record, evicting the oldest as needed. Returns (stored, evicted ids)."""
        if record.stored_bytes > self._max_bytes:
            return False, []
        evicted = []
        while self._changes and (
            len(self._changes) >= self._max_changes
            or sum(r.stored_bytes for r in self._changes.values()) + record.stored_bytes > self._max_bytes
        ):
            evicted.append(self._changes.popitem(last=False)[0])
        self._changes[record.change_id] = record
        return True, evicted

    def get(self, change_id: str) -> ChangeRecord | None:
        return self._changes.get(change_id)

    def remove(self, change_id: str) -> None:
        self._changes.pop(change_id, None)

    def ids(self) -> list[str]:
        return list(self._changes)


# --- Apply and revert ------------------------------------------------------------


@dataclass(frozen=True)
class AppliedChange:
    record: ChangeRecord
    reversible: bool
    evicted: list[str]


def apply_patch(workspace: Workspace, registry: ChangeRegistry, patch: object) -> AppliedChange:
    """Validate the complete patch, then apply it atomically and record it for revert."""
    with registry.lock:
        planned, patch_hash = prepare_patch(workspace, patch)
        writes = [_Write(p.rel, p.path, p.original, p.result) for p in planned]
        created_dirs = _commit(writes)
        record = ChangeRecord(
            change_id=registry.new_id(),
            patch_sha256=patch_hash,
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            files=tuple(
                RecordedFile(
                    rel=p.rel,
                    kind=p.kind,
                    original=p.original,
                    result_sha256=sha256(p.result) if p.result is not None else None,
                    result_size=len(p.result) if p.result is not None else None,
                    additions=p.additions,
                    deletions=p.deletions,
                )
                for p in planned
            ),
            created_dirs=tuple(workspace.relative(d) for d in created_dirs),
        )
        stored, evicted = registry.add(record)
        return AppliedChange(record, stored, evicted)


def revert_patch(workspace: Workspace, registry: ChangeRegistry, change_id: object) -> ChangeRecord:
    """Restore the exact pre-patch content of every file in a change, or change nothing."""
    if not isinstance(change_id, str) or not _CHANGE_ID.match(change_id):
        raise UnknownChangeError("change_id must be 'chg_' followed by 16 lowercase hex digits.")
    with registry.lock:
        record = registry.get(change_id)
        if record is None:
            raise UnknownChangeError(
                f"No active change '{change_id}'. Only changes applied by this server process can be reverted, "
                "each only once, and the registry is cleared when the server restarts."
            )
        writes: list[_Write] = []
        changed: list[str] = []
        for recorded in record.files:
            try:
                path = resolve_target(workspace, recorded.rel)
            except WorkspaceError:
                changed.append(recorded.rel)
                continue
            if recorded.kind == "deleted":
                if os.path.lexists(path):
                    changed.append(recorded.rel)
                    continue
                writes.append(_Write(recorded.rel, path, before=None, after=recorded.original))
                continue
            if not path.is_file():
                changed.append(recorded.rel)
                continue
            current = path.read_bytes()
            if len(current) != recorded.result_size or sha256(current) != recorded.result_sha256:
                changed.append(recorded.rel)
                continue
            after = None if recorded.kind == "created" else recorded.original
            writes.append(_Write(recorded.rel, path, before=current, after=after))

        if changed:
            raise RevertConflictError(
                f"Refusing to revert {change_id}: {', '.join(sorted(changed))} changed after the patch was applied. "
                "No file was modified."
            )
        _commit(writes)
        _remove_empty_dirs([workspace.root / d for d in record.created_dirs])
        registry.remove(change_id)
        return record
