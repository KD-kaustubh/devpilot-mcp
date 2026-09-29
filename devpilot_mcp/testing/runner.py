"""Controlled execution of a detected test command.

This is the only place DevPilot runs repository code, and it is narrow:

- the command comes from `detection.COMMANDS` only (pytest or unittest), for a
  framework that detection found; no tool argument or repository file can
  supply an executable, argument or shell string;
- the interpreter is an absolute path (`detection.select_interpreter`);
- subprocess with an argument list, shell=False, no stdin, cwd = workspace root;
- a sanitized environment: Python/pytest injection variables, GIT_* variables
  and secret-looking variables are removed, bytecode writing is disabled;
- a timeout (the process is killed) and bounded output (head + tail kept);
- secret-looking values are redacted from the returned output;
- a bounded before/after snapshot reports files the tests changed (nothing is
  reverted).

Test code is repository code: it runs with the permissions of the DevPilot
process and can have side effects, including network access.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from devpilot_mcp.testing.detection import (
    COMMANDS,
    FRAMEWORKS,
    Interpreter,
    InterpreterInfo,
    describe_interpreter,
    detect_test_commands,
    select_interpreter,
)
from devpilot_mcp.text_search import SKIPPED_DIRS
from devpilot_mcp.tools import git
from devpilot_mcp.tools.investigation import _SECRET_VALUE_PATTERNS, REDACTED
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
OUTPUT_HEAD_BYTES = 8_000  # kept from the start of each stream
OUTPUT_TAIL_BYTES = 24_000  # kept from the end of each stream (summaries are printed last)
READ_CHUNK = 65_536
MAX_SNAPSHOT_FILES = 20_000
MAX_REPORTED_CHANGES = 50
MAX_REMOVED_NAMES = 50

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Variables that could make the interpreter or pytest run something other than the fixed command.
_UNSAFE_VARIABLES = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONBREAKPOINT",
     "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG"}
)  # fmt: skip
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|AUTH", re.I)

_run_lock = threading.Lock()


class TestRunError(WorkspaceError):
    """The test command could not be run (not detected, interpreter missing, busy, ...)."""

    __test__ = False  # not a test class, despite the name


# --- Environment and redaction -------------------------------------------------


def build_test_environment() -> tuple[dict[str, str], list[str], list[str]]:
    """The child environment, the names of removed variables, and secret values to redact."""
    env: dict[str, str] = {}
    removed: list[str] = []
    secrets: list[str] = []
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _UNSAFE_VARIABLES or upper.startswith("GIT_"):
            removed.append(name)
        elif _SECRET_NAME.search(upper):
            removed.append(name)
            if len(value.strip()) >= 6:
                secrets.append(value.strip())
        else:
            env[name] = value
    env.update(
        PYTHONDONTWRITEBYTECODE="1",  # no __pycache__ written into the workspace
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
        PY_COLORS="0",
        NO_COLOR="1",
    )
    return env, sorted(removed, key=str.upper), secrets



class Redactor:
    """Masks secret values (counted) and local absolute paths (not counted) in test output."""

    def __init__(self, secrets: list[str], paths: dict[str, str] | None = None) -> None:
        self._secrets = sorted(set(secrets), key=len, reverse=True)
        # Longest first, so the interpreter path inside the workspace is masked before the root.
        self._paths = sorted((paths or {}).items(), key=lambda item: len(item[0]), reverse=True)
        self.count = 0

    def __call__(self, text: str) -> str:
        for path, placeholder in self._paths:
            text = text.replace(path, placeholder)
        for secret in self._secrets:
            if secret in text:
                self.count += text.count(secret)
                text = text.replace(secret, REDACTED)
        for pattern in _SECRET_VALUE_PATTERNS:
            text, n = pattern.subn(REDACTED, text)
            self.count += n
        return text


# --- Bounded process execution --------------------------------------------------


@dataclass
class _Stream:
    head: bytearray = field(default_factory=bytearray)
    tail: deque = field(default_factory=deque)
    tail_size: int = 0
    total: int = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = OUTPUT_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head.extend(chunk[:room])
            chunk = chunk[room:]
        if chunk:
            self.tail.append(chunk)
            self.tail_size += len(chunk)
            while self.tail_size - len(self.tail[0]) >= OUTPUT_TAIL_BYTES:
                self.tail_size -= len(self.tail.popleft())

    def render(self) -> tuple[str, str, bool]:
        """(text for the client, the last bytes of output for parsing, truncated)."""
        tail = b"".join(self.tail)[-OUTPUT_TAIL_BYTES:]
        omitted = self.total - len(self.head) - len(tail)
        truncated = omitted > 0
        if truncated:
            text = (
                self.head.decode("utf-8", "replace")
                + f"\n... [{omitted:,} bytes of output omitted] ...\n"
                + tail.decode("utf-8", "replace")
            )
        else:
            text = (bytes(self.head) + tail).decode("utf-8", "replace")
        last = (bytes(self.head) + tail)[-OUTPUT_TAIL_BYTES:].decode("utf-8", "replace")
        return text, last, truncated


@dataclass
class ProcessOutcome:
    exit_code: int | None
    stdout: _Stream
    stderr: _Stream
    timed_out: bool
    duration: float


def run_process(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> ProcessOutcome:
    """Run argv without a shell; keep head and tail of each stream; kill it on timeout."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        raise TestRunError("The test interpreter could not be found.") from None
    except OSError as exc:
        raise TestRunError(f"The test interpreter could not be started ({type(exc).__name__}).") from None

    streams = (_Stream(), _Stream())

    def pump(pipe, stream: _Stream) -> None:
        while chunk := pipe.read(READ_CHUNK):
            stream.feed(chunk)

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, streams[0]), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, streams[1]), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for reader in readers:
        reader.join(timeout=5)  # a grandchild holding the pipe open must not hang the server
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    return ProcessOutcome(proc.returncode, streams[0], streams[1], timed_out, time.monotonic() - started)


# --- Result parsing --------------------------------------------------------------


@dataclass
class Counts:
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    total: int | None = None
    other: dict[str, int] = field(default_factory=dict)
    no_tests: bool = False
    found: bool = False


_PYTEST_SUMMARY = re.compile(r"^=*\s*(?P<body>.*?)\s+in\s+[\d.]+s(?:\s+\([\d:.]+\))?\s*=*$")
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)")


def parse_pytest(stdout_tail: str) -> Counts:
    for line in reversed(stdout_tail.strip().splitlines()):
        match = _PYTEST_SUMMARY.match(line.strip())
        if not match:
            continue
        body = match["body"]
        if body.strip("= ").startswith("no tests ran"):
            return Counts(0, 0, 0, 0, 0, no_tests=True, found=True)
        pairs = _PYTEST_COUNT.findall(body)
        if not pairs:
            continue
        values: dict[str, int] = {}
        for number, word in pairs:
            key = {"error": "errors", "warning": "warnings"}.get(word, word)
            values[key] = values.get(key, 0) + int(number)
        counts = Counts(
            passed=values.get("passed", 0),
            failed=values.get("failed", 0),
            errors=values.get("errors", 0),
            skipped=values.get("skipped", 0),
            other={k: v for k, v in sorted(values.items()) if k not in ("passed", "failed", "errors", "skipped")},
            found=True,
        )
        counts.total = counts.passed + counts.failed + counts.errors + counts.skipped + values.get(
            "xfailed", 0
        ) + values.get("xpassed", 0)
        counts.no_tests = counts.total == 0
        return counts
    return Counts()


_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s", re.MULTILINE)
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED|NO TESTS RAN)(?: \((.*)\))?\s*$", re.MULTILINE)


def parse_unittest(stderr_tail: str) -> Counts:
    ran = _UNITTEST_RAN.findall(stderr_tail)
    results = _UNITTEST_RESULT.findall(stderr_tail)
    if not ran or not results:
        return Counts()
    total = int(ran[-1])
    status, detail = results[-1]
    values = {key.strip(): int(value) for key, value in re.findall(r"([a-z ]+)=(\d+)", detail)}
    failed, errors, skipped = values.get("failures", 0), values.get("errors", 0), values.get("skipped", 0)
    expected, unexpected = values.get("expected failures", 0), values.get("unexpected successes", 0)
    other = {k.replace(" ", "_"): v for k, v in (("expected failures", expected), ("unexpected successes", unexpected)) if v}
    return Counts(
        passed=max(0, total - failed - errors - skipped - expected - unexpected),
        failed=failed,
        errors=errors,
        skipped=skipped,
        total=total,
        other=other,
        no_tests=total == 0 or status == "NO TESTS RAN",
        found=True,
    )


# --- Side effects ------------------------------------------------------------------


def _file_snapshot(root: Path) -> tuple[dict[str, tuple[int, int]], bool]:
    """(relative path -> (size, mtime_ns)) for workspace files, skipping VCS/dependency/cache dirs."""
    entries: dict[str, tuple[int, int]] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for name in sorted(filenames):
            path = Path(directory) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            entries[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
            if len(entries) >= MAX_SNAPSHOT_FILES:
                return entries, False
    return entries, True


def _git_state(workspace: Workspace) -> tuple | None:
    try:
        status = git.git_status(workspace)
    except (WorkspaceError, OSError):
        return None
    return (
        status.branch,
        status.head_commit,
        tuple((c.path, c.change) for c in status.staged),
        tuple((c.path, c.change) for c in status.unstaged),
        tuple(status.untracked),
    )


class SideEffects(BaseModel):
    checked: bool
    complete: bool
    files_created: list[str]
    files_modified: list[str]
    files_deleted: list[str]
    total_changes: int
    git_state_changed: bool | None  # None when the workspace is not a Git repository


def _compare(before, after, complete: bool, git_before, git_after) -> SideEffects:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return SideEffects(
        checked=True,
        complete=complete,
        files_created=created[:MAX_REPORTED_CHANGES],
        files_modified=modified[:MAX_REPORTED_CHANGES],
        files_deleted=deleted[:MAX_REPORTED_CHANGES],
        total_changes=len(created) + len(modified) + len(deleted),
        git_state_changed=None if git_before is None or git_after is None else git_before != git_after,
    )


# --- run_tests ------------------------------------------------------------------------

Status = Literal["passed", "failed", "no_tests", "timed_out", "error"]


class TestRunResult(BaseModel):
    __test__ = False  # not a test class, despite the name

    framework: Literal["pytest", "unittest"]
    command: list[str]
    interpreter: InterpreterInfo
    status: Status
    exit_code: int | None
    passed: int | None
    failed: int | None
    errors: int | None
    skipped: int | None
    total: int | None
    other_counts: dict[str, int]
    duration_seconds: float
    timed_out: bool
    timeout_seconds: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    removed_environment_variables: list[str]
    redactions: int
    side_effects: SideEffects
    warnings: list[str]


def validate_timeout(timeout_seconds: object) -> int:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise TestRunError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}.")
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise TestRunError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}.")
    return timeout_seconds


def build_argv(workspace: Workspace, interpreter: Interpreter, command: list[str]) -> list[str]:
    """Turn a detected command into argv, refusing anything but DevPilot's fixed command shapes.

    Allowed: COMMANDS["pytest"], COMMANDS["unittest"], or COMMANDS["unittest"] +
    ["discover", "-s", <dir>] where <dir> is an existing directory inside the
    workspace that cannot be mistaken for an option.
    """
    if command in (COMMANDS["pytest"], COMMANDS["unittest"]):
        return [str(interpreter.path), *command[1:]]
    base = COMMANDS["unittest"]
    if len(command) == len(base) + 3 and command[: len(base)] == base and command[len(base) : -1] == ["discover", "-s"]:
        start = command[-1]
        parts = start.split("/")
        if start and not start.startswith("-") and "\\" not in start and not {"", ".", ".."} & set(parts):
            path = workspace.resolve(start)
            if path.is_dir() and path != workspace.root:
                return [str(interpreter.path), *command[1:]]
    raise TestRunError("Refusing to run a command that is not one of DevPilot's fixed test commands.")


def run_tests(
    workspace: Workspace, framework: object = None, timeout_seconds: object = DEFAULT_TIMEOUT_SECONDS
) -> TestRunResult:
    """Run the primary (or the named, detected) test framework's fixed command in the workspace."""
    if framework is not None and framework not in FRAMEWORKS:
        raise TestRunError(f"framework must be one of: {', '.join(FRAMEWORKS)} (or omitted for the primary one).")
    timeout = validate_timeout(timeout_seconds)
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")

    discovery = detect_test_commands(workspace)
    if framework is None:
        if discovery.primary is None:
            raise TestRunError("No supported test framework was detected, so nothing was run.")
        framework = discovery.primary.framework
    selected = next((d for d in discovery.detected if d.framework == framework), None)
    if selected is None:
        raise TestRunError(f"'{framework}' was not detected in this repository, so it was not run.")
    interpreter = select_interpreter()  # raises TestConfigurationError for a bad DEVPILOT_TEST_PYTHON
    _, info = describe_interpreter()
    argv = build_argv(workspace, interpreter, selected.command)
    env, removed, secrets = build_test_environment()

    if not _run_lock.acquire(blocking=False):
        raise TestRunError("Another test run is already in progress in this server; try again when it finishes.")
    try:
        git_before = _git_state(workspace)
        files_before, complete_before = _file_snapshot(root)
        outcome = run_process(argv, root, env, timeout)
        files_after, complete_after = _file_snapshot(root)
        git_after = _git_state(workspace)
    finally:
        _run_lock.release()

    paths = {}
    for form in {str(interpreter.path), interpreter.path.as_posix()}:
        paths[form] = "<python>"
    for form in {str(root), root.as_posix()}:
        paths[form] = "<workspace>"
    redact = Redactor(secrets, paths)
    stdout_text, stdout_tail, stdout_cut = outcome.stdout.render()
    stderr_text, stderr_tail, stderr_cut = outcome.stderr.render()
    counts = parse_pytest(stdout_tail) if framework == "pytest" else parse_unittest(stderr_tail)
    side_effects = _compare(files_before, files_after, complete_before and complete_after, git_before, git_after)

    warnings: list[str] = []
    if outcome.timed_out:
        status: Status = "timed_out"
        warnings.append(f"The test run exceeded {timeout} seconds and was stopped.")
    elif counts.found and counts.no_tests:
        status = "no_tests"
    elif outcome.exit_code == 5:  # pytest and unittest (3.12+) both use 5 for "no tests"
        status = "no_tests"
    elif counts.found and outcome.exit_code == 0:
        status = "passed"
    elif counts.found and (counts.failed or counts.errors):
        status = "failed"
    else:
        status = "passed" if outcome.exit_code == 0 else "error"
    if not counts.found and not outcome.timed_out:
        warnings.append("No test summary was found in the output, so test counts are unknown (null).")
    if f"No module named {framework}" in stderr_tail:
        warnings.append(f"{framework} is not installed in the test interpreter.")
    if stdout_cut or stderr_cut:
        warnings.append("Output was truncated; the beginning and end of each stream are kept.")
    if side_effects.total_changes:
        warnings.append(
            f"The test run changed {side_effects.total_changes} workspace file(s); DevPilot did not revert them."
        )
    if side_effects.git_state_changed:
        warnings.append("The Git working-tree state changed during the test run; DevPilot did not revert it.")
    if not side_effects.complete:
        warnings.append(f"Side-effect detection covered only the first {MAX_SNAPSHOT_FILES:,} files.")

    stdout, stderr = redact(stdout_text), redact(stderr_text)
    return TestRunResult(
        framework=framework,  # type: ignore[arg-type]
        command=list(selected.command),
        interpreter=info,
        status=status,
        exit_code=outcome.exit_code,
        passed=counts.passed,
        failed=counts.failed,
        errors=counts.errors,
        skipped=counts.skipped,
        total=counts.total,
        other_counts=counts.other,
        duration_seconds=round(outcome.duration, 3),
        timed_out=outcome.timed_out,
        timeout_seconds=timeout,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=outcome.stdout.total,
        stderr_bytes=outcome.stderr.total,
        output_truncated=stdout_cut or stderr_cut,
        removed_environment_variables=removed[:MAX_REMOVED_NAMES],
        redactions=redact.count,
        side_effects=side_effects,
        warnings=warnings,
    )
