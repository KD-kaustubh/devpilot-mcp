"""search_code: developer-oriented text search over source files.

Unlike search_files (every text file, whole workspace), search_code:

- only looks at source/config files, recognized by extension or well-known name;
- also skips build output and cache directories (dist, build, coverage, ...);
- can be limited to a subdirectory or a single file;
- uses smart case: case-insensitive unless the query contains an uppercase letter.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from devpilot_mcp.text_search import SKIPPED_DIRS, iter_files, scan_files
from devpilot_mcp.tools.common import READ_ONLY, as_tool_error
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

MAX_CODE_MATCHES = 100
MAX_CODE_FILE_BYTES = 512_000  # Hand-written source files are rarely larger; bigger ones are usually generated.

SOURCE_EXTENSIONS = frozenset(
    {
        # General-purpose languages
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt", ".kts", ".scala",
        ".groovy", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".fs", ".rb",
        ".php", ".swift", ".m", ".mm", ".dart", ".lua", ".pl", ".pm", ".r", ".jl", ".ex", ".exs", ".erl",
        ".hs", ".clj", ".elm", ".zig",
        # Web and UI
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
        # Shell and scripting
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
        # Data, schema, query and config-as-code
        ".sql", ".graphql", ".gql", ".proto", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
        ".tf", ".hcl", ".gradle", ".cmake",
    }
)  # fmt: skip

SOURCE_FILENAMES = frozenset(
    {"Dockerfile", "Makefile", "CMakeLists.txt", "Gemfile", "Rakefile", "Jenkinsfile", "Vagrantfile", "Procfile"}
)

# Generated or vendored files that match SOURCE_EXTENSIONS but are just noise.
EXCLUDED_FILE_PATTERNS = ("*.min.js", "*.min.css", "package-lock.json")

# Build output and tool caches, on top of the VCS/dependency dirs shared with search_files.
GENERATED_DIRS = frozenset(
    {"build", "dist", "target", ".next", ".nuxt", "coverage", "htmlcov", ".ruff_cache", ".gradle", ".eggs", "*.egg-info"}
)


class CodeMatch(BaseModel):
    file: str
    line: int
    text: str


class CodeSearchResults(BaseModel):
    query: str
    path: str
    case_sensitive: bool
    matches: list[CodeMatch]
    files_searched: int
    files_skipped: int
    truncated: bool


def is_source_file(path: Path) -> bool:
    """Return True if `path` looks like a source or config file worth searching."""
    name = path.name
    if any(fnmatch(name, pattern) for pattern in EXCLUDED_FILE_PATTERNS):
        return False
    return name in SOURCE_FILENAMES or path.suffix.lower() in SOURCE_EXTENSIONS


def search_code(
    workspace: Workspace,
    query: str,
    path: str = ".",
    *,
    max_matches: int = MAX_CODE_MATCHES,
    max_file_bytes: int = MAX_CODE_FILE_BYTES,
) -> CodeSearchResults:
    """Search source files under `path` for lines containing `query`.

    Raises:
        WorkspaceError: If the query is empty, or `path` is outside the
            workspace, does not exist, or is a file that isn't source code.
    """
    if not query or not query.strip():
        raise WorkspaceError("Search query must not be empty.")

    target = workspace.resolve(path)
    if not target.exists():
        raise PathNotFoundError(f"Search path not found: '{path}'.")
    if target.is_file() and not is_source_file(target):
        raise WorkspaceError(f"Not a source file: '{path}'. Use search_files to search any text file.")

    case_sensitive = any(ch.isupper() for ch in query)
    if case_sensitive:
        line_matches = lambda line: query in line  # noqa: E731
    else:
        needle = query.lower()
        line_matches = lambda line: needle in line.lower()  # noqa: E731

    files = (f for f in iter_files(target, SKIPPED_DIRS | GENERATED_DIRS) if is_source_file(f))
    scan = scan_files(workspace, files, line_matches, max_matches=max_matches, max_file_bytes=max_file_bytes)

    return CodeSearchResults(
        query=query,
        path=workspace.relative(target),
        case_sensitive=case_sensitive,
        matches=[CodeMatch(file=m.path, line=m.line_number, text=m.line) for m in scan.matches],
        files_searched=scan.files_searched,
        files_skipped=scan.files_skipped,
        truncated=scan.truncated,
    )


def register(server: MCPServer, workspace: Workspace) -> None:
    """Expose search_code on `server`, sandboxed to `workspace`."""

    @server.tool(name="search_code", annotations=READ_ONLY)
    def search_code_tool(query: str, path: str = ".") -> CodeSearchResults:
        """Search source code files for a text snippet, e.g. a function, class or variable name.

        Only source/config files are searched (by extension, e.g. .py, .ts, .java, .go, .json, .yaml);
        dependency, VCS and build-output directories are skipped. Matching is case-insensitive unless
        the query contains an uppercase letter. Each match gives the file path (relative to the
        workspace root), 1-based line number and the line's text. At most 100 matches are returned;
        `truncated` is true when more exist, so narrow the query or path.

        Args:
            query: Text to find, e.g. "authenticate" or "class User".
            path: Directory or file to search, relative to the workspace root. Defaults to the whole workspace.
        """
        try:
            return search_code(workspace, query, path)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
