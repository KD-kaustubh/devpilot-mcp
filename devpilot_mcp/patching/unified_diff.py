"""A strict unified-diff parser and in-memory hunk application.

Supported: standard text hunks (`--- a/x`, `+++ b/x`, `@@ -l,c +l,c @@`) for
modifying, creating (`--- /dev/null`) and deleting (`+++ /dev/null`) files,
optionally preceded by the Git headers `diff --git`, `index`, `new file mode`
and `deleted file mode`. Rejected: binary patches, renames/copies, mode changes,
quoted paths and any other unrecognized line.

Hunks must match exactly at the line they name (no offset search, no fuzz).
Error messages quote line numbers only, never patch or file contents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from devpilot_mcp.workspace import WorkspaceError

DEV_NULL = "/dev/null"
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_INDEX_LINE = re.compile(r"^index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}(?: [0-7]{6})?$")
_FILE_MODE = re.compile(r"^(?:new|deleted) file mode (100644|100755)$")
_UNSUPPORTED = {
    "GIT binary patch": "Binary patches are not supported.",
    "Binary files ": "Binary patches are not supported.",
    "old mode ": "File mode changes are not supported.",
    "new mode ": "File mode changes are not supported.",
    "rename from ": "Renames are not supported; delete and create the file in two sections instead.",
    "rename to ": "Renames are not supported; delete and create the file in two sections instead.",
    "copy from ": "Copies are not supported.",
    "copy to ": "Copies are not supported.",
    "similarity index ": "Renames and copies are not supported.",
    "dissimilarity index ": "Rewrites with similarity data are not supported.",
}

ChangeKind = Literal["modified", "created", "deleted"]


class PatchFormatError(WorkspaceError):
    """The patch is not a supported, well-formed unified diff."""


class PatchConflictError(WorkspaceError):
    """The patch does not match the current file content (stale patch)."""


@dataclass
class HunkLine:
    kind: Literal[" ", "-", "+"]
    text: str
    eol: bool = True  # False when followed by "\ No newline at end of file"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine]
    line_number: int  # of the @@ header, for error messages


@dataclass
class FilePatch:
    old_path: str | None  # None for /dev/null
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def kind(self) -> ChangeKind:
        if self.old_path is None:
            return "created"
        if self.new_path is None:
            return "deleted"
        return "modified"

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.kind == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.kind == "-")


# --- Parsing -------------------------------------------------------------------


def _header_path(value: str, line_number: int) -> str:
    """The path from a ---/+++ header value (a timestamp after a tab is ignored)."""
    value = value.split("\t", 1)[0]
    if not value or value != value.strip():
        raise PatchFormatError(f"Line {line_number}: file header has an empty or padded path.")
    if value.startswith('"'):
        raise PatchFormatError(f"Line {line_number}: quoted paths are not supported.")
    return value


def _strip_prefixes(old: str, new: str, line_number: int) -> tuple[str | None, str | None]:
    """Drop Git's a/ and b/ prefixes; /dev/null becomes None."""
    old_path = None if old == DEV_NULL else old
    new_path = None if new == DEV_NULL else new
    if old_path is None and new_path is None:
        raise PatchFormatError(f"Line {line_number}: both sides of a file header are /dev/null.")
    if old_path is not None and new_path is not None:
        if old_path.startswith("a/") and new_path.startswith("b/"):
            old_path, new_path = old_path[2:], new_path[2:]
        elif old_path.startswith("a/") or new_path.startswith("b/"):
            raise PatchFormatError(f"Line {line_number}: inconsistent a/ and b/ path prefixes.")
        if old_path != new_path:
            raise PatchFormatError(
                f"Line {line_number}: old and new paths differ; renames are not supported."
            )
    else:
        if old_path is not None and old_path.startswith("a/"):
            old_path = old_path[2:]
        if new_path is not None and new_path.startswith("b/"):
            new_path = new_path[2:]
    return old_path, new_path


def _parse_hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    header = _HUNK_HEADER.match(lines[index])
    if not header:
        raise PatchFormatError(f"Line {index + 1}: malformed hunk header.")
    old_start, old_count = int(header[1]), int(header[2] if header[2] is not None else 1)
    new_start, new_count = int(header[3]), int(header[4] if header[4] is not None else 1)
    if (old_start == 0 and old_count != 0) or (new_start == 0 and new_count != 0):
        raise PatchFormatError(f"Line {index + 1}: hunk header has an invalid line range.")
    if old_count == 0 and new_count == 0:
        raise PatchFormatError(f"Line {index + 1}: empty hunk.")

    hunk = Hunk(old_start, old_count, new_start, new_count, [], index + 1)
    old_seen = new_seen = 0
    index += 1
    while old_seen < old_count or new_seen < new_count:
        if index >= len(lines):
            raise PatchFormatError(f"Line {hunk.line_number}: hunk is shorter than its header says.")
        line = lines[index]
        if line.startswith("\\"):
            if not hunk.lines:
                raise PatchFormatError(f"Line {index + 1}: 'No newline' marker without a preceding line.")
            hunk.lines[-1].eol = False
            index += 1
            continue
        if line == "":
            kind, text = " ", ""  # an empty context line whose leading space was stripped
        elif line[0] in " -+":
            kind, text = line[0], line[1:]
        else:
            raise PatchFormatError(f"Line {index + 1}: unexpected line inside a hunk.")
        if kind in " -":
            old_seen += 1
        if kind in " +":
            new_seen += 1
        if old_seen > old_count or new_seen > new_count:
            raise PatchFormatError(f"Line {index + 1}: hunk is longer than its header says.")
        hunk.lines.append(HunkLine(kind, text))  # type: ignore[arg-type]
        index += 1
    if index < len(lines) and lines[index].startswith("\\"):
        hunk.lines[-1].eol = False
        index += 1
    return hunk, index


def parse_patch(patch: str) -> list[FilePatch]:
    """Parse a unified diff into per-file patches, rejecting anything unsupported."""
    lines = [line[:-1] if line.endswith("\r") else line for line in patch.split("\n")]
    if patch.endswith("\n"):
        lines.pop()  # the text after the final newline is not a line
    files: list[FilePatch] = []
    git_header: tuple[str, int] | None = None
    mode_line: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == "":
            index += 1
            continue
        for prefix, message in _UNSUPPORTED.items():
            if line.startswith(prefix):
                raise PatchFormatError(f"Line {index + 1}: {message}")
        if line.startswith("diff --git "):
            if git_header is not None:
                raise PatchFormatError(
                    f"Line {git_header[1]}: 'diff --git' section has no text hunks "
                    "(empty files and metadata-only changes are not supported)."
                )
            git_header, mode_line = (line, index + 1), None
            index += 1
        elif git_header is not None and _INDEX_LINE.match(line):
            index += 1
        elif git_header is not None and _FILE_MODE.match(line):
            mode_line = line
            index += 1
        elif line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PatchFormatError(f"Line {index + 1}: '---' header is not followed by '+++'.")
            old_path, new_path = _strip_prefixes(
                _header_path(line[4:], index + 1), _header_path(lines[index + 1][4:], index + 2), index + 1
            )
            file_patch = FilePatch(old_path, new_path)
            index += 2
            while index < len(lines) and lines[index].startswith("@@"):
                hunk, index = _parse_hunk(lines, index)
                file_patch.hunks.append(hunk)
            if not file_patch.hunks:
                raise PatchFormatError(f"Line {index}: file section for '{file_patch.path}' has no hunks.")
            _check_git_metadata(file_patch, git_header, mode_line)
            files.append(file_patch)
            git_header, mode_line = None, None
        else:
            raise PatchFormatError(f"Line {index + 1}: unexpected content; only unified diff sections are accepted.")
    if git_header is not None:
        raise PatchFormatError(f"Line {git_header[1]}: 'diff --git' section has no text hunks.")
    if not files:
        raise PatchFormatError("The patch contains no file sections.")
    return files


def _check_git_metadata(file_patch: FilePatch, git_header: tuple[str, int] | None, mode_line: str | None) -> None:
    if git_header is None:
        if mode_line is not None:
            raise PatchFormatError("File mode metadata without a 'diff --git' header.")
        return
    line, number = git_header
    if line != f"diff --git a/{file_patch.path} b/{file_patch.path}":
        raise PatchFormatError(f"Line {number}: 'diff --git' header does not match the file header paths.")
    if mode_line is not None:
        expected = "new" if file_patch.kind == "created" else "deleted" if file_patch.kind == "deleted" else None
        if expected is None or not mode_line.startswith(expected):
            raise PatchFormatError(f"Line {number}: file mode metadata does not match the change.")


# --- In-memory application -----------------------------------------------------

Line = tuple[str, str]  # (text, end of line: "\n", "\r\n" or "")


def split_lines(text: str) -> list[Line]:
    """Split on "\\n" only (keeping CRLF distinct), preserving a missing final newline."""
    parts = text.split("\n")
    lines: list[Line] = [(p[:-1], "\r\n") if p.endswith("\r") else (p, "\n") for p in parts[:-1]]
    if parts[-1]:
        lines.append((parts[-1], ""))
    return lines


def join_lines(lines: list[Line]) -> str:
    return "".join(text + eol for text, eol in lines)


def _dominant_eol(lines: list[Line]) -> str:
    crlf = sum(1 for _, eol in lines if eol == "\r\n")
    lf = sum(1 for _, eol in lines if eol == "\n")
    return "\r\n" if crlf > lf else "\n"


def apply_hunks(file_patch: FilePatch, original: list[Line]) -> list[Line]:
    """Apply every hunk exactly where it says, or raise PatchConflictError."""
    path = file_patch.path
    eol = _dominant_eol(original)
    result: list[Line] = []
    cursor = 0
    for number, hunk in enumerate(file_patch.hunks, start=1):
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if start < cursor:
            raise PatchFormatError(f"Hunk {number} of '{path}' overlaps or precedes the previous hunk.")
        if start + hunk.old_count > len(original):
            raise PatchConflictError(
                f"Hunk {number} of '{path}' does not match the current file: it extends past the end of the file."
            )
        result.extend(original[cursor:start])
        position = start
        for line in hunk.lines:
            if line.kind == "+":
                result.append((line.text, eol if line.eol else ""))
                continue
            text, current_eol = original[position]
            if text != line.text or (current_eol == "") != (not line.eol):
                raise PatchConflictError(
                    f"Hunk {number} of '{path}' does not match the current file at line {position + 1}; "
                    "the patch is stale or was made against different content."
                )
            if line.kind == " ":
                result.append((text, current_eol))
            position += 1
        cursor = position
    result.extend(original[cursor:])
    if any(eol == "" for _, eol in result[:-1]):
        raise PatchFormatError(f"The patch for '{path}' places a line without a newline before the end of the file.")
    return result
