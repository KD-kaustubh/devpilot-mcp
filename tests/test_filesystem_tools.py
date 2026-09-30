"""Tests for the filesystem tool logic."""

import unittest

from devpilot_mcp.sensitive import is_sensitive_path
from devpilot_mcp.text_search import SensitivePathError, is_link, iter_files
from devpilot_mcp.tools import code_search, filesystem, repository
from devpilot_mcp.tools.filesystem import NotATextFileError
from devpilot_mcp.workspace import PathNotFoundError, PathOutsideWorkspaceError, WorkspaceError
from tests.helpers import WorkspaceTestCase


class ListDirectoryTests(WorkspaceTestCase):
    def test_lists_root_with_directories_first(self) -> None:
        listing = filesystem.list_directory(self.workspace, ".")
        self.assertEqual(listing.path, ".")
        self.assertEqual([e.name for e in listing.entries], ["node_modules", "src", "image.bin", "README.md"])
        self.assertFalse(listing.truncated)

        readme = next(e for e in listing.entries if e.name == "README.md")
        self.assertEqual(readme.type, "file")
        self.assertEqual(readme.path, "README.md")
        self.assertGreater(readme.size_bytes, 0)
        src = next(e for e in listing.entries if e.name == "src")
        self.assertEqual(src.type, "directory")
        self.assertIsNone(src.size_bytes)

    def test_empty_path_is_root(self) -> None:
        self.assertEqual(filesystem.list_directory(self.workspace, "").path, ".")

    def test_lists_subdirectory_with_relative_paths(self) -> None:
        listing = filesystem.list_directory(self.workspace, "src")
        self.assertEqual([e.path for e in listing.entries], ["src/app.py", "src/util.py"])

    def test_missing_directory(self) -> None:
        with self.assertRaises(PathNotFoundError):
            filesystem.list_directory(self.workspace, "does-not-exist")

    def test_path_is_a_file(self) -> None:
        with self.assertRaises(WorkspaceError):
            filesystem.list_directory(self.workspace, "README.md")

    def test_escape_rejected(self) -> None:
        with self.assertRaises(PathOutsideWorkspaceError):
            filesystem.list_directory(self.workspace, "..")


class ReadFileTests(WorkspaceTestCase):
    def test_reads_text_file(self) -> None:
        result = filesystem.read_file(self.workspace, "src/app.py")
        self.assertEqual(result.path, "src/app.py")
        self.assertIn("Hello World", result.content)
        self.assertEqual(result.line_count, 2)

    def test_missing_file(self) -> None:
        with self.assertRaises(PathNotFoundError):
            filesystem.read_file(self.workspace, "src/missing.py")

    def test_directory_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            filesystem.read_file(self.workspace, "src")

    def test_empty_path_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            filesystem.read_file(self.workspace, "")

    def test_binary_file_rejected(self) -> None:
        with self.assertRaises(NotATextFileError):
            filesystem.read_file(self.workspace, "image.bin")

    def test_non_utf8_file_rejected(self) -> None:
        (self.workspace.root / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
        with self.assertRaises(NotATextFileError):
            filesystem.read_file(self.workspace, "latin1.txt")

    def test_oversized_file_rejected(self) -> None:
        (self.workspace.root / "big.txt").write_text("x" * (filesystem.MAX_READ_BYTES + 1), encoding="utf-8")
        with self.assertRaises(NotATextFileError):
            filesystem.read_file(self.workspace, "big.txt")

    def test_escape_rejected(self) -> None:
        for attack in ("../secret.txt", str(self.secret)):
            with self.subTest(path=attack):
                with self.assertRaises(PathOutsideWorkspaceError):
                    filesystem.read_file(self.workspace, attack)


class SearchFilesTests(WorkspaceTestCase):
    def test_finds_matches_case_insensitively(self) -> None:
        result = filesystem.search_files(self.workspace, "hello")
        self.assertEqual(result.files_with_matches, ["README.md", "src/app.py"])
        app_match = next(m for m in result.matches if m.path == "src/app.py")
        self.assertEqual(app_match.line_number, 2)
        self.assertEqual(app_match.line, "print('Hello World')")
        self.assertFalse(result.truncated)

    def test_skips_binary_files_and_dependency_dirs(self) -> None:
        paths = filesystem.search_files(self.workspace, "hello").files_with_matches
        self.assertNotIn("image.bin", paths)
        self.assertNotIn("node_modules/dep.js", paths)

    def test_no_matches(self) -> None:
        result = filesystem.search_files(self.workspace, "definitely-not-present")
        self.assertEqual(result.matches, [])
        self.assertEqual(result.files_with_matches, [])
        self.assertGreater(result.files_searched, 0)

    def test_never_searches_outside_workspace(self) -> None:
        self.assertEqual(filesystem.search_files(self.workspace, "TOP SECRET").matches, [])

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            filesystem.search_files(self.workspace, "   ")

    def test_results_are_truncated(self) -> None:
        lines = "\n".join("needle" for _ in range(filesystem.MAX_SEARCH_MATCHES + 10))
        (self.workspace.root / "many.txt").write_text(lines, encoding="utf-8")
        result = filesystem.search_files(self.workspace, "needle")
        self.assertEqual(len(result.matches), filesystem.MAX_SEARCH_MATCHES)
        self.assertTrue(result.truncated)


class SearchFilesPathTests(WorkspaceTestCase):
    def test_omitted_path_searches_the_whole_workspace(self) -> None:
        result = filesystem.search_files(self.workspace, "hello")
        self.assertEqual(result.files_with_matches, ["README.md", "src/app.py"])

    def test_dot_path_is_equivalent_to_omitting_it(self) -> None:
        for root_alias in (".", ""):
            with self.subTest(path=root_alias):
                self.assertEqual(
                    filesystem.search_files(self.workspace, "hello", root_alias).model_dump(),
                    filesystem.search_files(self.workspace, "hello").model_dump(),
                )

    def test_subdirectory_limits_the_scope(self) -> None:
        result = filesystem.search_files(self.workspace, "hello", "src")
        self.assertEqual(result.files_with_matches, ["src/app.py"])  # README.md is outside the scope
        self.assertEqual(result.files_searched, 2)
        self.assertEqual(filesystem.search_files(self.workspace, "hello", "src\\").files_with_matches, ["src/app.py"])

    def test_single_file_path(self) -> None:
        result = filesystem.search_files(self.workspace, "TODO", "src/util.py")
        self.assertEqual([(m.path, m.line_number) for m in result.matches], [("src/util.py", 1)])
        self.assertEqual(result.files_searched, 1)

    def test_traversal_is_rejected(self) -> None:
        for attack in ("..", "../", "../secret.txt", "src/../../secret.txt"):
            with self.subTest(path=attack):
                with self.assertRaises(PathOutsideWorkspaceError):
                    filesystem.search_files(self.workspace, "TOP SECRET", attack)

    def test_absolute_drive_and_unc_paths_are_rejected(self) -> None:
        for attack in (str(self.secret), "/etc/passwd", "C:\\Windows", "D:/other-project", "\\\\server\\share\\x"):
            with self.subTest(path=attack):
                with self.assertRaises(PathOutsideWorkspaceError):
                    filesystem.search_files(self.workspace, "TOP SECRET", attack)

    def test_nonexistent_path(self) -> None:
        with self.assertRaisesRegex(PathNotFoundError, "Search path not found"):
            filesystem.search_files(self.workspace, "hello", "does/not/exist")

    def test_explicit_dependency_directory_can_be_searched(self) -> None:
        # Like search_code, the skip list only prunes sub-directories below the search path.
        result = filesystem.search_files(self.workspace, "hello", "node_modules")
        self.assertEqual(result.files_with_matches, ["node_modules/dep.js"])


SECRET_VALUE = "sk-live-DEVPILOT-TEST-VALUE"


class SensitivePathTests(WorkspaceTestCase):
    """Secret files and .git internals are never returned by the read and search tools."""

    def setUp(self) -> None:
        super().setUp()
        root = self.workspace.root
        (root / "config").mkdir()
        (root / ".git").mkdir()
        files = {
            ".env": f"API_KEY={SECRET_VALUE}\n",
            "config/.env.production": f"API_KEY={SECRET_VALUE}\n",
            "id_rsa": f"-----BEGIN OPENSSH PRIVATE KEY-----\n{SECRET_VALUE}\n",
            "server.KEY": f"{SECRET_VALUE}\n",
            "credentials.json": f'{{"token": "{SECRET_VALUE}"}}\n',
            ".git/config": f'[remote "origin"]\n\turl = https://user:{SECRET_VALUE}@github.com/o/r.git\n',
            ".env.example": "API_KEY=\n",
        }
        for rel, text in files.items():
            (root / rel).write_bytes(text.encode("utf-8"))

    def test_read_file_refuses_secret_files_and_git_internals(self) -> None:
        for path in (".env", "config/.env.production", "id_rsa", "server.KEY", "credentials.json",
                     ".git/config", "./.git/config", "config/../.env"):  # fmt: skip
            with self.subTest(path=path):
                with self.assertRaisesRegex(SensitivePathError, "never returns its contents") as ctx:
                    filesystem.read_file(self.workspace, path)
                self.assertNotIn(SECRET_VALUE, str(ctx.exception))

    def test_templates_stay_readable(self) -> None:
        self.assertEqual(filesystem.read_file(self.workspace, ".env.example").content, "API_KEY=\n")

    def test_link_to_a_secret_file_is_refused(self) -> None:
        link = self.workspace.root / "notes.txt"
        try:
            link.symlink_to(self.workspace.root / ".env")
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this system.")
        with self.assertRaises(SensitivePathError):
            filesystem.read_file(self.workspace, "notes.txt")

    def test_search_files_never_returns_secret_content(self) -> None:
        result = filesystem.search_files(self.workspace, SECRET_VALUE)
        self.assertEqual(result.matches, [])
        result = filesystem.search_files(self.workspace, "API_KEY")
        self.assertEqual(result.files_with_matches, [".env.example"])

    def test_search_code_never_returns_secret_content(self) -> None:
        result = code_search.search_code(self.workspace, SECRET_VALUE.lower())
        self.assertEqual(result.matches, [])
        self.assertGreaterEqual(result.files_skipped, 1)  # credentials.json is a .json "source" file

    def test_search_paths_inside_git_or_secret_files_are_refused(self) -> None:
        for path in (".git", ".git/config", ".env", "credentials.json"):
            with self.subTest(path=path):
                with self.assertRaises(SensitivePathError):
                    filesystem.search_files(self.workspace, "url", path)
                with self.assertRaises(SensitivePathError):
                    code_search.search_code(self.workspace, "url", path)

    def test_listing_shows_names_but_never_contents(self) -> None:
        names = [e.name for e in filesystem.list_directory(self.workspace, ".").entries]
        self.assertIn(".env", names)

    def test_sensitive_path_rules(self) -> None:
        for rel in (".env", ".ENV", "a/b/.env.local", "id_ed25519", "x.pem", ".git", ".git/HEAD", "sub/.GIT/config"):
            self.assertTrue(is_sensitive_path(rel), rel)
        for rel in (".", "", ".env.example", ".env.sample", "src/app.py", "docs/keys.md", ".github/workflows/a.yml"):
            self.assertFalse(is_sensitive_path(rel), rel)


class LinkedDirectoryWalkTests(WorkspaceTestCase):
    """The shared directory walk never enters a symlinked or junction directory."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = self.secret.parent / "outside"
        self.outside.mkdir()
        (self.outside / "leak.py").write_text("OUTSIDE_MARKER = 'TOP SECRET'\n", encoding="utf-8")

    def _assert_not_followed(self, link_name: str) -> None:
        link = self.workspace.root / link_name
        self.assertTrue(is_link(link))
        walked = [p.name for p in iter_files(self.workspace.root)]
        self.assertNotIn("leak.py", walked)
        self.assertEqual(filesystem.search_files(self.workspace, "TOP SECRET").matches, [])
        self.assertEqual(code_search.search_code(self.workspace, "OUTSIDE_MARKER").matches, [])
        self.assertEqual(repository.analyze_repository(self.workspace).total_files, 4)  # leak.py not counted
        with self.assertRaises(PathOutsideWorkspaceError):
            filesystem.read_file(self.workspace, f"{link_name}/leak.py")

    def test_windows_junction_is_not_followed(self) -> None:
        try:
            import _winapi  # Windows only; junctions need no special privileges
        except ImportError:
            self.skipTest("Directory junctions are Windows-only.")
        _winapi.CreateJunction(str(self.outside), str(self.workspace.root / "jlink"))
        self._assert_not_followed("jlink")

    def test_directory_symlink_is_not_followed(self) -> None:
        try:
            (self.workspace.root / "slink").symlink_to(self.outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this system.")
        self._assert_not_followed("slink")

    def test_regular_directories_are_not_links(self) -> None:
        self.assertFalse(is_link(self.workspace.root / "src"))
        self.assertFalse(is_link(self.workspace.root / "missing"))


if __name__ == "__main__":
    unittest.main()
