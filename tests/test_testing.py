"""Tests for get_test_commands, run_tests and validate_repository."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from devpilot_mcp.server import create_server
from devpilot_mcp.testing import detection, runner
from devpilot_mcp.testing.detection import TestConfigurationError, detect_test_commands
from devpilot_mcp.testing.runner import (
    COMMANDS,
    ProcessOutcome,
    Redactor,
    TestRunError,
    _Stream,
    build_test_environment,
    parse_pytest,
    parse_unittest,
    run_tests,
)
from devpilot_mcp.testing.syntax import check_python_syntax
from devpilot_mcp.tools.testing import validate_repository
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError
from tests.test_git import GitRepoTestCase

PASSING = """\
import unittest


class TestMath(unittest.TestCase):
    def test_add(self):
        self.assertEqual(1 + 1, 2)

    def test_sub(self):
        self.assertEqual(3 - 1, 2)

    @unittest.skip("not today")
    def test_skipped(self):
        pass
"""

FAILING = """\
import unittest


class TestBroken(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError("boom")
"""

INJECTIONS = [
    "; whoami", "& whoami", "&& powershell -Command Get-Process", "| whoami", "python -c 'import os'",
    "cmd /c whoami", "powershell -Command whoami", "pytest; whoami", "unittest && calc", "../../python",
    "C:\\Windows\\System32\\cmd.exe", "\\\\server\\share\\evil.exe", "/bin/sh", "pytest\x00", "nose", "PYTEST", "",
]  # fmt: skip


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class WorkspaceCase(unittest.TestCase):
    """A fresh workspace at <tmp>/repo; DEVPILOT_TEST_PYTHON and secrets are cleared per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()  # long form: Windows TEMP can be an 8.3 short path
        self.root = self.base / "repo"
        self.root.mkdir()
        self.workspace = Workspace(self.root)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(detection.TEST_PYTHON_ENV_VAR, None)

    def write(self, files: dict[str, str | bytes]) -> None:
        write_files(self.root, files)

    def detect(self):
        return detect_test_commands(self.workspace)


# --- Detection -------------------------------------------------------------------


class DetectionTests(WorkspaceCase):
    def test_pytest_project(self) -> None:
        self.write({
            "pyproject.toml": '[project]\nname = "x"\ndependencies = []\n[project.optional-dependencies]\n'
                              'dev = ["pytest>=8"]\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "tests/test_app.py": "import pytest\n\n\ndef test_app():\n    assert True\n",
        })  # fmt: skip
        result = self.detect()
        self.assertEqual([d.framework for d in result.detected], ["pytest"])
        primary = result.primary
        self.assertEqual((primary.framework, primary.confidence), ("pytest", "high"))
        self.assertEqual(primary.command, ["python", "-m", "pytest", "-p", "no:cacheprovider"])
        self.assertEqual(
            primary.evidence,
            ["pyproject.toml [tool.pytest]", "pyproject.toml declares pytest", "test files import pytest",
             "module-level test functions (pytest style)", "tests/"],
        )  # fmt: skip
        self.assertEqual((result.test_file_count, result.test_directories), (1, ["tests"]))

    def test_each_pytest_configuration_is_strong_evidence(self) -> None:
        cases = {
            "pytest.ini": ("pytest.ini", "[pytest]\n"),
            "setup.cfg": ("setup.cfg", "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -q\n"),
            "tox.ini": ("tox.ini", "[tox]\nenvlist = py312\n\n[pytest]\nminversion = 7\n"),
            "conftest": ("tests/conftest.py", "import pytest\n"),
        }
        for name, (rel, content) in cases.items():
            with self.subTest(config=name):
                self.setUp()
                self.write({rel: content, "tests/test_x.py": "def test_x():\n    assert 1\n"})
                primary = self.detect().primary
                self.assertEqual((primary.framework, primary.confidence), ("pytest", "high"))

    def test_pytest_dependency_is_medium_evidence(self) -> None:
        self.write({"requirements-dev.txt": "black\npytest==8.3.0  # tests\n", "tests/test_x.py": "X = 1\n"})
        primary = self.detect().primary
        self.assertEqual((primary.framework, primary.confidence), ("pytest", "medium"))
        self.assertIn("requirements-dev.txt declares pytest", primary.evidence)

    def test_unittest_project(self) -> None:
        self.write({"tests/__init__.py": "", "tests/test_math.py": PASSING})
        result = self.detect()
        self.assertEqual([(d.framework, d.confidence) for d in result.detected], [("unittest", "high")])
        self.assertEqual(result.primary.command, ["python", "-m", "unittest"])
        self.assertTrue(result.primary.runner_available)

    def test_no_framework(self) -> None:
        self.write({"src/app.py": "print('hi')\n", "README.md": "# x\n"})
        result = self.detect()
        self.assertEqual((result.detected, result.primary), ([], None))
        self.assertIn("No supported test framework was detected", result.warnings[-1])

    def test_test_files_without_framework_evidence(self) -> None:
        self.write({"tests/test_data.py": "VALUES = [1, 2, 3]\n"})
        self.assertEqual(self.detect().detected, [])

    def test_malformed_configuration(self) -> None:
        self.write({
            "pyproject.toml": "[tool.pytest.ini_options\nbroken = ",
            "setup.cfg": "this is not ini\n[tool:pytest",
            "tox.ini": "[tox\n",
            "tests/test_math.py": PASSING,
        })  # fmt: skip
        result = self.detect()
        self.assertEqual(result.primary.framework, "unittest")  # still detected from the test files
        joined = " ".join(result.warnings)
        self.assertIn("pyproject.toml is not valid TOML", joined)
        self.assertIn("setup.cfg could not be parsed", joined)
        self.assertIn("tox.ini could not be parsed", joined)

    def test_conflicting_evidence(self) -> None:
        self.write({"requirements.txt": "pytest\n", "tests/test_math.py": PASSING})
        result = self.detect()
        self.assertEqual([(d.framework, d.confidence) for d in result.detected], [("unittest", "high"), ("pytest", "medium")])
        self.assertEqual(result.primary.framework, "unittest")
        self.assertTrue(any("Both pytest and unittest" in w for w in result.warnings))
        self.write({"pytest.ini": "[pytest]\n"})  # equal confidence: pytest wins the tie
        self.assertEqual([d.framework for d in self.detect().detected], ["pytest", "unittest"])

    def test_undiscoverable_unittest_files(self) -> None:
        self.write({"tests/math_test.py": PASSING})
        result = self.detect()
        self.assertEqual((result.primary.framework, result.primary.confidence), ("unittest", "low"))
        self.assertTrue(any("'test*.py' pattern" in w for w in result.warnings))

    def test_deterministic_and_bounded(self) -> None:
        self.write({f"tests/test_{i:03d}.py": PASSING + "\nimport pytest\n" for i in range(80)})
        self.write({"pytest.ini": "[pytest]\n", "tests/conftest.py": "", "requirements.txt": "pytest\n"})
        first, second = self.detect(), self.detect()
        self.assertEqual(first.model_dump(), second.model_dump())
        for detected in first.detected:
            self.assertLessEqual(len(detected.evidence), detection.MAX_EVIDENCE_ITEMS)
        self.assertEqual(first.test_file_count, 80)

    def test_configured_interpreter(self) -> None:
        self.write({"pytest.ini": "", "tests/test_math.py": PASSING})
        for value, problem in (("python", "absolute"), (str(self.base / "tool.exe"), "Python executable"),
                               (str(self.base / "python.exe"), "existing file")):  # fmt: skip
            with self.subTest(value=value):
                os.environ[detection.TEST_PYTHON_ENV_VAR] = value
                result = self.detect()
                self.assertFalse(result.interpreter.valid)
                self.assertIn(problem, result.interpreter.problem)
                self.assertTrue(any("Tests cannot be run" in w for w in result.warnings))
        os.environ[detection.TEST_PYTHON_ENV_VAR] = sys.executable
        result = self.detect()
        self.assertEqual((result.interpreter.source, result.interpreter.valid), ("DEVPILOT_TEST_PYTHON", True))
        pytest = next(d for d in result.detected if d.framework == "pytest")
        self.assertIsNone(pytest.runner_available)  # unknowable without running that interpreter

    def test_missing_workspace(self) -> None:
        with self.assertRaises(PathNotFoundError):
            detect_test_commands(Workspace(self.base / "gone"))

    def test_detection_executes_nothing(self) -> None:
        self.write({"pytest.ini": "", "tests/test_math.py": PASSING})
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")):
            self.detect()


# --- Output parsing and helpers ----------------------------------------------------


class ParsingTests(unittest.TestCase):
    def test_pytest_summaries(self) -> None:
        cases = {
            "==== 3 passed, 1 skipped in 0.12s ====": (3, 0, 0, 1, 4, {}),
            "1 failed, 2 passed, 1 error in 1.20s": (2, 1, 1, 0, 4, {}),
            "===== 5 passed, 2 warnings in 0.30s (0:00:01) =====": (5, 0, 0, 0, 5, {"warnings": 2}),
            "== 2 xfailed, 1 xpassed, 3 deselected in 0.10s ==": (0, 0, 0, 0, 3, {"deselected": 3, "xfailed": 2, "xpassed": 1}),
        }  # fmt: skip
        for line, (passed, failed, errors, skipped, total, other) in cases.items():
            with self.subTest(line=line):
                counts = parse_pytest("collected 4 items\n\ntests/test_x.py ....\n" + line + "\n")
                self.assertEqual((counts.passed, counts.failed, counts.errors, counts.skipped, counts.total, counts.other),
                                 (passed, failed, errors, skipped, total, other))  # fmt: skip
        self.assertTrue(parse_pytest("===== no tests ran in 0.01s =====\n").no_tests)
        self.assertFalse(parse_pytest("ImportError: something\n").found)

    def test_unittest_summaries(self) -> None:
        ok = parse_unittest("..s\n----------------------------------------------------------------------\nRan 3 tests in 0.001s\n\nOK (skipped=1)\n")
        self.assertEqual((ok.passed, ok.failed, ok.errors, ok.skipped, ok.total), (2, 0, 0, 1, 3))
        bad = parse_unittest("Ran 6 tests in 0.1s\n\nFAILED (failures=1, errors=2, skipped=1, expected failures=1)\n")
        self.assertEqual((bad.passed, bad.failed, bad.errors, bad.skipped, bad.other), (1, 1, 2, 1, {"expected_failures": 1}))
        empty = parse_unittest("\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n")
        self.assertTrue(empty.no_tests)
        self.assertFalse(parse_unittest("Traceback ...\nModuleNotFoundError\n").found)

    def test_output_is_bounded_with_head_and_tail(self) -> None:
        stream = _Stream()
        for i in range(2_000):
            stream.feed(f"line {i:05d} {'x' * 60}\n".encode())
        text, last, truncated = stream.render()
        self.assertTrue(truncated)
        self.assertTrue(text.startswith("line 00000"))
        self.assertIn("bytes of output omitted", text)
        self.assertTrue(last.rstrip().endswith("x" * 60) and "line 01999" in last)
        self.assertLessEqual(len(text), runner.OUTPUT_HEAD_BYTES + runner.OUTPUT_TAIL_BYTES + 100)
        small = _Stream()
        small.feed(b"short output\n")
        self.assertEqual(small.render(), ("short output\n", "short output\n", False))

    def test_environment_is_sanitized(self) -> None:
        values = {
            "GITHUB_TOKEN": "ghp_" + "A" * 36, "MY_API_KEY": "key-123456", "DB_PASSWORD": "hunter2hunter2",
            "AWS_SECRET_ACCESS_KEY": "abcdef123456", "PYTHONPATH": "C:\\evil", "PYTHONSTARTUP": "C:\\evil.py",
            "PYTEST_ADDOPTS": "-p evil_plugin", "PYTEST_PLUGINS": "evil", "GIT_DIR": "C:\\other\\.git",
            "HARMLESS_SETTING": "keep-me",
        }  # fmt: skip
        with mock.patch.dict(os.environ, values):
            env, removed, secrets = build_test_environment()
        for name in values:
            if name == "HARMLESS_SETTING":
                self.assertEqual(env[name], "keep-me")
            else:
                self.assertNotIn(name, env)
                self.assertIn(name, removed)
        self.assertEqual((env["PYTHONDONTWRITEBYTECODE"], env["PY_COLORS"]), ("1", "0"))
        self.assertIn("hunter2hunter2", secrets)
        self.assertNotIn("C:\\evil", secrets)  # removed for safety, not because it is a secret

    def test_redactor(self) -> None:
        redact = Redactor(["hunter2hunter2"], {"C:\\ws": "<workspace>"})
        text = redact("password hunter2hunter2 in C:\\ws\\x.py token ghp_" + "B" * 36)
        self.assertEqual(text, "password [REDACTED] in <workspace>\\x.py token [REDACTED]")
        self.assertEqual(redact.count, 2)

    def test_only_fixed_command_shapes_can_be_built(self) -> None:
        self.assertEqual(set(COMMANDS), {"pytest", "unittest"})
        self.assertEqual(COMMANDS["unittest"], ["python", "-m", "unittest"])
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "tests").mkdir()
            workspace = Workspace(root)
            python = detection.Interpreter(Path(sys.executable), "devpilot")
            argv = runner.build_argv(workspace, python, COMMANDS["pytest"])
            self.assertEqual(argv, [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"])
            self.assertTrue(Path(argv[0]).is_absolute())
            self.assertEqual(
                runner.build_argv(workspace, python, ["python", "-m", "unittest", "discover", "-s", "tests"])[1:],
                ["-m", "unittest", "discover", "-s", "tests"],
            )
            tampered = [
                ["cmd", "/c", "whoami"], ["python", "-c", "import os"], ["python", "-m", "pip", "install", "x"],
                ["python", "-m", "pytest", "-p", "evil"], ["python", "-m", "unittest", "; whoami"],
                ["python", "-m", "unittest", "discover", "-s", "../.."], ["python", "-m", "unittest", "discover", "-s", "-c"],
                ["python", "-m", "unittest", "discover", "-s", "C:\\Windows"], ["python", "-m", "unittest", "discover", "-s", "missing"],
                ["python", "-m", "unittest", "discover", "-s", "."], ["python", "-m", "unittest", "discover", "-t", "tests"],
                ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "*.py"], [],
            ]  # fmt: skip
            for command in tampered:
                with self.subTest(command=command):
                    with self.assertRaises(WorkspaceError):
                        runner.build_argv(workspace, python, command)


# --- Execution -----------------------------------------------------------------------


class RunTestsTests(WorkspaceCase):
    def test_successful_tests(self) -> None:
        self.write({"tests/__init__.py": "", "tests/test_math.py": PASSING})
        result = run_tests(self.workspace)
        self.assertEqual((result.framework, result.status, result.exit_code), ("unittest", "passed", 0))
        self.assertEqual((result.passed, result.failed, result.errors, result.skipped, result.total), (2, 0, 0, 1, 3))
        self.assertEqual(result.command, ["python", "-m", "unittest"])
        self.assertIn("Ran 3 tests", result.stderr)
        self.assertFalse(result.timed_out or result.output_truncated)
        self.assertEqual(result.side_effects.total_changes, 0)
        self.assertGreater(result.duration_seconds, 0)

    def test_non_package_test_directory(self) -> None:
        # tests/ without __init__.py: root discovery (Python 3.11+) would find nothing.
        self.write({"tests/test_math.py": PASSING, "tests/unit/test_more.py": PASSING})
        detected = self.detect_primary_command()
        self.assertEqual(detected, ["python", "-m", "unittest", "discover", "-s", "tests"])
        result = run_tests(self.workspace)
        self.assertEqual((result.status, result.passed, result.command), ("passed", 2, detected))
        self.assertTrue(any("sub-directories without __init__.py" in w for w in self.detect().warnings))

    def detect_primary_command(self) -> list[str]:
        return self.detect().primary.command

    def test_failing_tests(self) -> None:
        self.write({"tests/test_broken.py": FAILING})
        result = run_tests(self.workspace, "unittest")
        self.assertEqual((result.status, result.exit_code), ("failed", 1))
        self.assertEqual((result.passed, result.failed, result.errors, result.total), (1, 1, 1, 3))
        self.assertIn("AssertionError", result.stderr)

    def test_no_tests_collected(self) -> None:
        self.write({"tests/test_empty.py": "import unittest\n\n\nclass TestNothing(unittest.TestCase):\n    pass\n"})
        result = run_tests(self.workspace)
        self.assertEqual((result.status, result.total), ("no_tests", 0))

    def test_timeout(self) -> None:
        self.write({"tests/test_slow.py": "import time, unittest\n\n\nclass TestSlow(unittest.TestCase):\n"
                                          "    def test_sleep(self):\n        time.sleep(60)\n"})  # fmt: skip
        started = time.monotonic()
        result = run_tests(self.workspace, timeout_seconds=2)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((result.status, result.timed_out, result.passed), ("timed_out", True, None))
        self.assertIn("exceeded 2 seconds", result.warnings[0])

    def test_output_truncation_keeps_the_summary(self) -> None:
        self.write({"tests/test_noisy.py": "import sys, unittest\n\n\nclass TestNoisy(unittest.TestCase):\n"
                                           "    def test_noise(self):\n        sys.stderr.write('noise ' * 50000)\n"
                                           "        print('out ' * 50000)\n"})  # fmt: skip
        result = run_tests(self.workspace)
        self.assertTrue(result.output_truncated)
        self.assertEqual((result.status, result.passed), ("passed", 1))  # parsed from the kept tail
        self.assertGreater(result.stderr_bytes, 250_000)
        self.assertLess(len(result.stderr) + len(result.stdout), 2 * (runner.OUTPUT_HEAD_BYTES + runner.OUTPUT_TAIL_BYTES) + 200)

    def test_side_effects_are_reported_not_reverted(self) -> None:
        self.write({"tests/test_writes.py": "import unittest\n\n\nclass TestWrites(unittest.TestCase):\n"
                                            "    def test_write(self):\n        open('created_by_test.txt', 'w').write('x')\n"})  # fmt: skip
        result = run_tests(self.workspace)
        self.assertEqual(result.side_effects.files_created, ["created_by_test.txt"])
        self.assertTrue((self.root / "created_by_test.txt").exists())  # DevPilot did not revert it
        self.assertTrue(any("did not revert" in w for w in result.warnings))

    def test_secrets_cwd_and_paths(self) -> None:
        token = "ghp_" + "Q1w2E3r4" * 5
        self.write({"tests/test_env.py": "import os, unittest\n\n\nclass TestEnv(unittest.TestCase):\n"
                                         "    def test_env(self):\n        print('TOKEN', os.environ.get('GITHUB_TOKEN'))\n"
                                         "        print('CWD', os.getcwd())\n"
                                         f"        print('LITERAL', '{token}')\n"})  # fmt: skip
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": token, "SERVICE_PASSWORD": "pa55w0rd-value"}):
            result = run_tests(self.workspace)
        self.assertEqual(result.status, "passed")
        self.assertIn("TOKEN None", result.stdout)  # the variable never reached the test process
        self.assertIn("CWD <workspace>", result.stdout)  # ran in the workspace root, path masked
        self.assertIn("LITERAL [REDACTED]", result.stdout)
        self.assertNotIn(token, result.model_dump_json())
        self.assertNotIn(str(self.root), result.model_dump_json())
        self.assertIn("GITHUB_TOKEN", result.removed_environment_variables)
        self.assertIn("SERVICE_PASSWORD", result.removed_environment_variables)

    def test_interpreter_failures(self) -> None:
        self.write({"tests/test_math.py": PASSING})
        with mock.patch.object(runner.subprocess, "Popen", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(TestRunError, "could not be found"):
                run_tests(self.workspace)
        fake = self.base / "python.exe"
        fake.write_bytes(b"not a program")
        os.environ[detection.TEST_PYTHON_ENV_VAR] = str(fake)
        with self.assertRaisesRegex(TestRunError, "could not be (started|found)"):
            run_tests(self.workspace)
        os.environ[detection.TEST_PYTHON_ENV_VAR] = "relative/python"
        with self.assertRaises(TestConfigurationError):
            run_tests(self.workspace)

    def test_pytest_not_installed_is_reported(self) -> None:
        if importlib.util.find_spec("pytest") is not None:
            self.skipTest("pytest is installed here.")
        self.write({"pytest.ini": "[pytest]\n", "tests/test_x.py": "def test_x():\n    assert True\n"})
        result = run_tests(self.workspace)
        self.assertEqual((result.framework, result.status, result.passed), ("pytest", "error", None))
        self.assertIn("pytest is not installed in the test interpreter.", result.warnings)
        self.assertIn("<python>", result.stderr)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed in this interpreter.")
    def test_real_pytest_run(self) -> None:
        self.write({"pytest.ini": "[pytest]\n", "tests/test_x.py": "import pytest\n\ndef test_ok():\n    assert True\n\n"
                                                                     "@pytest.mark.skip\ndef test_skip():\n    pass\n"})  # fmt: skip
        result = run_tests(self.workspace)
        self.assertEqual((result.status, result.passed, result.skipped), ("passed", 1, 1))
        self.assertFalse((self.root / ".pytest_cache").exists())

    def test_pytest_results_through_a_fake_process(self) -> None:
        self.write({"pytest.ini": "", "tests/test_x.py": "def test_x():\n    assert True\n"})

        def fake(stdout: str, code: int) -> ProcessOutcome:
            out, err = _Stream(), _Stream()
            out.feed(stdout.encode())
            return ProcessOutcome(code, out, err, False, 0.5)

        cases = [
            ("===== 1 failed, 3 passed, 1 skipped in 0.20s =====\n", 1, "failed", (3, 1, 0, 1)),
            ("===== 4 passed in 0.10s =====\n", 0, "passed", (4, 0, 0, 0)),
            ("===== no tests ran in 0.01s =====\n", 5, "no_tests", (0, 0, 0, 0)),
            ("ERROR: usage: pytest [options]\n", 4, "error", (None, None, None, None)),
        ]
        for stdout, code, status, counts in cases:
            with self.subTest(status=status), mock.patch.object(runner, "run_process", return_value=fake(stdout, code)):
                result = run_tests(self.workspace, "pytest")
                self.assertEqual(result.status, status)
                self.assertEqual((result.passed, result.failed, result.errors, result.skipped), counts)


class ExecutionSecurityTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.write({"tests/__init__.py": "", "tests/test_math.py": PASSING})

    def test_injection_attempts_never_start_a_process(self) -> None:
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            for payload in INJECTIONS + [["pytest"], {"command": "whoami"}, 1]:
                with self.subTest(payload=repr(payload)):
                    with self.assertRaises(TestRunError):
                        run_tests(self.workspace, payload)
            for timeout in (0, -1, 601, 10_000, True, "10", 2.5, None):
                with self.subTest(timeout=repr(timeout)):
                    with self.assertRaises(TestRunError):
                        run_tests(self.workspace, None, timeout)
        popen.assert_not_called()

    def test_undetected_or_missing_frameworks_are_refused(self) -> None:
        with self.assertRaisesRegex(TestRunError, "'pytest' was not detected"):
            run_tests(self.workspace, "pytest")
        (self.base / "empty").mkdir()
        with self.assertRaisesRegex(TestRunError, "No supported test framework"):
            run_tests(Workspace(self.base / "empty"))

    def test_process_is_launched_without_a_shell_in_the_workspace(self) -> None:
        with mock.patch.object(runner.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_" + "Z" * 36, "PYTEST_ADDOPTS": "-p evil"}):
                run_tests(self.workspace, timeout_seconds=60)
        python_calls = [c for c in popen.call_args_list if c.args[0][0] == sys.executable]
        self.assertEqual(len(python_calls), 1)
        argv, kwargs = python_calls[0].args[0], python_calls[0].kwargs
        self.assertEqual(argv, [sys.executable, "-m", "unittest"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(Path(kwargs["cwd"]), self.root)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        self.assertNotIn("PYTEST_ADDOPTS", kwargs["env"])
        for call in popen.call_args_list:  # nothing but the interpreter (and read-only git) is ever launched
            self.assertTrue(Path(call.args[0][0]).name.lower().startswith(("python", "git")), call.args[0][0])

    def test_concurrent_runs_are_refused(self) -> None:
        with runner._run_lock:
            with self.assertRaisesRegex(TestRunError, "already in progress"):
                run_tests(self.workspace)


# --- Syntax and validation -----------------------------------------------------------


class SyntaxTests(WorkspaceCase):
    def test_success_errors_and_warnings(self) -> None:
        self.write({
            "good.py": "def f(x):\n    return x * 2\n",
            "bad.py": "def broken(:\n    pass\n",
            "indent.py": "if True:\npass\n",
            "warn.py": "PATTERN = '\\d+'\n",
            "latin.py": "# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n".encode("latin-1"),
            "stub.pyi": "def f(x: int) -> int: ...\n",
            "node_modules/pkg/ignored.py": "def (:\n",
        })  # fmt: skip
        report = check_python_syntax(self.workspace)
        self.assertEqual((report.files_checked, report.error_count), (6, 2))
        self.assertEqual([(e.path, e.line) for e in report.errors], [("bad.py", 1), ("indent.py", 2)])
        self.assertIn("IndentationError", report.errors[1].message)
        # An invalid escape such as '\d' is a SyntaxWarning from Python 3.12; 3.11 raises a DeprecationWarning.
        self.assertEqual([w.path for w in report.warnings], ["warn.py"] if sys.version_info >= (3, 12) else [])
        self.assertTrue(report.complete)

    def test_parsing_never_executes_code(self) -> None:
        marker = self.base / "executed.txt"
        self.write({"payload.py": f"open(r'{marker}', 'w').write('ran')\nimport os\nos.system('whoami')\n"})
        report = check_python_syntax(self.workspace)
        self.assertEqual((report.files_checked, report.error_count), (1, 0))
        self.assertFalse(marker.exists())


class ValidationTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(detection.TEST_PYTHON_ENV_VAR, None)
        self.write("tests/__init__.py", "")
        self.write("tests/test_math.py", PASSING)
        self.write("app.py", "def main():\n    return 0\n")
        self.commit("Initial commit")

    def statuses(self, report) -> dict[str, str]:
        return {c.name: c.status for c in report.checks}

    def test_clean_repository_with_tests(self) -> None:
        report = validate_repository(self.workspace, run_tests=True)
        self.assertEqual(
            [(c.name, c.status) for c in report.checks],
            [("repository_analysis", "passed"), ("git_working_tree", "passed"), ("test_discovery", "passed"),
             ("test_execution", "passed"), ("python_syntax", "passed")],
        )  # fmt: skip
        self.assertEqual((report.failures, report.skipped, report.complete), ([], [], True))
        self.assertTrue(report.tests.executed and report.git.clean)
        self.assertEqual(report.tests.result.passed, 2)
        self.assertFalse({"score", "rating", "quality", "grade"} & set(report.model_dump()))

    def test_default_executes_nothing(self) -> None:
        with mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
            report = validate_repository(self.workspace)
        self.assertFalse(any(Path(c.args[0][0]) == Path(sys.executable) for c in popen.call_args_list))
        self.assertEqual(self.statuses(report)["test_execution"], "skipped")
        self.assertEqual(report.skipped, ["test_execution: Not requested (run_tests is false); no test code was executed."])
        self.assertFalse(report.tests.executed)
        self.assertTrue(report.tests.discovery is not None and not report.tests.execution_requested)

    def test_dirty_repository_failing_tests_and_syntax_errors(self) -> None:
        self.write("tests/test_math.py", FAILING)
        self.write("broken.py", "def broken(:\n")
        report = validate_repository(self.workspace, run_tests=True)
        statuses = self.statuses(report)
        self.assertEqual(
            (statuses["git_working_tree"], statuses["test_execution"], statuses["python_syntax"]),
            ("warning", "failed", "failed"),
        )
        self.assertEqual([f.split(":")[0] for f in report.failures], ["test_execution", "python_syntax"])
        self.assertIn("broken.py:1", report.failures[1])
        self.assertTrue(any(w.startswith("git_working_tree:") for w in report.warnings))
        self.assertEqual(report.tests.result.failed, 1)

    def test_discovery_failure_skips_execution(self) -> None:
        for rel in ("tests/test_math.py", "tests/__init__.py"):
            (self.root / rel).unlink()
        report = validate_repository(self.workspace, run_tests=True)
        statuses = self.statuses(report)
        self.assertEqual((statuses["test_discovery"], statuses["test_execution"]), ("warning", "skipped"))
        self.assertIn("No test framework was detected", report.skipped[0])

    def test_non_git_workspace(self) -> None:
        plain = self.base / "plain"
        write_files(plain, {"tests/test_math.py": PASSING})
        report = validate_repository(Workspace(plain))
        self.assertEqual(self.statuses(report)["git_working_tree"], "skipped")
        self.assertFalse(report.git.available)
        self.assertTrue(report.complete)

    def test_deterministic_and_no_writes_or_git_changes(self) -> None:
        before = snapshot(self.root)  # includes every file under .git
        first = validate_repository(self.workspace)
        second = validate_repository(self.workspace)
        self.assertEqual(first.model_dump(), second.model_dump())
        validate_repository(self.workspace, run_tests=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(first.git.clean)

    def test_invalid_arguments(self) -> None:
        for kwargs in ({"run_tests": "yes"}, {"run_tests": 1}, {"timeout_seconds": 0}, {"timeout_seconds": 601}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkspaceError):
                    validate_repository(self.workspace, **kwargs)


# --- MCP -----------------------------------------------------------------------------


class TestingMcpTests(WorkspaceCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write({"tests/__init__.py": "", "tests/test_math.py": PASSING})

    async def test_registration_schemas_and_annotations(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        self.assertEqual(len(tools), 18)
        self.assertEqual(tools["get_test_commands"].input_schema.get("properties", {}), {})
        self.assertTrue(tools["get_test_commands"].annotations.read_only_hint)
        run_props = tools["run_tests"].input_schema["properties"]
        self.assertEqual(set(run_props), {"framework", "timeout_seconds"})  # no command/executable/args
        self.assertEqual(run_props["framework"]["anyOf"][0]["enum"], ["pytest", "unittest"])
        self.assertEqual((run_props["timeout_seconds"]["minimum"], run_props["timeout_seconds"]["maximum"]), (1, 600))
        self.assertEqual(set(tools["validate_repository"].input_schema["properties"]), {"run_tests", "timeout_seconds"})
        for name in ("run_tests", "validate_repository"):
            self.assertFalse(tools[name].annotations.read_only_hint)
            self.assertTrue(tools[name].annotations.destructive_hint)

    async def test_round_trips(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            commands = await client.call_tool("get_test_commands", {})
            ran = await client.call_tool("run_tests", {"timeout_seconds": 60})
            report = await client.call_tool("validate_repository", {})
        self.assertEqual(commands.structured_content["primary"]["framework"], "unittest")
        self.assertEqual((ran.structured_content["status"], ran.structured_content["passed"]), ("passed", 2))
        self.assertEqual([c["name"] for c in report.structured_content["checks"]][3], "test_execution")

    async def test_invalid_and_injected_arguments(self) -> None:
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            async with Client(create_server(self.workspace)) as client:
                for args in ([{"framework": p} for p in INJECTIONS] + [{"timeout_seconds": 0}, {"timeout_seconds": 601},
                             {"timeout_seconds": "10; whoami"}, {"framework": ["pytest", "; whoami"]}]):  # fmt: skip
                    with self.subTest(args=args):
                        self.assertTrue((await client.call_tool("run_tests", args)).is_error)
                self.assertTrue((await client.call_tool("validate_repository", {"run_tests": "yes; whoami"})).is_error)
        popen.assert_not_called()

    async def test_extra_arguments_cannot_supply_a_command(self) -> None:
        extras = {"command": ["cmd", "/c", "whoami"], "executable": "C:\\Windows\\System32\\cmd.exe",
                  "shell": True, "args": ["-c", "import os"], "cwd": "C:\\", "env": {"PYTHONPATH": "x"}}  # fmt: skip
        with mock.patch.object(runner.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            async with Client(create_server(self.workspace)) as client:
                result = await client.call_tool("run_tests", {"timeout_seconds": 60, **extras})
        self.assertFalse(result.is_error)
        python_calls = [c for c in popen.call_args_list if c.args[0][0] == sys.executable]
        self.assertEqual(len(python_calls), 1)
        argv, kwargs = python_calls[0].args[0], python_calls[0].kwargs
        self.assertEqual(argv, [sys.executable, "-m", "unittest"])
        self.assertEqual((kwargs["shell"], Path(kwargs["cwd"])), (False, self.root))
        self.assertNotIn("PYTHONPATH", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
