"""Python syntax validation by parsing only.

Each .py/.pyi file is compiled to an AST with `compile(..., ast.PyCF_ONLY_AST)`;
nothing is executed or imported, so this finds syntax errors, not runtime,
import or type errors. The grammar is that of the Python running DevPilot.
"""

from __future__ import annotations

import ast
import sys
import warnings
from pathlib import Path

from pydantic import BaseModel

from devpilot_mcp.text_search import BINARY_SNIFF_BYTES, SKIPPED_DIRS, iter_files
from devpilot_mcp.tools.code_search import GENERATED_DIRS
from devpilot_mcp.workspace import PathNotFoundError, Workspace

MAX_SYNTAX_FILES = 5_000
MAX_SYNTAX_FILE_BYTES = 1_000_000
MAX_REPORTED_ISSUES = 50


class SyntaxIssue(BaseModel):
    path: str
    line: int | None
    column: int | None
    message: str


class SyntaxReport(BaseModel):
    python_version: str
    files_checked: int
    files_skipped: int
    error_count: int
    errors: list[SyntaxIssue]
    warning_count: int
    warnings: list[SyntaxIssue]
    complete: bool


def _check_file(path: Path, rel: str) -> tuple[SyntaxIssue | None, list[SyntaxIssue]]:
    """Parse one file. Returns (error or None, syntax warnings)."""
    source = path.read_bytes()
    found: list[SyntaxIssue] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            # Bytes, so PEP 263 encoding declarations are honoured. PyCF_ONLY_AST: parse, never run.
            compile(source, rel, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
            error = None
        except SyntaxError as exc:  # includes IndentationError and TabError
            error = SyntaxIssue(path=rel, line=exc.lineno, column=exc.offset, message=f"{type(exc).__name__}: {exc.msg}")
        except (ValueError, UnicodeDecodeError) as exc:
            error = SyntaxIssue(path=rel, line=None, column=None, message=f"{type(exc).__name__}: could not decode source")
        except (RecursionError, MemoryError) as exc:
            error = SyntaxIssue(path=rel, line=None, column=None, message=f"{type(exc).__name__}: too complex to parse")
    for warning in caught:
        if issubclass(warning.category, SyntaxWarning):
            found.append(SyntaxIssue(path=rel, line=warning.lineno or None, column=None, message=str(warning.message)))
    return error, found


def check_python_syntax(workspace: Workspace) -> SyntaxReport:
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")
    errors: list[SyntaxIssue] = []
    syntax_warnings: list[SyntaxIssue] = []
    checked = skipped = 0
    complete = True
    for path in iter_files(root, SKIPPED_DIRS | GENERATED_DIRS):
        if path.suffix not in (".py", ".pyi") or path.is_symlink() or not path.is_file():
            continue
        if checked + skipped >= MAX_SYNTAX_FILES:
            complete = False
            break
        rel = workspace.relative(path)
        try:
            if path.stat().st_size > MAX_SYNTAX_FILE_BYTES:
                skipped += 1
                continue
            with path.open("rb") as handle:
                if b"\x00" in handle.read(BINARY_SNIFF_BYTES):
                    skipped += 1
                    continue
            error, file_warnings = _check_file(path, rel)
        except OSError:
            skipped += 1
            continue
        checked += 1
        if error is not None:
            errors.append(error)
        syntax_warnings.extend(file_warnings)
    return SyntaxReport(
        python_version=".".join(map(str, sys.version_info[:3])),
        files_checked=checked,
        files_skipped=skipped,
        error_count=len(errors),
        errors=errors[:MAX_REPORTED_ISSUES],
        warning_count=len(syntax_warnings),
        warnings=syntax_warnings[:MAX_REPORTED_ISSUES],
        complete=complete,
    )
