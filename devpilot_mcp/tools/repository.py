"""analyze_repository: a deterministic, factual overview of the workspace.

Everything is derived from file paths and from fields declared in dependency
manifests. Nothing in the repository is executed or written. Conclusions that
rest on naming conventions rather than hard facts (entry points, frameworks)
are grouped under `heuristics` so a client never mistakes them for facts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from devpilot_mcp.text_search import SKIPPED_DIRS, NotATextFileError, iter_files, read_text
from devpilot_mcp.tools.code_search import GENERATED_DIRS
from devpilot_mcp.tools.common import READ_ONLY, as_tool_error
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: TOML manifests are listed but not parsed.
    tomllib = None  # type: ignore[assignment]

MAX_FILES_SCANNED = 20_000
MAX_LIST_ITEMS = 50
MAX_DIRECTORY_DEPTH = 2
MAX_MANIFESTS_PARSED = 20
MAX_MANIFEST_BYTES = 512_000

# --- Classification tables ---------------------------------------------------

LANGUAGES_BY_EXTENSION = {
    ".py": "Python", ".pyi": "Python", ".pyw": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".gradle": "Gradle", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".hxx": "C++",
    ".cs": "C#", ".fs": "F#", ".fsx": "F#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".m": "Objective-C", ".mm": "Objective-C++", ".dart": "Dart", ".lua": "Lua", ".pl": "Perl", ".pm": "Perl",
    ".r": "R", ".jl": "Julia", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hrl": "Erlang",
    ".hs": "Haskell", ".clj": "Clojure", ".elm": "Elm", ".zig": "Zig",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS", ".sass": "Sass", ".less": "Less",
    ".vue": "Vue", ".svelte": "Svelte",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Fish", ".ps1": "PowerShell",
    ".psm1": "PowerShell", ".bat": "Batchfile", ".cmd": "Batchfile",
    ".sql": "SQL", ".graphql": "GraphQL", ".gql": "GraphQL", ".proto": "Protocol Buffers",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".xml": "XML", ".ini": "INI", ".cfg": "INI",
    ".tf": "HCL", ".hcl": "HCL", ".cmake": "CMake",
    ".md": "Markdown", ".markdown": "Markdown", ".rst": "reStructuredText", ".adoc": "AsciiDoc", ".txt": "Text",
}  # fmt: skip

LANGUAGES_BY_FILENAME = {
    "Dockerfile": "Dockerfile", "Makefile": "Makefile", "CMakeLists.txt": "CMake",
    "Gemfile": "Ruby", "Rakefile": "Ruby", "Jenkinsfile": "Groovy",
}  # fmt: skip

DOC_STEMS = frozenset(
    {"README", "CHANGELOG", "CHANGES", "HISTORY", "CONTRIBUTING", "LICENSE", "LICENCE", "COPYING",
     "CODE_OF_CONDUCT", "SECURITY", "AUTHORS", "NOTICE"}
)  # fmt: skip
DOC_EXTENSIONS = frozenset({"", ".md", ".markdown", ".rst", ".txt", ".adoc"})
DOC_DIR_NAMES = frozenset({"docs", "doc"})

DEPENDENCY_MANIFEST_PATTERNS = (
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements*.txt", "environment.yml",
    "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml", "*.csproj",
)  # fmt: skip

LOCK_FILE_NAMES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "uv.lock",
     "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "pubspec.lock"}
)  # fmt: skip

CONFIG_PATTERNS = (
    # Python tooling
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "mypy.ini", ".flake8", "ruff.toml", ".ruff.toml",
    ".python-version",
    # JavaScript/TypeScript tooling
    "tsconfig*.json", "jsconfig.json", ".eslintrc*", "eslint.config.*", ".prettierrc*", "prettier.config.*",
    ".babelrc", "babel.config.*", "vite.config.*", "webpack.config.*", "jest.config.*", "vitest.config.*",
    "next.config.*", "nuxt.config.*", "angular.json", ".nvmrc",
    # Containers, build and deployment
    "Dockerfile", "*.dockerfile", ".dockerignore", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "Makefile", "Procfile", "netlify.toml", "vercel.json",
    # CI
    "Jenkinsfile", ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml",
    # Repository and environment
    ".gitignore", ".gitattributes", ".editorconfig", ".pre-commit-config.yaml", ".tool-versions",
    ".env.example", ".env.sample", ".env.template",
)  # fmt: skip

TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec"})
TEST_FILE_PATTERNS = (
    "test_*.py", "*_test.py",
    "*.test.js", "*.test.jsx", "*.test.mjs", "*.test.ts", "*.test.tsx",
    "*.spec.js", "*.spec.jsx", "*.spec.mjs", "*.spec.ts", "*.spec.tsx",
    "*_test.go", "*Test.java", "*Tests.java", "*Test.kt", "*_spec.rb", "*Tests.cs",
)  # fmt: skip

ENTRY_POINT_FILENAMES = frozenset(
    {"main.py", "app.py", "server.py", "manage.py", "wsgi.py", "asgi.py", "__main__.py",
     "index.js", "index.mjs", "index.ts", "index.jsx", "index.tsx", "main.js", "main.ts", "main.jsx", "main.tsx",
     "server.js", "server.ts", "app.js", "app.ts",
     "main.go", "main.rs", "Main.java", "Program.cs", "main.c", "main.cpp"}
)  # fmt: skip

# Declared dependency name -> framework. Python names are PEP 503-normalized.
PYTHON_FRAMEWORKS = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "starlette": "Starlette", "tornado": "Tornado",
    "pyramid": "Pyramid", "aiohttp": "aiohttp", "sanic": "Sanic", "streamlit": "Streamlit", "gradio": "Gradio",
    "pytest": "pytest", "mcp": "MCP Python SDK",
}  # fmt: skip
JAVASCRIPT_FRAMEWORKS = {
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "@angular/core": "Angular",
    "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "express": "Express", "fastify": "Fastify", "koa": "Koa",
    "@nestjs/core": "NestJS", "electron": "Electron", "jest": "Jest", "mocha": "Mocha", "vitest": "Vitest",
    "@modelcontextprotocol/sdk": "MCP TypeScript SDK",
}  # fmt: skip
RUST_FRAMEWORKS = {"actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket", "warp": "Warp"}
# Manifests without a stdlib parser are matched on exact dependency coordinates.
TEXT_MANIFEST_MARKERS = {
    "go.mod": {"github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo": "Echo",
               "github.com/gofiber/fiber": "Fiber", "github.com/go-chi/chi": "chi"},
    "pom.xml": {"org.springframework.boot": "Spring Boot", "io.quarkus": "Quarkus", "io.micronaut": "Micronaut"},
}  # fmt: skip
TEXT_MANIFEST_MARKERS["build.gradle"] = TEXT_MANIFEST_MARKERS["pom.xml"]
TEXT_MANIFEST_MARKERS["build.gradle.kts"] = TEXT_MANIFEST_MARKERS["pom.xml"]

# --- Result models (these also become the tool's output schema) --------------


class DirectorySummary(BaseModel):
    path: str
    file_count: int


class DetectedTests(BaseModel):
    directories: list[str]
    files: list[str]
    file_count: int


class EntryPointCandidate(BaseModel):
    file: str
    kind: Literal["manifest", "filename"]
    detail: str


class FrameworkIndicator(BaseModel):
    name: str
    file: str
    evidence: str


class Heuristics(BaseModel):
    possible_entry_points: list[EntryPointCandidate]
    framework_indicators: list[FrameworkIndicator]


class RepositoryAnalysis(BaseModel):
    total_files: int
    scan_complete: bool
    languages: dict[str, int]
    unclassified_files: int
    directories: list[DirectorySummary]
    documentation_files: list[str]
    configuration_files: list[str]
    dependency_manifests: list[str]
    lock_files: list[str]
    tests: DetectedTests
    heuristics: Heuristics
    truncated_fields: list[str]
    warnings: list[str]


# --- Path classification -----------------------------------------------------


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    # Case-sensitive so results are identical on every OS.
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def detect_language(rel_path: str) -> str | None:
    """Return the language for a file from its name or extension, or None."""
    path = PurePosixPath(rel_path)
    return LANGUAGES_BY_FILENAME.get(path.name) or LANGUAGES_BY_EXTENSION.get(path.suffix.lower())


def is_documentation(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    suffix = path.suffix.lower()
    if path.stem.upper() in DOC_STEMS and suffix in DOC_EXTENSIONS:
        return True
    in_docs_dir = any(part.lower() in DOC_DIR_NAMES for part in path.parts[:-1])
    return in_docs_dir and suffix in DOC_EXTENSIONS and suffix != ""


def is_configuration(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    if _matches(path.name, CONFIG_PATTERNS):
        return True
    # CI workflows and repository automation under .github/
    return path.parts[:1] == (".github",) and path.suffix in (".yml", ".yaml")


def is_dependency_manifest(rel_path: str) -> bool:
    return _matches(PurePosixPath(rel_path).name, DEPENDENCY_MANIFEST_PATTERNS)


def enclosing_test_directory(rel_path: str) -> str | None:
    """Return the outermost conventional test directory containing the file, if any."""
    parts = PurePosixPath(rel_path).parts[:-1]
    for index, part in enumerate(parts):
        if part.lower() in TEST_DIR_NAMES:
            return "/".join(parts[: index + 1])
    return None


def is_test_file(rel_path: str) -> bool:
    return _matches(PurePosixPath(rel_path).name, TEST_FILE_PATTERNS)


def _by_depth(paths: list[str]) -> list[str]:
    """Shallow paths first, then alphabetical, so capping keeps the most relevant."""
    return sorted(paths, key=lambda p: (p.count("/"), p))


# --- Manifest inspection -----------------------------------------------------


def _normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_requirement_name(requirement: str) -> str | None:
    """'Flask[async]>=2.0 ; python_version>"3"' -> 'flask'."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return _normalize_python_name(match.group(1)) if match else None


def _requirements_txt_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):  # skip options such as -r, -e, --index-url
            if name := _python_requirement_name(line):
                names.add(name)
    return names


def _pyproject_names(data: dict[str, Any]) -> set[str]:
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))
    names = {n for r in requirements if isinstance(r, str) and (n := _python_requirement_name(r))}

    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
    names |= {_normalize_python_name(name) for table in tables for name in table}
    return names


def _pyproject_scripts(data: dict[str, Any]) -> list[str]:
    project = data.get("project", {})
    tables = {
        "[project.scripts]": project.get("scripts", {}),
        "[project.gui-scripts]": project.get("gui-scripts", {}),
        "[tool.poetry.scripts]": data.get("tool", {}).get("poetry", {}).get("scripts", {}),
    }
    return [f"{table} {name} = {target}" for table, entries in tables.items() for name, target in entries.items()]


def _package_json_entry_points(data: dict[str, Any]) -> list[str]:
    details = []
    if isinstance(data.get("main"), str):
        details.append(f'"main": "{data["main"]}"')
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        details.append(f'"bin": "{bin_field}"')
    elif isinstance(bin_field, dict):
        details.extend(f'"bin.{name}": "{target}"' for name, target in bin_field.items())
    start = data.get("scripts", {}).get("start") if isinstance(data.get("scripts"), dict) else None
    if isinstance(start, str):
        details.append(f'"scripts.start": "{start}"')
    return details


def _package_json_names(data: dict[str, Any]) -> set[str]:
    sections = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    return {name for section in sections if isinstance(data.get(section), dict) for name in data[section]}


def _inspect_manifest(
    workspace: Workspace, rel_path: str, warnings: list[str]
) -> tuple[list[EntryPointCandidate], list[FrameworkIndicator]]:
    """Read one manifest's declared entry points and framework dependencies.

    Only data fields are read (JSON/TOML parsing or plain text matching);
    setup.py and other executable manifests are listed but never inspected.
    """
    name = PurePosixPath(rel_path).name
    is_toml = name in ("pyproject.toml", "Pipfile", "Cargo.toml")
    if not (is_toml or name == "package.json" or fnmatchcase(name, "requirements*.txt") or name in TEXT_MANIFEST_MARKERS):
        return [], []
    if is_toml and tomllib is None:
        warnings.append(f"{rel_path}: not inspected (TOML parsing needs Python 3.11+).")
        return [], []

    path = workspace.resolve(rel_path)
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            warnings.append(f"{rel_path}: not inspected (larger than {MAX_MANIFEST_BYTES:,} bytes).")
            return [], []
        text = read_text(path)
        data: Any = tomllib.loads(text) if is_toml else json.loads(text) if name == "package.json" else None
    except (OSError, NotATextFileError, ValueError) as exc:  # TOMLDecodeError and JSONDecodeError are ValueErrors
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        warnings.append(f"{rel_path}: could not be parsed ({reason}).")
        return [], []
    if data is not None and not isinstance(data, dict):
        warnings.append(f"{rel_path}: unexpected top-level structure.")
        return [], []

    entry_details: list[str] = []
    frameworks: list[tuple[str, str]] = []  # (framework, evidence)

    def declared(names: set[str], table: dict[str, str]) -> None:
        frameworks.extend((table[n], f"declares dependency '{n}'") for n in sorted(names) if n in table)

    if name == "pyproject.toml":
        entry_details = _pyproject_scripts(data)
        declared(_pyproject_names(data), PYTHON_FRAMEWORKS)
    elif name == "Pipfile":
        tables = (data.get("packages", {}), data.get("dev-packages", {}))
        declared({_normalize_python_name(n) for table in tables for n in table}, PYTHON_FRAMEWORKS)
    elif name == "Cargo.toml":
        entry_details = [f"[[bin]] {b.get('name', '?')} = {b['path']}" for b in data.get("bin", []) if "path" in b]
        deps = {n for key in ("dependencies", "dev-dependencies") for n in data.get(key, {})}
        declared(deps, RUST_FRAMEWORKS)
    elif name == "package.json":
        entry_details = _package_json_entry_points(data)
        declared(_package_json_names(data), JAVASCRIPT_FRAMEWORKS)
    elif name in TEXT_MANIFEST_MARKERS:
        frameworks = [
            (framework, f"references '{marker}'")
            for marker, framework in TEXT_MANIFEST_MARKERS[name].items()
            if marker in text
        ]
    else:  # requirements*.txt
        declared(_requirements_txt_names(text), PYTHON_FRAMEWORKS)

    return (
        [EntryPointCandidate(file=rel_path, kind="manifest", detail=d) for d in entry_details],
        [FrameworkIndicator(name=f, file=rel_path, evidence=e) for f, e in frameworks],
    )


# --- Tool logic --------------------------------------------------------------


def analyze_repository(workspace: Workspace) -> RepositoryAnalysis:
    """Collect objective facts about the files in the workspace.

    The same Phase 1/2 ignore rules apply (VCS, dependency, cache and build
    output directories), symlinks are never followed, and at most
    MAX_FILES_SCANNED files are considered.
    """
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")

    files: list[str] = []
    scan_complete = True
    for path in iter_files(root, SKIPPED_DIRS | GENERATED_DIRS):
        if path.is_symlink() or not path.is_file():
            continue
        if len(files) >= MAX_FILES_SCANNED:
            scan_complete = False
            break
        files.append(workspace.relative(path))

    languages: Counter[str] = Counter()
    directory_counts: Counter[str] = Counter()
    for rel in files:
        if language := detect_language(rel):
            languages[language] += 1
        parents = PurePosixPath(rel).parts[:-1]
        for depth in range(1, min(len(parents), MAX_DIRECTORY_DEPTH) + 1):
            directory_counts["/".join(parents[:depth])] += 1

    truncated: list[str] = []

    def cap(field: str, items: list) -> list:
        if len(items) > MAX_LIST_ITEMS:
            truncated.append(field)
            return items[:MAX_LIST_ITEMS]
        return items

    manifests = _by_depth([f for f in files if is_dependency_manifest(f)])
    test_files = _by_depth([f for f in files if is_test_file(f)])
    test_dirs = _by_depth(sorted({d for f in files if (d := enclosing_test_directory(f))}))

    warnings: list[str] = []
    manifest_entry_points: list[EntryPointCandidate] = []
    framework_indicators: list[FrameworkIndicator] = []
    for manifest in manifests[:MAX_MANIFESTS_PARSED]:
        entry_points, indicators = _inspect_manifest(workspace, manifest, warnings)
        manifest_entry_points.extend(entry_points)
        framework_indicators.extend(indicators)
    if len(manifests) > MAX_MANIFESTS_PARSED:
        warnings.append(f"Only the first {MAX_MANIFESTS_PARSED} dependency manifests were inspected.")

    filename_entry_points = [
        EntryPointCandidate(file=f, kind="filename", detail=f"conventional entry-point filename '{PurePosixPath(f).name}'")
        for f in _by_depth([f for f in files if PurePosixPath(f).name in ENTRY_POINT_FILENAMES])
        if enclosing_test_directory(f) is None
    ]

    return RepositoryAnalysis(
        total_files=len(files),
        scan_complete=scan_complete,
        languages=dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        unclassified_files=len(files) - sum(languages.values()),
        directories=cap(
            "directories",
            [DirectorySummary(path=p, file_count=directory_counts[p]) for p in _by_depth(list(directory_counts))],
        ),
        documentation_files=cap("documentation_files", _by_depth([f for f in files if is_documentation(f)])),
        configuration_files=cap("configuration_files", _by_depth([f for f in files if is_configuration(f)])),
        dependency_manifests=cap("dependency_manifests", manifests),
        lock_files=cap("lock_files", _by_depth([f for f in files if PurePosixPath(f).name in LOCK_FILE_NAMES])),
        tests=DetectedTests(
            directories=cap("tests.directories", test_dirs),
            files=cap("tests.files", test_files),
            file_count=len(test_files),
        ),
        heuristics=Heuristics(
            possible_entry_points=cap("heuristics.possible_entry_points", manifest_entry_points + filename_entry_points),
            framework_indicators=cap("heuristics.framework_indicators", framework_indicators),
        ),
        truncated_fields=truncated,
        warnings=cap("warnings", warnings),
    )


# --- MCP registration --------------------------------------------------------


def register(server: MCPServer, workspace: Workspace) -> None:
    """Expose analyze_repository on `server`, sandboxed to `workspace`."""

    @server.tool(name="analyze_repository", annotations=READ_ONLY)
    def analyze_repository_tool() -> RepositoryAnalysis:
        """Return a deterministic, factual overview of the whole workspace repository.

        Facts (from file names/paths): total_files, languages (file counts by extension/name),
        directories (up to 2 levels deep, with recursive file counts), documentation_files,
        configuration_files, dependency_manifests, lock_files, and tests (conventional test
        directories and test-file name patterns). Nothing is executed.

        `heuristics` holds convention-based guesses, not confirmed facts: possible_entry_points
        (entry points declared in manifests such as [project.scripts] or package.json "main"/"bin",
        plus conventional filenames like main.py or index.ts) and framework_indicators (frameworks
        declared as dependencies in manifests).

        Dependency, VCS, cache and build-output directories are skipped. Each list holds at most
        50 items, shallowest paths first; `truncated_fields` names any list that was cut, and
        `scan_complete` is false if the file limit was reached. Use read_file to confirm details.
        """
        try:
            return analyze_repository(workspace)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
