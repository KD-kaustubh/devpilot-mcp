"""Testing and validation tools: get_test_commands, run_tests, validate_repository.

get_test_commands only inspects files. run_tests executes one of two fixed test
commands (pytest or unittest) chosen by detection; callers can pick between
detected frameworks and set a bounded timeout, but can never supply a command,
executable, argument or shell string. validate_repository reports objective
checks (repository facts, Git state, test discovery, Python syntax) and runs
tests only when explicitly asked to. None of these tools modifies files or Git
state itself, and none calls an LLM or ranks or scores the repository.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from devpilot_mcp.testing import runner
from devpilot_mcp.testing.detection import TestCommandsResult, detect_test_commands
from devpilot_mcp.testing.runner import DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, TestRunResult
from devpilot_mcp.testing.syntax import SyntaxReport, check_python_syntax
from devpilot_mcp.tools import git, repository
from devpilot_mcp.tools.common import EXECUTES_CODE, READ_ONLY, as_tool_error
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

Timeout = Annotated[int, Field(ge=1, le=MAX_TIMEOUT_SECONDS)]

CheckName = Literal["repository_analysis", "git_working_tree", "test_discovery", "test_execution", "python_syntax"]
CheckStatus = Literal["passed", "failed", "warning", "skipped", "error"]


class ValidationCheck(BaseModel):
    name: CheckName
    status: CheckStatus
    detail: str


class RepositoryFacts(BaseModel):
    name: str
    total_files: int
    scan_complete: bool
    languages: dict[str, int]
    python_files: int
    dependency_manifests: list[str]
    test_file_count: int


class GitFacts(BaseModel):
    available: bool
    reason: str | None = None
    branch: str | None = None
    head_commit: str | None = None
    clean: bool | None = None
    counts: dict[str, int] = {}


class TestsSection(BaseModel):
    __test__ = False  # not a test class, despite the name

    execution_requested: bool
    executed: bool
    discovery: TestCommandsResult | None
    result: TestRunResult | None


class ValidationReport(BaseModel):
    repository: RepositoryFacts | None
    git: GitFacts
    tests: TestsSection
    syntax: SyntaxReport | None
    checks: list[ValidationCheck]
    warnings: list[str]
    failures: list[str]
    skipped: list[str]
    complete: bool


def _error_text(exc: Exception) -> str:
    return str(as_tool_error(exc))


def validate_repository(
    workspace: Workspace, run_tests: object = False, timeout_seconds: object = DEFAULT_TIMEOUT_SECONDS
) -> ValidationReport:
    """Collect objective validation evidence. Tests run only when run_tests is True."""
    if not isinstance(run_tests, bool):
        raise WorkspaceError("run_tests must be true or false.")
    timeout = runner.validate_timeout(timeout_seconds)
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")

    checks: list[ValidationCheck] = []
    warnings: list[str] = []

    def check(name: CheckName, status: CheckStatus, detail: str) -> None:
        checks.append(ValidationCheck(name=name, status=status, detail=detail))

    # 1. Repository structure (Phase 3).
    facts = None
    try:
        analysis = repository.analyze_repository(workspace)
        facts = RepositoryFacts(
            name=workspace.root.name,
            total_files=analysis.total_files,
            scan_complete=analysis.scan_complete,
            languages=dict(list(analysis.languages.items())[:8]),
            python_files=analysis.languages.get("Python", 0),
            dependency_manifests=analysis.dependency_manifests[:10],
            test_file_count=analysis.tests.file_count,
        )
        check("repository_analysis", "passed", f"{analysis.total_files} files analyzed.")
        if not analysis.scan_complete:
            warnings.append("repository_analysis: the file limit was reached; facts cover part of the repository.")
    except (WorkspaceError, OSError) as exc:
        check("repository_analysis", "error", _error_text(exc))

    # 2. Git state (Phase 4, read-only).
    try:
        status = git.git_status(workspace)
        git_facts = GitFacts(
            available=True, branch=status.branch, head_commit=status.head_commit, clean=status.clean, counts=status.counts
        )
        if status.clean:
            check("git_working_tree", "passed", f"Working tree is clean on {status.branch or 'a detached HEAD'}.")
        else:
            c = status.counts
            detail = f"Working tree has changes: {c['staged']} staged, {c['unstaged']} unstaged, {c['untracked']} untracked."
            check("git_working_tree", "warning", detail)
            warnings.append(f"git_working_tree: {detail}")
    except git.NotAGitRepositoryError as exc:
        git_facts = GitFacts(available=False, reason=str(exc))
        check("git_working_tree", "skipped", "The workspace is not a Git repository root.")
    except (WorkspaceError, OSError) as exc:
        git_facts = GitFacts(available=False, reason=_error_text(exc))
        check("git_working_tree", "error", _error_text(exc))

    # 3. Test discovery (Phase 8, nothing executed).
    discovery = None
    try:
        discovery = detect_test_commands(workspace)
        warnings.extend(f"test_discovery: {w}" for w in discovery.warnings)
        if discovery.primary is None:
            check("test_discovery", "warning", "No supported test framework was detected.")
        else:
            primary = discovery.primary
            check("test_discovery", "passed", f"Primary: {primary.framework} ({primary.confidence} confidence).")
    except (WorkspaceError, OSError) as exc:
        check("test_discovery", "error", _error_text(exc))

    # 4. Test execution (only on request).
    result = None
    if not run_tests:
        check("test_execution", "skipped", "Not requested (run_tests is false); no test code was executed.")
    elif discovery is None or discovery.primary is None:
        check("test_execution", "skipped", "No test framework was detected, so no tests were run.")
    else:
        try:
            result = runner.run_tests(workspace, discovery.primary.framework, timeout)
            warnings.extend(f"test_execution: {w}" for w in result.warnings)
            summary = (
                f"{result.framework}: {result.status}; exit code {result.exit_code}; "
                f"passed={result.passed}, failed={result.failed}, errors={result.errors}, skipped={result.skipped}."
            )
            status_map: dict[str, CheckStatus] = {
                "passed": "passed", "failed": "failed", "error": "failed", "timed_out": "failed", "no_tests": "warning",
            }  # fmt: skip
            check("test_execution", status_map[result.status], summary)
        except (WorkspaceError, OSError) as exc:
            check("test_execution", "error", _error_text(exc))

    # 5. Python syntax (parse only, never executed).
    syntax = None
    try:
        syntax = check_python_syntax(workspace)
        if syntax.files_checked == 0:
            check("python_syntax", "skipped", "No Python files were found to parse.")
        elif syntax.error_count:
            first = syntax.errors[0]
            check(
                "python_syntax",
                "failed",
                f"{syntax.error_count} file(s) failed to parse (first: {first.path}"
                f"{f':{first.line}' if first.line else ''}: {first.message}).",
            )
        else:
            check("python_syntax", "passed", f"{syntax.files_checked} Python file(s) parsed without syntax errors.")
        if syntax.warning_count:
            warnings.append(f"python_syntax: {syntax.warning_count} SyntaxWarning(s) reported by the parser.")
        if syntax.files_skipped:
            warnings.append(f"python_syntax: {syntax.files_skipped} file(s) were too large, binary or unreadable.")
        if not syntax.complete:
            warnings.append("python_syntax: the file limit was reached; not every Python file was parsed.")
    except (WorkspaceError, OSError) as exc:
        check("python_syntax", "error", _error_text(exc))

    return ValidationReport(
        repository=facts,
        git=git_facts,
        tests=TestsSection(execution_requested=run_tests, executed=result is not None, discovery=discovery, result=result),
        syntax=syntax,
        checks=checks,
        warnings=warnings,
        failures=[f"{c.name}: {c.detail}" for c in checks if c.status == "failed"],
        skipped=[f"{c.name}: {c.detail}" for c in checks if c.status == "skipped"],
        complete=all(c.status != "error" for c in checks)
        and (facts is None or facts.scan_complete)
        and (syntax is None or syntax.complete),
    )


def register(server: MCPServer, workspace: Workspace) -> None:
    """Expose get_test_commands, run_tests and validate_repository, bound to the workspace."""

    @server.tool(name="get_test_commands", annotations=READ_ONLY)
    def get_test_commands_tool() -> TestCommandsResult:
        """Detect supported Python test frameworks and the fixed commands DevPilot would use. Runs nothing.

        Looks for pytest configuration (pytest.ini, pyproject.toml [tool.pytest...], setup.cfg
        [tool:pytest], tox.ini [pytest]), conftest.py, pytest in requirements/pyproject, and test
        files using pytest or unittest.TestCase. Each detected framework has a confidence (high,
        medium, low) and the evidence behind it; `primary` is the one run_tests uses by default.
        Only two commands exist: python -m pytest -p no:cacheprovider, and python -m unittest.
        Empty `detected` plus a warning means nothing was found; nothing is guessed.
        """
        try:
            return detect_test_commands(workspace)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="run_tests", annotations=EXECUTES_CODE)
    def run_tests_tool(
        framework: Literal["pytest", "unittest"] | None = None,
        timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> TestRunResult:
        """Run the repository's tests with a fixed, detected command. EXECUTES repository test code.

        Runs the primary framework from get_test_commands, or `framework` if it was also detected.
        No command, executable, argument or shell string can be supplied: the only commands are
        python -m pytest -p no:cacheprovider and python -m unittest, run without a shell in the
        workspace root, with a sanitized environment (secret-looking variables removed), a timeout
        and bounded output. Returns status (passed/failed/no_tests/timed_out/error), exit code,
        parsed counts (null when not parseable), stdout/stderr (start and end kept when long),
        and files the run changed. Test code may have side effects; DevPilot never reverts them.

        Args:
            framework: "pytest" or "unittest"; must be one that was detected. Omit for the primary.
            timeout_seconds: 1-600, default 120. The test process is killed when it is exceeded.
        """
        try:
            return runner.run_tests(workspace, framework, timeout_seconds)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="validate_repository", annotations=EXECUTES_CODE)
    def validate_repository_tool(
        run_tests: bool = False,
        timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> ValidationReport:
        """Deterministic validation report: facts, checks, warnings, failures and skipped checks.

        Checks, in order: repository_analysis, git_working_tree (clean/dirty; never modified),
        test_discovery, test_execution and python_syntax (files are parsed, never executed or
        imported). By default NO code is executed: test_execution is skipped unless run_tests is
        true, in which case the primary detected test command runs exactly as with run_tests.
        There is no score or ranking; interpret the evidence yourself.

        Args:
            run_tests: Execute the primary detected test command (default false).
            timeout_seconds: Timeout for that test run, 1-600, default 120.
        """
        try:
            return validate_repository(workspace, run_tests, timeout_seconds)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
