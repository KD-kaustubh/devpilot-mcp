"""Tests for the search_code tool logic."""

import unittest

from devpilot_mcp.tools import code_search
from devpilot_mcp.workspace import PathNotFoundError, PathOutsideWorkspaceError, WorkspaceError
from tests.helpers import WorkspaceTestCase

AUTH_PY = """\
import hashlib


def authenticate_user(username, password):
    return check(username, password)
"""

LOGIN_PY = """\
from auth import authenticate_user


def login(request):
    user = authenticate_user(request.username, request.password)
    return user
"""


class CodeSearchTestCase(WorkspaceTestCase):
    """Adds a small multi-directory codebase to the shared test workspace."""

    def setUp(self) -> None:
        super().setUp()
        root = self.workspace.root
        (root / "src" / "routes").mkdir()
        (root / "src" / "auth.py").write_text(AUTH_PY, encoding="utf-8")
        (root / "src" / "routes" / "login.py").write_text(LOGIN_PY, encoding="utf-8")
        (root / "docs.md").write_text("authenticate_user is documented here\n", encoding="utf-8")
        (root / "build").mkdir()
        (root / "build" / "auth_generated.py").write_text("authenticate_user = None\n", encoding="utf-8")

    def search(self, query: str, path: str = ".", **limits):
        return code_search.search_code(self.workspace, query, path, **limits)


class SearchCodeTests(CodeSearchTestCase):
    def test_basic_search_returns_file_line_and_text(self) -> None:
        result = self.search("def authenticate_user")
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.file, "src/auth.py")
        self.assertEqual(match.line, 4)
        self.assertEqual(match.text, "def authenticate_user(username, password):")
        self.assertFalse(result.truncated)

    def test_multiple_matching_files_with_relative_paths(self) -> None:
        result = self.search("authenticate_user")
        locations = [(m.file, m.line) for m in result.matches]
        self.assertEqual(locations, [("src/auth.py", 4), ("src/routes/login.py", 1), ("src/routes/login.py", 5)])
        self.assertEqual(result.matches[2].text, "user = authenticate_user(request.username, request.password)")

    def test_search_within_subdirectory(self) -> None:
        result = self.search("authenticate_user", "src/routes")
        self.assertEqual(result.path, "src/routes")
        self.assertEqual({m.file for m in result.matches}, {"src/routes/login.py"})
        # Paths stay relative to the workspace root, not to the search path.
        self.assertTrue(all(m.file.startswith("src/routes/") for m in result.matches))

    def test_search_single_file(self) -> None:
        result = self.search("import", "src/auth.py")
        self.assertEqual([(m.file, m.line) for m in result.matches], [("src/auth.py", 1)])
        self.assertEqual(result.files_searched, 1)

    def test_backslash_path_accepted(self) -> None:
        self.assertEqual(self.search("login", "src\\routes").path, "src/routes")

    def test_no_matches_returns_empty_result(self) -> None:
        result = self.search("definitely_not_in_the_code")
        self.assertEqual(result.matches, [])
        self.assertFalse(result.truncated)
        self.assertGreater(result.files_searched, 0)

    def test_smart_case(self) -> None:
        (self.workspace.root / "src" / "models.py").write_text("class User:\n    user_id = 1\n", encoding="utf-8")
        insensitive = self.search("user", "src/models.py")
        self.assertFalse(insensitive.case_sensitive)
        self.assertEqual([m.line for m in insensitive.matches], [1, 2])

        sensitive = self.search("User", "src/models.py")
        self.assertTrue(sensitive.case_sensitive)
        self.assertEqual([m.line for m in sensitive.matches], [1])

    def test_only_source_files_are_searched(self) -> None:
        files = {m.file for m in self.search("authenticate_user").matches}
        self.assertNotIn("docs.md", files)

    def test_skips_dependency_and_build_dirs(self) -> None:
        files = {m.file for m in self.search("authenticate_user").matches}
        self.assertNotIn("build/auth_generated.py", files)
        self.assertEqual(self.search("hello from a dependency").matches, [])  # node_modules/dep.js

    def test_explicit_path_into_skipped_dir_is_searched(self) -> None:
        result = self.search("hello", "node_modules")
        self.assertEqual([m.file for m in result.matches], ["node_modules/dep.js"])

    def test_skips_minified_files(self) -> None:
        (self.workspace.root / "src" / "bundle.min.js").write_text("function authenticate_user(){}", encoding="utf-8")
        files = {m.file for m in self.search("authenticate_user").matches}
        self.assertNotIn("src/bundle.min.js", files)


class SearchCodeErrorTests(CodeSearchTestCase):
    def test_empty_query_rejected(self) -> None:
        for query in ("", "   "):
            with self.subTest(query=query):
                with self.assertRaises(WorkspaceError):
                    self.search(query)

    def test_missing_path(self) -> None:
        with self.assertRaises(PathNotFoundError):
            self.search("x", "src/does-not-exist")

    def test_non_source_file_path_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.search("authenticate", "docs.md")

    def test_path_traversal_rejected(self) -> None:
        for attack in ("../", "../../secret", "src/../../secret.txt", str(self.secret), "C:\\Windows", "D:\\other-project"):
            with self.subTest(path=attack):
                with self.assertRaises(PathOutsideWorkspaceError):
                    self.search("SECRET", attack)


class SearchCodeLimitTests(CodeSearchTestCase):
    def test_binary_files_are_skipped(self) -> None:
        (self.workspace.root / "src" / "compiled.py").write_bytes(b"authenticate_user\x00\x01\x02")
        result = self.search("authenticate_user")
        self.assertNotIn("src/compiled.py", {m.file for m in result.matches})
        self.assertEqual(result.files_skipped, 1)

    def test_non_utf8_files_are_skipped(self) -> None:
        (self.workspace.root / "src" / "legacy.py").write_bytes("# caf\xe9 authenticate_user".encode("latin-1"))
        result = self.search("authenticate_user")
        self.assertNotIn("src/legacy.py", {m.file for m in result.matches})
        self.assertEqual(result.files_skipped, 1)

    def test_files_over_size_limit_are_skipped(self) -> None:
        big = "authenticate_user()\n" * 100
        (self.workspace.root / "src" / "big.py").write_text(big, encoding="utf-8")
        result = self.search("authenticate_user", max_file_bytes=len(big) - 1)
        self.assertNotIn("src/big.py", {m.file for m in result.matches})
        self.assertEqual(result.files_skipped, 1)
        self.assertEqual(len(result.matches), 3)  # the normal-sized files are still searched

    def test_results_are_truncated_at_limit(self) -> None:
        many = "\n".join(f"needle_{i} = {i}" for i in range(20))
        (self.workspace.root / "src" / "many.py").write_text(many, encoding="utf-8")
        result = self.search("needle_", max_matches=5)
        self.assertEqual(len(result.matches), 5)
        self.assertTrue(result.truncated)
        self.assertEqual([m.line for m in result.matches], [1, 2, 3, 4, 5])

    def test_exactly_limit_matches_is_not_truncated(self) -> None:
        result = self.search("authenticate_user", max_matches=3)
        self.assertEqual(len(result.matches), 3)
        self.assertFalse(result.truncated)


if __name__ == "__main__":
    unittest.main()
