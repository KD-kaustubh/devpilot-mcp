"""Tests for investigate_repository. No test contacts the real GitHub API."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from devpilot_mcp.github.client import GitHubClient, GitHubConnectionError
from devpilot_mcp.server import create_server
from devpilot_mcp.tools import git
from devpilot_mcp.tools import investigation as inv
from devpilot_mcp.tools.git import GitCommandError, GitTimeoutError
from devpilot_mcp.tools.investigation import extract_search_terms, investigate_repository
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError
from tests.test_git import GitRepoTestCase
from tests.test_github import API, TOKEN, FakeTransport, NoTokenMixin, issue_json, pull_json, repo_json, respond

LOGIN_PY = """\
\"\"\"User login.\"\"\"
import hashlib


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def authenticate_user(username, password):
    \"\"\"Authentication entry point used by the login view.\"\"\"
    user = find_user(username)
    return user is not None and user.password_hash == hash_password(password)


def find_user(username):
    return None
"""

SHOP_FILES: dict[str, str | bytes] = {
    "README.md": "# Shop\n\nA demo shop with authentication and payments.\n",
    "docs/auth.md": "# Authentication\n\nUsers log in with a password.\n",
    "src/auth/login.py": LOGIN_PY,
    "src/auth/tokens.py": "def issue_token(user):\n    return 'token-for-' + user\n",
    "src/payments/billing.py": "def charge(payment):\n    return payment.amount\n",
    "src/importer/csv_upload.py": "import csv\n\n\ndef read_upload(stream):\n    return list(csv.reader(stream))\n",
    "src/views.py": "from auth.login import authenticate_user\n\n\ndef login_view(request):\n    return authenticate_user(request.user, request.password)\n",
    "tests/test_auth.py": "from auth.login import authenticate_user\n\n\ndef test_authenticate_user():\n    assert not authenticate_user('a', 'b')\n",
    "node_modules/auth-lib/index.js": "export function authenticate() { /* authentication */ }\n",
    "build/lib/auth_generated.py": "authentication = 'generated'\n",
    ".venv/lib/auth.py": "authentication = 'venv'\n",
    "assets/logo.png": b"\x89PNG\x00\x00authentication",
    "src/compiled_auth.py": b"authentication\x00\x01\x02",
    ".env": "AUTH_SECRET=super-secret-value\n",
}


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)


def offline_client() -> tuple[GitHubClient, FakeTransport]:
    transport = FakeTransport()
    return GitHubClient(transport=transport), transport


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()
    }


class PlainWorkspaceTestCase(NoTokenMixin, unittest.TestCase):
    """A non-Git workspace with the shop files, plus a secret file just outside it."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.secret = self.base / "secret.txt"
        self.secret.write_text("TOP SECRET authentication", encoding="utf-8")
        self.root = self.base / "shop"
        self.root.mkdir()
        write_files(self.root, SHOP_FILES)
        self.workspace = Workspace(self.root)
        self.client, self.transport = offline_client()

    def investigate(self, query: str):
        return investigate_repository(self.workspace, self.client, query)


# --- Query handling ------------------------------------------------------------


class SearchTermTests(unittest.TestCase):
    def terms(self, query: str) -> list[tuple[str, str]]:
        return [(t.term, t.source) for t in extract_search_terms(query)[0]]

    def test_authentication_example(self) -> None:
        self.assertEqual(
            self.terms("How is authentication implemented?"),
            [("authentication", "query"), ("auth", "synonym"), ("login", "synonym")],
        )

    def test_csv_upload_example(self) -> None:
        self.assertEqual(self.terms("What handles CSV uploads?"), [("csv", "query"), ("upload", "query")])

    def test_stemming(self) -> None:
        self.assertEqual(self.terms("uploads")[0][0], "upload")
        self.assertEqual(self.terms("batches")[0][0], "batch")
        self.assertEqual(self.terms("processing")[0][0], "process")
        self.assertEqual(self.terms("processes")[0][0], "process")
        self.assertEqual(self.terms("queries")[0][0], "query")
        self.assertEqual(self.terms("structured")[0][0], "structur")
        self.assertEqual(self.terms("registered")[0][0], "register")
        self.assertEqual(self.terms("string")[0][0], "string")  # too short to strip "ing"

    def test_stopwords_short_words_and_numbers_are_dropped(self) -> None:
        self.assertEqual(self.terms("How is it done in the app? v2 42 of a db"), [])
        self.assertEqual(self.terms("Where does search_code work"), [("search_code", "query")])

    def test_duplicates_and_order(self) -> None:
        self.assertEqual(
            self.terms("login login Authentication"),
            [("login", "query"), ("authentication", "query"), ("auth", "synonym")],
        )

    def test_term_cap(self) -> None:
        terms, capped = extract_search_terms("alpha bravo charlie delta echoes foxtrot golf hotel india juliet")
        self.assertEqual(len(terms), inv.MAX_SEARCH_TERMS)
        self.assertTrue(capped)

    def test_query_validation(self) -> None:
        for bad in ("", "   ", "\n\t ", "x" * (inv.MAX_QUERY_CHARS + 1), "auth\x00", None, 42):
            with self.subTest(query=repr(bad)[:20]):
                with self.assertRaises(WorkspaceError):
                    inv.validate_query(bad)
        self.assertEqual(inv.validate_query("  How?  "), "How?")


class QueryRejectionTests(PlainWorkspaceTestCase):
    def test_empty_and_whitespace_queries_are_rejected_before_any_work(self) -> None:
        with mock.patch.object(inv, "_repository_context") as repo_context:
            for query in ("", " ", "\t\n"):
                with self.subTest(query=repr(query)):
                    with self.assertRaisesRegex(WorkspaceError, "must not be empty"):
                        self.investigate(query)
        repo_context.assert_not_called()

    def test_query_without_search_terms_still_returns_context(self) -> None:
        result = self.investigate("How does this work?")
        self.assertEqual((result.search_terms, result.relevant_files, result.code_matches), ([], [], []))
        self.assertGreater(result.repository.total_files, 0)
        self.assertIn("No search terms remained", result.warnings[0])


# --- Local evidence ----------------------------------------------------------


class LocalEvidenceTests(PlainWorkspaceTestCase):
    def test_basic_investigation(self) -> None:
        result = self.investigate("How is authentication implemented?")
        self.assertEqual(result.query, "How is authentication implemented?")
        self.assertIn("did not interpret the question or answer it", result.evidence_note)
        self.assertEqual([t.term for t in result.search_terms], ["authentication", "auth", "login"])
        self.assertEqual(result.repository.name, "shop")
        self.assertEqual(result.relevant_files[0].path, "src/auth/login.py")
        self.assertEqual(result.limits, inv.LIMITS)
        # Evidence, not conclusions: there is no answer/summary field.
        self.assertFalse({"answer", "summary", "explanation", "plan"} & set(result.model_dump()))

    def test_relevant_file_discovery_and_reasons(self) -> None:
        result = self.investigate("How is authentication implemented?")
        files = {f.path: f for f in result.relevant_files}
        self.assertEqual(
            set(files), {"src/auth/login.py", "src/auth/tokens.py", "src/views.py", "tests/test_auth.py",
                         "docs/auth.md", "README.md"},
        )  # fmt: skip
        self.assertEqual(files["docs/auth.md"].kind, "documentation")
        self.assertEqual(files["tests/test_auth.py"].kind, "test")
        self.assertIn("path contains 'auth'", files["src/auth/tokens.py"].reasons)
        self.assertIn("file name contains 'login'", files["src/auth/login.py"].reasons)
        self.assertEqual(files["src/auth/login.py"].matched_terms, ["authentication", "auth", "login"])
        self.assertNotIn("src/payments/billing.py", files)
        dirs = [d.path for d in result.relevant_directories]
        self.assertEqual(dirs[0], "src/auth")

    def test_ranking_is_deterministic_and_prefers_implementation(self) -> None:
        first = self.investigate("How is authentication implemented?")
        second = self.investigate("How is authentication implemented?")
        self.assertEqual(first.model_dump(), second.model_dump())
        scores = [(-f.score, f.path) for f in first.relevant_files]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(first.relevant_files[0].path, "src/auth/login.py")

    def test_implementation_ranks_before_tests_and_docs(self) -> None:
        same = "gizmo = 1\ngizmo += 1\n"
        write_files(self.root, {"src/gizmo.py": same, "tests/test_gizmo.py": same, "docs/gizmo.md": same})
        result = self.investigate("gizmo")
        self.assertEqual(
            [(f.path, f.kind) for f in result.relevant_files],
            [("src/gizmo.py", "source"), ("tests/test_gizmo.py", "test"), ("docs/gizmo.md", "documentation")],
        )

    def test_code_matches_follow_file_ranking(self) -> None:
        result = self.investigate("How is authentication implemented?")
        rank = {f.path: i for i, f in enumerate(result.relevant_files)}
        keys = [(rank[m.file], m.line) for m in result.code_matches]
        self.assertEqual(keys, sorted(keys))
        login = [m for m in result.code_matches if m.file == "src/auth/login.py"]
        self.assertIn((9, "def authenticate_user(username, password):", ["auth"]), [(m.line, m.text, m.terms) for m in login])
        self.assertTrue(all(m.terms for m in result.code_matches))

    def test_multiple_terms_raise_relevance(self) -> None:
        write_files(self.root, {"src/one.py": "alpha = 1\n", "src/two.py": "alpha = 1\nbravo = 2\n"})
        result = self.investigate("alpha bravo")
        self.assertEqual([f.path for f in result.relevant_files], ["src/two.py", "src/one.py"])
        self.assertEqual(result.relevant_files[0].matched_terms, ["alpha", "bravo"])

    def test_payment_and_csv_queries(self) -> None:
        self.assertEqual(self.investigate("payment processing").relevant_files[0].path, "src/payments/billing.py")
        csv = self.investigate("What handles CSV uploads?")
        self.assertEqual(csv.relevant_files[0].path, "src/importer/csv_upload.py")

    def test_file_context(self) -> None:
        result = self.investigate("authenticate_user")
        snippet = next(s for s in result.file_context if s.path == "src/auth/login.py")
        lines = LOGIN_PY.splitlines()
        self.assertEqual((snippet.start_line, snippet.end_line), (5, 13))  # line 9 +/- 4
        self.assertEqual(snippet.content, "\n".join(lines[4:13]))
        self.assertFalse(snippet.truncated)
        self.assertLessEqual(len({s.path for s in result.file_context}), inv.MAX_CONTEXT_FILES)

    def test_file_context_truncation(self) -> None:
        write_files(self.root, {"src/long.py": "x = 'authentication" + "y" * 1000 + "'\n"})
        result = self.investigate("authentication")
        long_snippet = next(s for s in result.file_context if s.path == "src/long.py")
        self.assertTrue(long_snippet.truncated)
        self.assertTrue(long_snippet.content.endswith(" …"))
        with mock.patch.object(inv, "MAX_CONTEXT_BYTES", 120):
            result = self.investigate("authentication")
        self.assertLessEqual(sum(len(s.content.encode()) + 1 for s in result.file_context), 120 + 50)
        self.assertIn("file_context", result.truncated_fields)

    def test_result_limits(self) -> None:
        write_files(self.root, {f"src/mod{i:02d}.py": "\n".join(f"widget_{j} = {j}" for j in range(12)) for i in range(25)})
        result = self.investigate("widget")
        self.assertEqual(len(result.relevant_files), inv.MAX_RELEVANT_FILES)
        self.assertEqual(len(result.code_matches), inv.MAX_CODE_MATCHES)
        self.assertLessEqual(max(sum(1 for m in result.code_matches if m.file == f) for f in {m.file for m in result.code_matches}),
                             inv.MAX_MATCHES_PER_FILE)  # fmt: skip
        self.assertLessEqual(len(result.relevant_directories), inv.MAX_RELEVANT_DIRECTORIES)
        self.assertEqual(result.truncated_fields, ["relevant_files", "code_matches"])
        self.assertLess(len(result.model_dump_json()), 60_000)

    def test_large_repository(self) -> None:
        write_files(self.root, {f"pkg{i // 50}/m{i:03d}.py": "gadget = 1\ngadget += 1\n" for i in range(300)})
        with mock.patch.object(inv, "MAX_SCAN_MATCHES", 50):
            result = self.investigate("gadget")
        self.assertIn("search_scan", result.truncated_fields)
        self.assertTrue(any("stopped after 50 matches" in w for w in result.warnings))
        with mock.patch.object(inv, "MAX_FILES_SCANNED", 100):
            result = self.investigate("gadget")
        self.assertTrue(any("Only the first 100 candidate files" in w for w in result.warnings))
        self.assertLessEqual(result.files_scanned, 100)

    def test_binary_ignored_and_secret_files_are_excluded(self) -> None:
        result = self.investigate("authentication secret")
        paths = {f.path for f in result.relevant_files} | {m.file for m in result.code_matches}
        for excluded in ("assets/logo.png", "src/compiled_auth.py", "node_modules/auth-lib/index.js",
                         "build/lib/auth_generated.py", ".venv/lib/auth.py", ".env"):  # fmt: skip
            self.assertNotIn(excluded, paths)
        self.assertTrue(any("binary, non-UTF-8, oversized or unreadable" in w for w in result.warnings))
        self.assertNotIn("super-secret-value", result.model_dump_json())

    def test_secret_values_are_redacted(self) -> None:
        fake_pat = "ghp_" + "A1b2C3d4" * 5
        write_files(self.root, {"src/settings.py": f"GITHUB_TOKEN_FOR_AUTH = '{fake_pat}'\n"})
        result = self.investigate("auth settings")
        dumped = result.model_dump_json()
        self.assertNotIn(fake_pat, dumped)
        self.assertIn("[REDACTED]", dumped)
        self.assertTrue(any("redacted" in w for w in result.warnings))


class WorkspaceSecurityTests(PlainWorkspaceTestCase):
    def test_query_cannot_reach_outside_the_workspace(self) -> None:
        with mock.patch.object(inv.filesystem, "read_file", wraps=inv.filesystem.read_file) as read_file:
            result = self.investigate(f"../../secret.txt {self.secret} C:\\Windows\\win.ini /etc/passwd authentication")
        dumped = result.model_dump_json()
        self.assertNotIn("TOP SECRET", dumped)
        self.assertNotIn(str(self.base), dumped)
        for call in read_file.call_args_list:
            self.assertFalse(Path(call.args[1]).is_absolute() or call.args[1].startswith(".."), call.args[1])
        for path in [f.path for f in result.relevant_files] + [s.path for s in result.file_context]:
            self.assertTrue((self.root / path).resolve().is_relative_to(self.root.resolve()))

    def test_missing_workspace(self) -> None:
        shutil.rmtree(self.root)
        with self.assertRaises(PathNotFoundError):
            self.investigate("authentication")

    def test_non_git_workspace_still_investigates(self) -> None:
        result = self.investigate("authentication")
        self.assertFalse(result.git_context.available)
        self.assertIn("not a Git repository", result.git_context.reason)
        self.assertFalse(result.github_context.available)
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(result.relevant_files)
        self.assertEqual(result.warnings, [w for w in result.warnings if "Git" not in w])  # not treated as a failure

    def test_no_files_are_modified(self) -> None:
        before = snapshot(self.base)
        self.investigate("How is authentication implemented?")
        self.assertEqual(snapshot(self.base), before)


# --- Git and GitHub context -----------------------------------------------------


class GitEvidenceTestCase(NoTokenMixin, GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        for rel, content in SHOP_FILES.items():
            if not rel.startswith((".env", "node_modules", "build", ".venv")):
                self.write(rel, content)
        self.commit("Initial shop")
        self.write("src/payments/billing.py", "def charge(payment):\n    return payment.amount * 2\n")
        self.commit("Double payment charges")
        self.write("src/auth/login.py", LOGIN_PY + "\n# authentication audit\n")
        self.commit("Add authentication audit note")
        self.client, self.transport = offline_client()

    def investigate(self, query: str = "How is authentication implemented?"):
        return investigate_repository(self.workspace, self.client, query)


class GitContextTests(GitEvidenceTestCase):
    def test_clean_repository(self) -> None:
        git_context = self.investigate().git_context
        self.assertTrue(git_context.available)
        self.assertEqual((git_context.branch, git_context.clean, git_context.detached), ("main", True, False))
        self.assertEqual(git_context.relevant_changed_files, [])
        self.assertEqual(git_context.relevant_diffs, [])
        self.assertEqual(
            [c.subject for c in git_context.recent_commits],
            ["Add authentication audit note", "Double payment charges", "Initial shop"],
        )
        self.assertEqual([(c.subject, c.matched_terms) for c in git_context.relevant_commits],
                         [("Add authentication audit note", ["authentication", "auth"])])  # fmt: skip
        self.assertEqual(git_context.commits_scanned, 3)

    def test_repository_with_changes(self) -> None:
        self.write("src/auth/login.py", LOGIN_PY.replace("return None", "return lookup(username)"))
        self.write("src/auth/tokens.py", "def issue_token(user):\n    return 'v2-' + user\n")
        self.run_git("add", "src/auth/tokens.py")
        self.write("src/auth/sessions.py", "SESSION = 'auth'\n")
        self.write("src/payments/refunds.py", "refund = 1\n")
        git_context = self.investigate().git_context
        self.assertFalse(git_context.clean)
        self.assertEqual(
            [(c.path, c.area, c.change) for c in git_context.relevant_changed_files],
            [("src/auth/login.py", "unstaged", "modified"), ("src/auth/sessions.py", "untracked", "added"),
             ("src/auth/tokens.py", "staged", "modified")],
        )  # fmt: skip
        self.assertEqual(git_context.changed_file_counts["untracked"], 2)  # counts cover every change
        diffs = {d.path: d for d in git_context.relevant_diffs}
        self.assertEqual(set(diffs), {"src/auth/login.py", "src/auth/tokens.py"})
        self.assertIn("+    return lookup(username)", diffs["src/auth/login.py"].diff)
        self.assertTrue(diffs["src/auth/tokens.py"].staged)

    def test_diff_excerpt_is_capped(self) -> None:
        self.write("src/auth/login.py", LOGIN_PY + "".join(f"# auth line {i}\n" for i in range(500)))
        with mock.patch.object(inv, "MAX_DIFF_CHARS", 300):
            diff = self.investigate().git_context.relevant_diffs[0]
        self.assertTrue(diff.truncated)
        self.assertLessEqual(len(diff.diff), 300)

    def test_git_failures_are_handled(self) -> None:
        with mock.patch.object(inv.git, "git_log", side_effect=GitCommandError("git log failed: fatal: boom")):
            result = self.investigate()
        self.assertTrue(result.git_context.available)
        self.assertEqual(result.git_context.recent_commits, [])
        self.assertIn("Git history unavailable: git log failed: fatal: boom", result.warnings)
        self.assertTrue(result.relevant_files)

        with mock.patch.object(inv.git, "git_status", side_effect=GitTimeoutError("Git command timed out after 15 seconds.")):
            result = self.investigate()
        self.assertFalse(result.git_context.available)
        self.assertIn("timed out", result.git_context.reason)
        self.assertIn("Git context unavailable: Git command timed out after 15 seconds.", result.warnings)
        self.assertTrue(result.relevant_files)

    def test_only_read_only_git_invocations(self) -> None:
        self.write("src/auth/login.py", LOGIN_PY + "# changed\n")
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")
        self.transport.responses.append(GitHubConnectionError("offline"))
        with mock.patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            self.investigate()
        allowed = {"status", "log", "diff", "diff-files", "rev-parse", "remote"}
        self.assertTrue(popen.call_args_list)
        for call in popen.call_args_list:
            argv, kwargs = call.args[0], call.kwargs
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(argv[0], shutil.which("git"))  # nothing but Git is ever executed
            subcommand = argv[argv.index("protocol.allow=never") + 1]
            self.assertIn(subcommand, allowed)
            if subcommand == "remote":
                self.assertEqual(argv[-2:], ["get-url", "origin"])

    def test_repository_and_git_state_are_not_modified(self) -> None:
        self.write("src/auth/login.py", LOGIN_PY + "# changed\n")
        self.run_git("add", "src/auth/login.py")
        self.write("src/auth/login.py", LOGIN_PY + "# changed again\n")
        self.write("notes.txt", "untracked\n")
        stamp = time.time() + 120
        os.utime(self.root / "src" / "auth" / "tokens.py", (stamp, stamp))  # stale index stat data
        head, branch = self.run_git("rev-parse", "HEAD"), self.run_git("branch", "--show-current")
        before = snapshot(self.root)
        self.investigate()
        self.investigate("payment charges")
        self.assertEqual(snapshot(self.root), before)  # includes every file under .git
        self.assertEqual((self.run_git("rev-parse", "HEAD"), self.run_git("branch", "--show-current")), (head, branch))


class GitHubContextTests(GitEvidenceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")

    def test_relevant_issues_and_pull_requests(self) -> None:
        self.transport.responses += [
            respond(repo_json()),
            respond([issue_json(3, title="Login fails for SSO users", labels=[{"name": "auth"}]),
                     issue_json(2, title="Improve payment receipts"), issue_json(1, pr=True, title="auth PR as issue")]),
            respond([pull_json(9, title="Refactor authentication middleware"), pull_json(8, title="Bump deps")]),
        ]  # fmt: skip
        github = self.investigate().github_context
        self.assertTrue(github.available)
        self.assertEqual(github.repository.full_name, "octo-org/hello-world")
        self.assertEqual([(i.number, i.matched_terms) for i in github.relevant_issues], [(3, ["auth", "login"])])
        self.assertEqual([(p.number, p.matched_terms) for p in github.relevant_pull_requests],
                         [(9, ["authentication", "auth"])])  # fmt: skip
        self.assertEqual((github.open_issues_scanned, github.open_pull_requests_scanned), (2, 2))
        self.assertEqual(
            self.transport.urls,
            [API, f"{API}/issues?state=open&per_page=31&page=1", f"{API}/pulls?state=open&per_page=30&page=1"],
        )

    def test_github_failure_does_not_break_local_investigation(self) -> None:
        self.transport.responses.append(GitHubConnectionError("Could not connect to GitHub (gaierror)."))
        result = self.investigate()
        self.assertFalse(result.github_context.available)
        self.assertIn("Could not connect to GitHub", result.github_context.reason)
        self.assertEqual(len(self.transport.calls), 1)  # no further requests after the first failure
        self.assertTrue(any(w.startswith("GitHub context is incomplete") for w in result.warnings))
        self.assertEqual(result.relevant_files[0].path, "src/auth/login.py")
        self.assertTrue(result.git_context.available)

    def test_partial_github_failure_keeps_repository_metadata(self) -> None:
        self.transport.responses += [respond(repo_json()), respond({"message": "Issues are disabled"}, 410)]
        github = self.investigate().github_context
        self.assertTrue(github.available)
        self.assertEqual(github.repository.default_branch, "main")
        self.assertEqual(github.relevant_issues, [])

    def test_no_github_origin_makes_no_requests(self) -> None:
        self.run_git("remote", "set-url", "origin", "https://gitlab.com/octo-org/hello-world.git")
        github = self.investigate().github_context
        self.assertFalse(github.available)
        self.assertIn("gitlab.com", github.reason)
        self.assertEqual(self.transport.calls, [])

    def test_token_never_appears_in_output_errors_or_logs(self) -> None:
        os.environ["GITHUB_TOKEN"] = TOKEN
        self.write("src/auth/config.py", f"AUTH_TOKEN = '{TOKEN}'\n")  # a leaked token inside the repository
        self.transport.responses += [
            respond(repo_json(description=f"auth {TOKEN}")),
            respond([issue_json(1, title=f"auth token {TOKEN} leaked")]),
            respond({"message": f"Bad credentials {TOKEN}"}, 401),
        ]
        records: list[str] = []
        handler = logging.Handler(level=logging.DEBUG)
        handler.emit = lambda record: records.append(handler.format(record))  # type: ignore[method-assign]
        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        streams = io.StringIO()
        try:
            with contextlib.redirect_stdout(streams), contextlib.redirect_stderr(streams):
                result = self.investigate("auth token")
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)
        self.assertTrue(all(h["Authorization"] == f"Bearer {TOKEN}" for _, h, _ in self.transport.calls))
        for text in [result.model_dump_json(), streams.getvalue(), *records]:
            self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED]", result.model_dump_json())
        self.assertTrue(any("401" in w for w in result.warnings))


# --- MCP -----------------------------------------------------------------------


class InvestigationMcpTests(GitEvidenceTestCase, unittest.IsolatedAsyncioTestCase):
    async def call(self, args: dict):
        async with Client(create_server(self.workspace, github_client=self.client)) as client:
            return await client.call_tool("investigate_repository", args)

    async def test_registration_and_schema(self) -> None:
        async with Client(create_server(self.workspace, github_client=self.client)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        self.assertEqual(len(tools), 15)
        tool = tools["investigate_repository"]
        schema = tool.input_schema
        self.assertEqual(set(schema["properties"]), {"query"})
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual((schema["properties"]["query"]["minLength"], schema["properties"]["query"]["maxLength"]), (1, 500))
        self.assertTrue(tool.annotations.read_only_hint)
        self.assertFalse(tool.annotations.destructive_hint)
        self.assertIn("evidence", tool.description.lower())
        self.assertIn("relevant_files", tool.output_schema["properties"])

    async def test_round_trip(self) -> None:
        result = await self.call({"query": "How is authentication implemented?"})
        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual(data["relevant_files"][0]["path"], "src/auth/login.py")
        self.assertTrue(data["git_context"]["available"])
        self.assertFalse(data["github_context"]["available"])  # no origin configured

    async def test_invalid_arguments(self) -> None:
        for args, expected in (({"query": ""}, None), ({"query": "   "}, "must not be empty"), ({"query": "x" * 501}, None),
                               ({}, None), ({"query": 42}, None)):  # fmt: skip
            with self.subTest(args=str(args)[:30]):
                result = await self.call(args)
                self.assertTrue(result.is_error)
                if expected:
                    self.assertIn(expected, result.content[0].text)

    async def test_extra_arguments_are_ignored(self) -> None:
        result = await self.call({"query": "authentication", "path": "../..", "url": "https://evil.example", "cwd": "/"})
        self.assertFalse(result.is_error)
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
