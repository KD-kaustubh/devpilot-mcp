"""Tests for the filesystem tool logic."""

import unittest

from devpilot_mcp.tools import filesystem
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


if __name__ == "__main__":
    unittest.main()
