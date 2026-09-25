"""Tests for the read-only Git tools, run against real throwaway repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from devpilot_mcp.server import create_server
from devpilot_mcp.tools import git
from devpilot_mcp.tools.git import (
    GitCommandError,
    GitCommandNotAllowedError,
    GitOutputError,
    GitResult,
    GitTimeoutError,
    GitUnavailableError,
    NotAGitRepositoryError,
)
from devpilot_mcp.workspace import PathNotFoundError, PathOutsideWorkspaceError, Workspace, WorkspaceError

GIT = shutil.which("git")


def _setup_env(date: str = "2026-01-01T12:00:00+00:00") -> dict[str, str]:
    """Environment for the tests' own setup commands, isolated from the user's Git config."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Ada Lovelace",
        GIT_AUTHOR_EMAIL="ada@example.com",
        GIT_COMMITTER_NAME="Ada Lovelace",
        GIT_COMMITTER_EMAIL="ada@example.com",
        GIT_AUTHOR_DATE=date,
        GIT_COMMITTER_DATE=date,
    )
    return env


@unittest.skipUnless(GIT, "Git is not installed.")
class GitRepoTestCase(unittest.TestCase):
    """A fresh repository at `<tmp>/repo` on branch main, with no commits yet."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.run_git("-c", "init.defaultBranch=main", "init", "-q")
        self.run_git("config", "core.autocrlf", "false")
        self.run_git("config", "commit.gpgsign", "false")
        self.workspace = Workspace(self.root)
        self._commit_count = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_git(self, *args: str, cwd: Path | None = None, date: str | None = None) -> str:
        env = _setup_env(date) if date else _setup_env()
        result = subprocess.run(
            [GIT, *args], cwd=cwd or self.root, env=env, capture_output=True, check=True
        )
        return result.stdout.decode("utf-8", "replace").strip()

    def write(self, rel: str, content: bytes | str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)

    def commit(self, message: str, *paths: str) -> str:
        self._commit_count += 1
        self.run_git("add", *(paths or ["-A"]))
        date = f"2026-01-{self._commit_count:02d}T12:00:00+00:00"
        self.run_git("commit", "-q", "--no-verify", "-m", message, date=date)
        return self.run_git("rev-parse", "HEAD")

    def committed_repo(self) -> str:
        self.write("app.py", "print('v1')\n")
        self.write("README.md", "# Demo\n")
        return self.commit("Initial commit")


# --- git_status --------------------------------------------------------------


class GitStatusTests(GitRepoTestCase):
    def test_clean_repository(self) -> None:
        head = self.committed_repo()
        status = git.git_status(self.workspace)
        self.assertTrue(status.clean)
        self.assertEqual(status.branch, "main")
        self.assertFalse(status.detached)
        self.assertEqual(status.head_commit, head)
        self.assertIsNone(status.upstream)
        self.assertEqual((status.staged, status.unstaged, status.untracked, status.deleted), ([], [], [], []))
        self.assertTrue(status.complete)

    def test_modified_file_is_unstaged(self) -> None:
        self.committed_repo()
        self.write("app.py", "print('v2')\n")
        status = git.git_status(self.workspace)
        self.assertFalse(status.clean)
        self.assertEqual([(c.path, c.change) for c in status.unstaged], [("app.py", "modified")])
        self.assertEqual(status.staged, [])

    def test_staged_files(self) -> None:
        self.committed_repo()
        self.write("app.py", "print('v2')\n")
        self.write("lib/new.py", "x = 1\n")
        self.run_git("add", "app.py", "lib/new.py")
        status = git.git_status(self.workspace)
        self.assertEqual(
            [(c.path, c.change) for c in status.staged], [("app.py", "modified"), ("lib/new.py", "added")]
        )
        self.assertEqual(status.unstaged, [])

    def test_file_both_staged_and_unstaged(self) -> None:
        self.committed_repo()
        self.write("app.py", "print('v2')\n")
        self.run_git("add", "app.py")
        self.write("app.py", "print('v3')\n")
        status = git.git_status(self.workspace)
        self.assertEqual([c.path for c in status.staged], ["app.py"])
        self.assertEqual([c.path for c in status.unstaged], ["app.py"])

    def test_untracked_files_listed_individually(self) -> None:
        self.committed_repo()
        self.write("notes.txt", "todo\n")
        self.write("new dir/naïve file.txt", "x\n")  # spaces and non-ASCII must survive
        status = git.git_status(self.workspace)
        self.assertEqual(status.untracked, ["new dir/naïve file.txt", "notes.txt"])
        self.assertEqual(status.counts["untracked"], 2)

    def test_deleted_files(self) -> None:
        self.committed_repo()
        (self.root / "app.py").unlink()
        self.run_git("rm", "-q", "README.md")
        status = git.git_status(self.workspace)
        self.assertEqual([(c.path, c.change) for c in status.unstaged], [("app.py", "deleted")])
        self.assertEqual([(c.path, c.change) for c in status.staged], [("README.md", "deleted")])
        self.assertEqual(status.deleted, ["README.md", "app.py"])

    def test_staged_rename(self) -> None:
        self.committed_repo()
        self.run_git("mv", "app.py", "main.py")
        status = git.git_status(self.workspace)
        self.assertEqual(
            [(c.path, c.change, c.original_path) for c in status.staged], [("main.py", "renamed", "app.py")]
        )

    def test_repository_without_commits(self) -> None:
        self.write("first.py", "")
        status = git.git_status(self.workspace)
        self.assertEqual(status.branch, "main")
        self.assertIsNone(status.head_commit)
        self.assertEqual(status.untracked, ["first.py"])

    def test_detached_head(self) -> None:
        head = self.committed_repo()
        self.run_git("checkout", "-q", "--detach")
        status = git.git_status(self.workspace)
        self.assertTrue(status.detached)
        self.assertIsNone(status.branch)
        self.assertEqual(status.head_commit, head)

    def test_merge_conflict(self) -> None:
        self.write("shared.txt", "base\n")
        self.commit("base")
        self.run_git("checkout", "-q", "-b", "feature")
        self.write("shared.txt", "feature\n")
        self.commit("feature change")
        self.run_git("checkout", "-q", "main")
        self.write("shared.txt", "main\n")
        self.commit("main change")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_git("merge", "-q", "feature")
        status = git.git_status(self.workspace)
        self.assertEqual(status.conflicted, ["shared.txt"])
        self.assertFalse(status.clean)

    def test_lists_are_capped_with_full_counts(self) -> None:
        self.committed_repo()
        for i in range(5):
            self.write(f"new_{i}.txt", "x")
        with mock.patch.object(git, "MAX_FILE_ENTRIES", 3):
            status = git.git_status(self.workspace)
        self.assertEqual(status.untracked, ["new_0.txt", "new_1.txt", "new_2.txt"])
        self.assertEqual(status.counts["untracked"], 5)
        self.assertEqual(status.truncated_fields, ["untracked"])


# --- git_log -----------------------------------------------------------------


class GitLogTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.hashes = []
        for i in range(1, 4):
            self.write("app.py", f"v{i}\n")
            self.hashes.append(self.commit(f"Commit {i}"))

    def test_recent_commits_newest_first(self) -> None:
        log = git.git_log(self.workspace)
        self.assertEqual([c.subject for c in log.commits], ["Commit 3", "Commit 2", "Commit 1"])
        self.assertEqual([c.hash for c in log.commits], list(reversed(self.hashes)))
        self.assertFalse(log.has_more)

    def test_commit_fields(self) -> None:
        self.write("app.py", "v4\n")
        self.run_git("add", "app.py")
        self.run_git(
            "commit", "-q", "-m", "Add feature", "-m", "Longer explanation.\n\nSecond paragraph.",
            date="2026-02-03T04:05:06+05:30",
        )  # fmt: skip
        commit = git.git_log(self.workspace, 1).commits[0]
        self.assertEqual(len(commit.hash), 40)
        self.assertTrue(commit.hash.startswith(commit.short_hash))
        self.assertEqual((commit.author_name, commit.author_email), ("Ada Lovelace", "ada@example.com"))
        self.assertEqual(commit.date, "2026-02-03T04:05:06+05:30")
        self.assertEqual(commit.parents, [self.hashes[-1]])
        self.assertEqual(commit.subject, "Add feature")
        self.assertEqual(commit.body, "Longer explanation.\n\nSecond paragraph.")
        self.assertFalse(commit.body_truncated)

    def test_limit_and_has_more(self) -> None:
        log = git.git_log(self.workspace, 2)
        self.assertEqual(log.limit, 2)
        self.assertEqual([c.subject for c in log.commits], ["Commit 3", "Commit 2"])
        self.assertTrue(log.has_more)

    def test_limit_bounds(self) -> None:
        self.assertEqual(len(git.git_log(self.workspace, git.MAX_LOG_LIMIT).commits), 3)
        for bad in (0, -1, git.MAX_LOG_LIMIT + 1, 1000, True, "5", 2.5, None):
            with self.subTest(limit=bad):
                with self.assertRaises(WorkspaceError):
                    git.git_log(self.workspace, bad)  # type: ignore[arg-type]

    def test_git_is_never_asked_for_more_than_limit_plus_one(self) -> None:
        with mock.patch.object(git, "_execute", wraps=git._execute) as execute:
            git.git_log(self.workspace, git.MAX_LOG_LIMIT)
        log_argv = next(call.args[0] for call in execute.call_args_list if "log" in call.args[0])
        self.assertIn(f"--max-count={git.MAX_LOG_LIMIT + 1}", log_argv)

    def test_long_body_is_truncated(self) -> None:
        self.write("app.py", "v4\n")
        self.run_git("add", "app.py")
        self.run_git("commit", "-q", "-m", "Big", "-m", "x" * 50)
        with mock.patch.object(git, "MAX_COMMIT_BODY_CHARS", 10):
            commit = git.git_log(self.workspace, 1).commits[0]
        self.assertEqual(commit.body, "x" * 10)
        self.assertTrue(commit.body_truncated)


class GitLogEmptyRepoTests(GitRepoTestCase):
    def test_repository_without_commits(self) -> None:
        log = git.git_log(self.workspace)
        self.assertEqual((log.commits, log.has_more), ([], False))


# --- git_diff ----------------------------------------------------------------


class GitDiffTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("app.py", "line 1\nline 2\nline 3\n")
        self.write("docs/guide.md", "# Guide\n")
        self.write("logo.bin", b"\x00\x01\x02")
        self.commit("Initial commit")

    def test_no_changes(self) -> None:
        for staged in (False, True):
            with self.subTest(staged=staged):
                diff = git.git_diff(self.workspace, staged=staged)
                self.assertEqual((diff.files, diff.diff, diff.truncated), ([], "", False))
                self.assertEqual((diff.files_changed, diff.additions, diff.deletions), (0, 0, 0))

    def test_unstaged_changes(self) -> None:
        self.write("app.py", "line 1\nline two\nline 3\nline 4\n")
        diff = git.git_diff(self.workspace)
        self.assertFalse(diff.staged)
        self.assertEqual(
            [(f.path, f.change, f.additions, f.deletions, f.binary) for f in diff.files],
            [("app.py", "modified", 2, 1, False)],
        )
        self.assertEqual((diff.additions, diff.deletions), (2, 1))
        self.assertIn("-line 2\n+line two\n", diff.diff)
        self.assertIn("diff --git a/app.py b/app.py", diff.diff)
        self.assertEqual(git.git_diff(self.workspace, staged=True).files, [])

    def test_staged_changes(self) -> None:
        self.write("app.py", "line 1\nline 2\nline 3\nstaged\n")
        self.write("new.py", "created\n")
        self.run_git("add", "app.py", "new.py")
        diff = git.git_diff(self.workspace, staged=True)
        self.assertTrue(diff.staged)
        self.assertEqual([(f.path, f.change) for f in diff.files], [("app.py", "modified"), ("new.py", "added")])
        self.assertIn("+staged", diff.diff)
        self.assertEqual(git.git_diff(self.workspace, staged=False).files, [])

    def test_deleted_file_and_path_filter(self) -> None:
        (self.root / "app.py").unlink()
        self.write("docs/guide.md", "# Guide v2\n")
        everything = git.git_diff(self.workspace)
        self.assertEqual([(f.path, f.change) for f in everything.files], [("app.py", "deleted"), ("docs/guide.md", "modified")])
        # A path that no longer exists on disk can still be diffed.
        only_app = git.git_diff(self.workspace, path="app.py")
        self.assertEqual(only_app.path, "app.py")
        self.assertEqual([f.path for f in only_app.files], ["app.py"])
        only_docs = git.git_diff(self.workspace, path="docs\\")
        self.assertEqual([f.path for f in only_docs.files], ["docs/guide.md"])

    def test_touched_but_unchanged_file_is_not_reported(self) -> None:
        stamp = time.time() + 120
        os.utime(self.root / "docs" / "guide.md", (stamp, stamp))  # stale stat data, same content
        self.write("app.py", "line 1\n")
        diff = git.git_diff(self.workspace)
        self.assertEqual([f.path for f in diff.files], ["app.py"])
        self.assertNotIn("guide.md", diff.diff)

    def test_binary_file(self) -> None:
        self.write("logo.bin", b"\x00\x09\x09")
        diff = git.git_diff(self.workspace)
        self.assertEqual(
            [(f.path, f.binary, f.additions, f.deletions) for f in diff.files], [("logo.bin", True, None, None)]
        )
        self.assertIn("Binary files", diff.diff)

    def test_staged_rename(self) -> None:
        self.run_git("mv", "app.py", "main.py")
        diff = git.git_diff(self.workspace, staged=True)
        self.assertEqual(
            [(f.path, f.change, f.original_path) for f in diff.files], [("main.py", "renamed", "app.py")]
        )

    def test_large_diff_is_truncated(self) -> None:
        self.write("app.py", "".join(f"changed line {i}\n" for i in range(500)))
        with mock.patch.object(git, "MAX_DIFF_BYTES", 400):
            diff = git.git_diff(self.workspace)
        self.assertTrue(diff.truncated)
        self.assertLessEqual(diff.diff_bytes, 400)
        self.assertEqual(diff.diff_bytes, len(diff.diff.encode()))
        self.assertTrue(diff.diff.endswith("\n"))
        self.assertIn("truncated", diff.warnings[0])
        # File statistics are still complete.
        self.assertEqual(diff.files[0].additions, 500)

    def test_file_list_is_capped(self) -> None:
        for i in range(4):
            self.write(f"f{i}.txt", "x\n")
        self.run_git("add", "-A")
        with mock.patch.object(git, "MAX_FILE_ENTRIES", 2):
            diff = git.git_diff(self.workspace, staged=True)
        self.assertEqual(len(diff.files), 2)
        self.assertEqual(diff.files_changed, 4)
        self.assertTrue(diff.truncated)

    def test_invalid_arguments(self) -> None:
        with mock.patch.object(git, "_execute") as execute:
            for bad in ("../", "../../secret", "C:\\Windows", "D:\\other-project", str(self.base)):
                with self.subTest(path=bad):
                    with self.assertRaises(PathOutsideWorkspaceError):
                        git.git_diff(self.workspace, path=bad)
            with self.assertRaises(WorkspaceError):
                git.git_diff(self.workspace, staged="yes")  # type: ignore[arg-type]
        execute.assert_not_called()  # rejected before any Git process starts

    def test_option_like_path_is_only_a_pathspec(self) -> None:
        self.write("app.py", "changed\n")
        diff = git.git_diff(self.workspace, path="--output=pwned.txt")
        self.assertEqual(diff.files, [])
        self.assertFalse((self.root / "pwned.txt").exists())


# --- git_branch --------------------------------------------------------------


class GitBranchTests(GitRepoTestCase):
    def test_current_branch_only(self) -> None:
        head = self.committed_repo()
        branches = git.git_branch(self.workspace)
        self.assertEqual(branches.current_branch, "main")
        self.assertFalse(branches.detached)
        self.assertEqual(
            [(b.name, b.current, b.upstream) for b in branches.branches], [("main", True, None)]
        )
        self.assertTrue(head.startswith(branches.branches[0].commit))

    def test_multiple_branches_sorted(self) -> None:
        self.committed_repo()
        self.run_git("branch", "zeta")
        self.run_git("branch", "alpha")
        self.run_git("branch", "feature/login")
        branches = git.git_branch(self.workspace)
        self.assertEqual([b.name for b in branches.branches], ["alpha", "feature/login", "main", "zeta"])
        self.assertEqual([b.name for b in branches.branches if b.current], ["main"])
        self.assertEqual(branches.total_branches, 4)

    def test_detached_head(self) -> None:
        self.committed_repo()
        self.run_git("checkout", "-q", "--detach")
        branches = git.git_branch(self.workspace)
        self.assertIsNone(branches.current_branch)
        self.assertTrue(branches.detached)
        self.assertEqual([b.name for b in branches.branches], ["main"])  # no "(HEAD detached ...)" entry
        self.assertFalse(any(b.current for b in branches.branches))

    def test_repository_without_commits(self) -> None:
        branches = git.git_branch(self.workspace)
        self.assertEqual((branches.current_branch, branches.branches), ("main", []))

    def test_branch_list_is_capped(self) -> None:
        self.committed_repo()
        for name in ("a", "b", "c"):
            self.run_git("branch", name)
        with mock.patch.object(git, "MAX_BRANCHES", 2):
            branches = git.git_branch(self.workspace)
        self.assertEqual([b.name for b in branches.branches], ["a", "b"])
        self.assertEqual((branches.total_branches, branches.truncated), (4, True))


# --- Repository detection and error handling ---------------------------------

ALL_TOOLS = (git.git_status, git.git_log, git.git_diff, git.git_branch)


@unittest.skipUnless(GIT, "Git is not installed.")
class RepositoryDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_git_workspace(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        for tool in ALL_TOOLS:
            with self.subTest(tool=tool.__name__):
                with self.assertRaises(NotAGitRepositoryError):
                    tool(Workspace(plain))
        self.assertFalse((plain / ".git").exists())  # nothing was initialized

    def test_parent_repository_is_not_used(self) -> None:
        outer = self.base / "outer"
        (outer / "inner").mkdir(parents=True)
        subprocess.run([GIT, "init", "-q"], cwd=outer, env=_setup_env(), check=True)
        with self.assertRaises(NotAGitRepositoryError):
            git.git_status(Workspace(outer / "inner"))

    def test_missing_workspace(self) -> None:
        workspace = Workspace(self.base / "gone")
        for tool in ALL_TOOLS:
            with self.subTest(tool=tool.__name__):
                with self.assertRaises(PathNotFoundError):
                    tool(workspace)

    def test_workspace_is_a_file(self) -> None:
        file_path = self.base / "file.txt"
        file_path.write_text("x")
        with self.assertRaises(PathNotFoundError):
            git.git_status(Workspace(file_path))


class ErrorHandlingTests(GitRepoTestCase):
    def test_git_not_on_path(self) -> None:
        with mock.patch.object(git.shutil, "which", return_value=None):
            with self.assertRaisesRegex(GitUnavailableError, "not found"):
                git.git_status(self.workspace)

    def test_git_executable_missing(self) -> None:
        with mock.patch.object(git.shutil, "which", return_value=str(self.base / "no-such-git.exe")):
            with self.assertRaises(GitUnavailableError):
                git.git_status(self.workspace)

    def _fake_git(self, status_result: GitResult):
        real_execute = git._execute

        def fake(argv, cwd, env, *, timeout, max_bytes):
            if "status" in argv:
                return status_result
            return real_execute(argv, cwd, env, timeout=timeout, max_bytes=max_bytes)

        return mock.patch.object(git, "_execute", side_effect=fake)

    def test_git_error_is_sanitized(self) -> None:
        stderr = f"warning: noise\nfatal: something broke in '{self.root.as_posix()}'\n"
        with self._fake_git(GitResult(128, b"", stderr, False)):
            with self.assertRaises(GitCommandError) as ctx:
                git.git_status(self.workspace)
        message = str(ctx.exception)
        self.assertEqual(message, "git status failed: fatal: something broke in '<workspace>'")
        self.assertNotIn(str(self.base), message)

    def test_malformed_output(self) -> None:
        with self._fake_git(GitResult(0, b"Z not porcelain\0", "", False)):
            with self.assertRaises(GitOutputError):
                git.git_status(self.workspace)

    def test_truncated_status_output_is_marked_incomplete(self) -> None:
        raw = b"# branch.oid abc\0# branch.head main\0? a.txt\0? b.t"
        with self._fake_git(GitResult(0, raw, "", True)):
            status = git.git_status(self.workspace)
        self.assertEqual(status.untracked, ["a.txt"])  # the partial last record is dropped
        self.assertFalse(status.complete)


class ExecuteTests(unittest.TestCase):
    """The subprocess runner itself, exercised with a harmless Python child process."""

    def run_python(self, code: str, *, timeout: float = 10, max_bytes: int = 1000) -> GitResult:
        return git._execute(
            [sys.executable, "-c", code], Path.cwd(), dict(os.environ), timeout=timeout, max_bytes=max_bytes
        )

    def test_timeout(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(GitTimeoutError, "timed out"):
            self.run_python("import time; time.sleep(30)", timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)

    def test_stdout_is_capped_while_reading(self) -> None:
        result = self.run_python("import sys; sys.stdout.write('x' * 5_000_000)", max_bytes=1000)
        self.assertTrue(result.truncated)
        self.assertEqual(result.stdout, b"x" * 1000)

    def test_large_stderr_does_not_deadlock(self) -> None:
        result = self.run_python("import sys; sys.stderr.write('e' * 2_000_000); print('done')")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, b"done"))

    def test_nonzero_exit_code_is_reported(self) -> None:
        result = self.run_python("import sys; sys.exit(3)")
        self.assertEqual(result.returncode, 3)


# --- Security ----------------------------------------------------------------


class ExecutionBoundaryTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.committed_repo()
        self.write("app.py", "print('v2')\n")

    def test_only_allow_listed_subcommands_can_run(self) -> None:
        forbidden = ["add", "commit", "push", "pull", "fetch", "checkout", "switch", "reset", "merge", "rebase",
                     "clean", "rm", "mv", "init", "clone", "config", "gc", "!sh", "--exec-path=x", ""]  # fmt: skip
        with mock.patch.object(git, "_execute") as execute:
            for subcommand in forbidden:
                with self.subTest(subcommand=subcommand):
                    with self.assertRaises(GitCommandNotAllowedError):
                        git._run_git(self.workspace, subcommand)
        execute.assert_not_called()

    def test_every_invocation_is_fixed_and_read_only(self) -> None:
        with mock.patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            git.git_status(self.workspace)
            git.git_log(self.workspace, 5)
            git.git_diff(self.workspace)
            git.git_diff(self.workspace, staged=True, path="app.py")
            git.git_branch(self.workspace)

        expected_prefix = [shutil.which("git"), "--no-pager", "--literal-pathspecs"]
        for pair in git.SAFE_CONFIG:
            expected_prefix += ["-c", pair]
        subcommands = set()
        for call in popen.call_args_list:
            argv, kwargs = call.args[0], call.kwargs
            self.assertIsInstance(argv, list)
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(Path(kwargs["cwd"]), self.root)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(argv[: len(expected_prefix)], expected_prefix)
            subcommand, *rest = argv[len(expected_prefix) :]
            subcommands.add(subcommand)
            if subcommand == "branch":  # listing only; no name that could create, delete or switch
                self.assertTrue(all(arg.startswith("--") for arg in rest), rest)
            if "--" in rest:  # user-supplied paths only ever appear after "--"
                self.assertEqual(rest.index("--"), len(rest) - 2)
        self.assertEqual(subcommands, {"rev-parse", "status", "log", "diff", "diff-files", "branch"})
        self.assertTrue(subcommands <= git.ALLOWED_SUBCOMMANDS)

    def test_environment_cannot_redirect_git(self) -> None:
        # Another repository that inherited GIT_* variables try to point Git at.
        other = self.base / "other"
        other.mkdir()
        self.run_git("-c", "init.defaultBranch=main", "init", "-q", cwd=other)
        (other / "x.txt").write_text("x")
        self.run_git("add", "x.txt", cwd=other)
        self.run_git("commit", "-q", "-m", "OTHER REPOSITORY", cwd=other)

        hostile = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
            "GIT_INDEX_FILE": str(other / ".git" / "index"),
            "GIT_CONFIG_PARAMETERS": "'core.pager'='evil'",
            "git_ceiling_directories": "",
        }
        with mock.patch.dict(os.environ, hostile), mock.patch.object(
            git.subprocess, "Popen", wraps=subprocess.Popen
        ) as popen:
            log = git.git_log(self.workspace)
            status = git.git_status(self.workspace)
        self.assertEqual([c.subject for c in log.commits], ["Initial commit"])
        self.assertEqual([c.path for c in status.unstaged], ["app.py"])

        env = popen.call_args.kwargs["env"]
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS"):
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_CEILING_DIRECTORIES"], str(self.root.parent))

    def test_tools_expose_no_repository_or_command_arguments(self) -> None:
        import asyncio

        tools = {t.name: t for t in asyncio.run(create_server(self.workspace).list_tools())}
        self.assertEqual(set(tools["git_status"].input_schema.get("properties", {})), set())
        self.assertEqual(set(tools["git_branch"].input_schema.get("properties", {})), set())
        self.assertEqual(set(tools["git_log"].input_schema["properties"]), {"limit"})
        self.assertEqual(set(tools["git_diff"].input_schema["properties"]), {"staged", "path"})

    def test_tools_do_not_modify_the_repository(self) -> None:
        self.write("notes.txt", "untracked\n")
        self.run_git("add", "app.py")
        self.write("app.py", "print('v3')\n")
        # Make the index stat data stale so a normal `git status` would rewrite .git/index.
        stamp = time.time() + 120
        os.utime(self.root / "README.md", (stamp, stamp))

        def snapshot() -> dict[str, tuple[int, int, bytes]]:
            return {
                str(p.relative_to(self.root)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                for p in sorted(self.root.rglob("*"))
                if p.is_file()
            }

        before = snapshot()
        git.git_status(self.workspace)
        git.git_log(self.workspace)
        git.git_diff(self.workspace)
        git.git_diff(self.workspace, staged=True)
        git.git_branch(self.workspace)
        self.assertEqual(snapshot(), before)


class RepositoryConfiguredProgramTests(GitRepoTestCase):
    """Repository config can make read-only Git commands launch programs; the tools must not.

    Each test first proves the vector is live with plain Git, then checks the tool.
    """

    def setUp(self) -> None:
        super().setUp()
        self.committed_repo()
        self.write("app.py", "print('changed')\n")
        self.marker = self.base / "pwned.txt"
        self.payload = f"echo pwned > '{self.marker.as_posix()}'"

    def assert_blocked(self, control_args: list[str], tool) -> None:
        self.run_git(*control_args)
        if not self.marker.exists():
            self.skipTest("This Git build did not launch the configured program.")
        self.marker.unlink()
        tool()
        self.assertFalse(self.marker.exists(), "the tool launched a repository-configured program")

    def test_fsmonitor_hook_is_not_run(self) -> None:
        self.run_git("config", "core.fsmonitor", self.payload)
        self.assert_blocked(["status"], lambda: git.git_status(self.workspace))

    def test_external_diff_is_not_run(self) -> None:
        self.run_git("config", "diff.external", self.payload)
        self.assert_blocked(["diff"], lambda: git.git_diff(self.workspace))

    def test_textconv_is_not_run(self) -> None:
        self.write(".gitattributes", "*.py diff=evil\n")
        self.run_git("config", "diff.evil.textconv", f"{self.payload}; cat")
        self.assert_blocked(["diff"], lambda: git.git_diff(self.workspace))


# --- MCP round trips ---------------------------------------------------------


@unittest.skipUnless(GIT, "Git is not installed.")
class GitMcpTests(GitRepoTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.committed_repo()
        self.write("app.py", "print('v2')\n")
        self.write("staged.py", "x = 1\n")
        self.run_git("add", "staged.py")

    async def call(self, tool: str, args: dict | None = None, workspace: Workspace | None = None):
        async with Client(create_server(workspace or self.workspace)) as client:
            return await client.call_tool(tool, args or {})

    async def test_git_tools_are_registered_read_only(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("git_status", "git_log", "git_diff", "git_branch"):
            with self.subTest(tool=name):
                self.assertTrue(tools[name].annotations.read_only_hint)
                self.assertIsNotNone(tools[name].output_schema)
        limit = tools["git_log"].input_schema["properties"]["limit"]
        self.assertEqual((limit["minimum"], limit["maximum"], limit["default"]), (1, 50, 10))

    async def test_git_status_round_trip(self) -> None:
        result = await self.call("git_status")
        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual(data["branch"], "main")
        self.assertEqual(data["staged"], [{"path": "staged.py", "change": "added", "original_path": None}])
        self.assertEqual(data["unstaged"], [{"path": "app.py", "change": "modified", "original_path": None}])

    async def test_git_log_round_trip(self) -> None:
        result = await self.call("git_log", {"limit": 1})
        self.assertFalse(result.is_error)
        self.assertEqual([c["subject"] for c in result.structured_content["commits"]], ["Initial commit"])

    async def test_git_diff_round_trip(self) -> None:
        unstaged = await self.call("git_diff")
        staged = await self.call("git_diff", {"staged": True})
        scoped = await self.call("git_diff", {"path": "README.md"})
        self.assertEqual([f["path"] for f in unstaged.structured_content["files"]], ["app.py"])
        self.assertEqual([f["path"] for f in staged.structured_content["files"]], ["staged.py"])
        self.assertEqual(scoped.structured_content["files"], [])

    async def test_git_branch_round_trip(self) -> None:
        result = await self.call("git_branch")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["current_branch"], "main")

    async def test_invalid_arguments_are_tool_errors(self) -> None:
        cases = [
            ("git_log", {"limit": 0}),
            ("git_log", {"limit": 51}),
            ("git_log", {"limit": "many"}),
            ("git_diff", {"staged": "maybe"}),
            ("git_diff", {"path": "../"}),
            ("git_diff", {"path": "C:\\Windows"}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool, args=args):
                result = await self.call(tool, args)
                self.assertTrue(result.is_error)

    async def test_extra_arguments_cannot_change_the_repository(self) -> None:
        result = await self.call("git_log", {"cwd": str(self.base), "repo": "..", "git_dir": "/tmp"})
        self.assertFalse(result.is_error)
        self.assertEqual([c["subject"] for c in result.structured_content["commits"]], ["Initial commit"])

    async def test_non_git_workspace_is_a_tool_error(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        for tool in ("git_status", "git_log", "git_diff", "git_branch"):
            with self.subTest(tool=tool):
                result = await self.call(tool, workspace=Workspace(plain))
                self.assertTrue(result.is_error)
                self.assertIn("not a Git repository", result.content[0].text)
                self.assertNotIn(str(self.base), result.content[0].text)

    async def test_git_unavailable_is_a_tool_error(self) -> None:
        with mock.patch.object(git.shutil, "which", return_value=None):
            result = await self.call("git_status")
        self.assertTrue(result.is_error)
        self.assertIn("Git executable not found", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
