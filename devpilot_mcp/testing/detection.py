"""Deterministic detection of supported Python test frameworks. Nothing is executed.

Only two commands can ever be produced, and never from repository text:

    pytest:   <python> -m pytest -p no:cacheprovider
    unittest: <python> -m unittest

where <python> is the interpreter chosen by `select_interpreter` (DevPilot's
own, or an absolute path configured by the server operator).
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from devpilot_mcp.text_search import NotATextFileError, read_text
from devpilot_mcp.tools import repository
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

Framework = Literal["pytest", "unittest"]
Confidence = Literal["high", "medium", "low"]
FRAMEWORKS: tuple[Framework, ...] = ("pytest", "unittest")  # also the tie-break preference

# The complete set of commands; "python" stands for the selected interpreter.
COMMANDS: dict[str, list[str]] = {
    "pytest": ["python", "-m", "pytest", "-p", "no:cacheprovider"],
    "unittest": ["python", "-m", "unittest"],
}

TEST_PYTHON_ENV_VAR = "DEVPILOT_TEST_PYTHON"
MAX_TEST_FILES_INSPECTED = 50
MAX_TEST_FILE_BYTES = 256_000
MAX_CONFIG_BYTES = 256_000
MAX_EVIDENCE_ITEMS = 10
_RANK = {"high": 3, "medium": 2, "low": 1}
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)?)?(\.exe)?$")

_IMPORTS_PYTEST = re.compile(r"^\s*(?:import pytest\b|from pytest\b)", re.MULTILINE)
_IMPORTS_UNITTEST = re.compile(r"^\s*(?:import unittest\b|from unittest\b)", re.MULTILINE)
_TESTCASE_CLASS = re.compile(
    r"^\s*class\s+\w+\s*\([^)]*\b(?:unittest\.)?(?:TestCase|IsolatedAsyncioTestCase)\b", re.MULTILINE
)
_PLAIN_TEST_FUNCTION = re.compile(r"^(?:async\s+)?def\s+test\w*\s*\(", re.MULTILINE)


class TestConfigurationError(WorkspaceError):
    """The server's test-execution configuration (DEVPILOT_TEST_PYTHON) is invalid."""

    __test__ = False  # not a test class, despite the name


# --- Interpreter -------------------------------------------------------------


@dataclass(frozen=True)
class Interpreter:
    path: Path
    source: Literal["devpilot", "DEVPILOT_TEST_PYTHON"]


def select_interpreter() -> Interpreter:
    """The Python used for tests: DevPilot's own, unless the operator configured another.

    The executable is always an absolute path, so it is never looked up on PATH
    or in the workspace, and no tool argument or repository file can choose it.
    """
    configured = os.environ.get(TEST_PYTHON_ENV_VAR, "").strip()
    if not configured:
        return Interpreter(Path(sys.executable), "devpilot")
    path = Path(configured)
    if not path.is_absolute():
        raise TestConfigurationError(f"{TEST_PYTHON_ENV_VAR} must be an absolute path to a Python executable.")
    if not _PYTHON_NAME.match(path.name.lower()):
        raise TestConfigurationError(f"{TEST_PYTHON_ENV_VAR} must point to a Python executable (python or python.exe).")
    if not path.is_file():
        raise TestConfigurationError(f"{TEST_PYTHON_ENV_VAR} does not point to an existing file.")
    return Interpreter(path, "DEVPILOT_TEST_PYTHON")


class InterpreterInfo(BaseModel):
    source: Literal["devpilot", "DEVPILOT_TEST_PYTHON"]
    valid: bool
    python_version: str | None  # known only for DevPilot's own interpreter (nothing is executed)
    problem: str | None = None


def describe_interpreter() -> tuple[Interpreter | None, InterpreterInfo]:
    try:
        interpreter = select_interpreter()
    except TestConfigurationError as exc:
        return None, InterpreterInfo(source="DEVPILOT_TEST_PYTHON", valid=False, python_version=None, problem=str(exc))
    version = ".".join(map(str, sys.version_info[:3])) if interpreter.source == "devpilot" else None
    return interpreter, InterpreterInfo(source=interpreter.source, valid=True, python_version=version)


# --- Result models (also the tool's output schema) ---------------------------


class DetectedTestCommand(BaseModel):
    framework: Framework
    command: list[str]
    confidence: Confidence
    evidence: list[str]
    runner_available: bool | None  # None when it can't be known without running the interpreter


class TestCommandsResult(BaseModel):
    __test__ = False  # not a test class, despite the name

    detected: list[DetectedTestCommand]
    primary: DetectedTestCommand | None
    test_file_count: int
    test_directories: list[str]
    interpreter: InterpreterInfo
    warnings: list[str]


# --- Evidence ----------------------------------------------------------------


@dataclass
class _Evidence:
    high: list[str]
    medium: list[str]
    low: list[str]

    def add(self, level: Confidence, item: str) -> None:
        bucket = getattr(self, level)
        if item not in bucket:
            bucket.append(item)

    def confidence(self) -> Confidence | None:
        return "high" if self.high else "medium" if self.medium else "low" if self.low else None

    def items(self) -> list[str]:
        return (self.high + self.medium + self.low)[:MAX_EVIDENCE_ITEMS]


def _read_small(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_CONFIG_BYTES:
            return None
        return read_text(path)
    except (OSError, NotATextFileError):
        return None


def _ini_sections(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    return parser


def _inspect_config(root: Path, pytest: _Evidence, warnings: list[str]) -> None:
    """Root-level configuration files. Their contents are only inspected, never executed."""
    if (root / "pytest.ini").is_file():
        pytest.add("high", "pytest.ini")

    text = _read_small(root / "pyproject.toml")
    if text is not None:
        if tomllib is None:
            warnings.append("pyproject.toml was not inspected (TOML parsing needs Python 3.11+).")
        else:
            try:
                data = tomllib.loads(text)
            except ValueError:
                warnings.append("pyproject.toml is not valid TOML; its test configuration was ignored.")
            else:
                tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
                if isinstance(tool.get("pytest"), dict):
                    pytest.add("high", "pyproject.toml [tool.pytest]")
                try:
                    if "pytest" in repository._pyproject_names(data):
                        pytest.add("medium", "pyproject.toml declares pytest")
                except (AttributeError, TypeError):
                    warnings.append("pyproject.toml dependencies have an unexpected structure.")

    for name, section in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
        text = _read_small(root / name)
        if text is None:
            continue
        try:
            parser = _ini_sections(text)
        except configparser.Error:
            warnings.append(f"{name} could not be parsed; its test configuration was ignored.")
            continue
        if parser.has_section(section):
            pytest.add("high", f"{name} [{section}]")
        if name == "tox.ini" and any(
            "pytest" in parser.get(s, "commands", fallback="") for s in parser.sections() if s.startswith("testenv")
        ):
            pytest.add("medium", "tox.ini testenv commands mention pytest (tox itself is never run)")


def _inspect_requirements(workspace: Workspace, manifests: list[str], pytest: _Evidence) -> None:
    for rel in manifests:
        if not re.fullmatch(r"requirements[^/]*\.txt", rel.rsplit("/", 1)[-1]):
            continue
        text = _read_small(workspace.root / rel)
        if text is not None and "pytest" in repository._requirements_txt_names(text):
            pytest.add("medium", f"{rel} declares pytest")


def _reachable(base: Path, rel_dirs: list[str]) -> bool:
    """unittest discovery (3.11+) only descends into packages: every directory needs __init__.py."""
    current = base
    for part in rel_dirs:
        current = current / part
        if not (current / "__init__.py").is_file():
            return False
    return True


def unittest_command(workspace: Workspace, testcase_files: list[str], warnings: list[str]) -> list[str]:
    """`python -m unittest`, or `... discover -s <dir>` when root discovery cannot reach the tests.

    <dir> is a directory taken from the repository's own layout (never from a
    caller) and is validated again by the runner before anything is executed.
    """
    hidden = [f for f in testcase_files if not _reachable(workspace.root, f.split("/")[:-1])]
    if not hidden:
        return list(COMMANDS["unittest"])
    start = sorted({f.rsplit("/", 1)[0] for f in hidden}, key=lambda d: (d.count("/"), d))[0]
    missed = [
        f for f in hidden
        if not (f.startswith(start + "/") and _reachable(workspace.root / start, f[len(start) + 1 :].split("/")[:-1]))
    ]  # fmt: skip
    if missed:
        warnings.append(
            f"{len(missed)} unittest file(s) are outside '{start}' or in sub-directories without __init__.py, "
            "so unittest discovery will not run them."
        )
    return [*COMMANDS["unittest"], "discover", "-s", start]


def _inspect_test_files(
    workspace: Workspace, test_files: list[str], pytest: _Evidence, unittest: _Evidence, warnings: list[str]
) -> list[str]:
    """Record framework evidence; returns the discoverable (test*.py) files that define TestCase classes."""
    testcase_files: list[str] = []
    undiscoverable: list[str] = []
    plain_functions: list[str] = []
    for rel in [f for f in test_files if f.endswith(".py")][:MAX_TEST_FILES_INSPECTED]:
        path = workspace.root / rel
        try:
            if path.stat().st_size > MAX_TEST_FILE_BYTES:
                continue
            text = read_text(path)
        except (OSError, NotATextFileError):
            continue
        name = rel.rsplit("/", 1)[-1]
        if _IMPORTS_PYTEST.search(text):
            pytest.add("medium", "test files import pytest")
        if _TESTCASE_CLASS.search(text):
            if name.startswith("test"):
                unittest.add("high", "unittest.TestCase classes in test*.py files")
                testcase_files.append(rel)
            else:
                unittest.add("low", "unittest.TestCase classes (in files unittest discovery does not match)")
                undiscoverable.append(rel)
        elif _IMPORTS_UNITTEST.search(text):
            unittest.add("low", "test files import unittest")
        if _PLAIN_TEST_FUNCTION.search(text):
            pytest.add("medium", "module-level test functions (pytest style)")
            plain_functions.append(rel)
    if undiscoverable:
        warnings.append(
            f"{len(undiscoverable)} test file(s) define TestCase classes but do not match unittest's "
            "default 'test*.py' pattern, so 'python -m unittest' would not find them."
        )
    if plain_functions and not pytest.high:
        warnings.append(
            f"{len(plain_functions)} test file(s) define module-level test functions, which only pytest runs."
        )
    return testcase_files


# --- Detection -----------------------------------------------------------------


def detect_test_commands(workspace: Workspace) -> TestCommandsResult:
    """Identify supported test frameworks and their fixed commands. Nothing is executed."""
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")

    warnings: list[str] = []
    facts = repository.analyze_repository(workspace)
    pytest, unittest = _Evidence([], [], []), _Evidence([], [], [])

    _inspect_config(root, pytest, warnings)
    for directory in ["", *facts.tests.directories]:
        if (root / directory / "conftest.py").is_file():
            pytest.add("high", f"{directory + '/' if directory else ''}conftest.py")
    _inspect_requirements(workspace, facts.dependency_manifests, pytest)
    testcase_files = _inspect_test_files(workspace, facts.tests.files, pytest, unittest, warnings)
    for directory in facts.tests.directories[:3]:
        for evidence in (pytest, unittest):
            if evidence.confidence():
                evidence.add("low", f"{directory}/")

    interpreter, info = describe_interpreter()
    if not info.valid:
        warnings.append(f"Tests cannot be run: {info.problem}")

    detected = []
    for framework, evidence in (("pytest", pytest), ("unittest", unittest)):
        confidence = evidence.confidence()
        if confidence is None:
            continue
        available: bool | None = None
        if framework == "unittest":
            available = True if interpreter is not None else None
        elif interpreter is not None and interpreter.source == "devpilot":
            available = importlib.util.find_spec("pytest") is not None
        detected.append(
            DetectedTestCommand(
                framework=framework,  # type: ignore[arg-type]
                command=unittest_command(workspace, testcase_files, warnings)
                if framework == "unittest"
                else list(COMMANDS[framework]),
                confidence=confidence,
                evidence=evidence.items(),
                runner_available=available,
            )
        )
    detected.sort(key=lambda d: (-_RANK[d.confidence], FRAMEWORKS.index(d.framework)))
    primary = detected[0] if detected else None

    if not detected:
        warnings.append(
            "No supported test framework was detected (looked for pytest configuration or dependency, "
            "conftest.py, and Python test files using pytest or unittest). Nothing was guessed."
        )
    elif len(detected) == 2:
        warnings.append(
            f"Both pytest and unittest evidence was found; '{primary.framework}' is primary "
            f"({primary.confidence} confidence). pytest can also run unittest-style tests."
        )
    if primary is not None and primary.runner_available is False:
        warnings.append(f"{primary.framework} is not installed in the interpreter DevPilot uses; run_tests would fail.")
    if facts.tests.file_count == 0 and detected:
        warnings.append("Test configuration was found but no test files matching the usual naming patterns.")

    return TestCommandsResult(
        detected=detected,
        primary=primary,
        test_file_count=facts.tests.file_count,
        test_directories=facts.tests.directories[:MAX_EVIDENCE_ITEMS],
        interpreter=info,
        warnings=warnings,
    )
