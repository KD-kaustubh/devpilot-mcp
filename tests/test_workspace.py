"""Tests for workspace path sandboxing."""

import os
import unittest

from devpilot_mcp.workspace import PathOutsideWorkspaceError
from tests.helpers import WorkspaceTestCase


class ResolveTests(WorkspaceTestCase):
    def test_root_aliases(self) -> None:
        for alias in ("", ".", "./", "  "):
            with self.subTest(alias=alias):
                self.assertEqual(self.workspace.resolve(alias), self.workspace.root)

    def test_relative_paths_resolve_inside_root(self) -> None:
        expected = self.workspace.root / "src" / "app.py"
        self.assertEqual(self.workspace.resolve("src/app.py"), expected)
        self.assertEqual(self.workspace.resolve("src\\app.py"), expected)
        self.assertEqual(self.workspace.resolve("src/../src/app.py"), expected)

    def test_rejects_escapes_and_absolute_paths(self) -> None:
        attacks = [
            "..",
            "../secret.txt",
            "../../secret.txt",
            "src/../../secret.txt",
            "..\\secret.txt",
            str(self.secret),
            "/etc/passwd",
            "C:\\Users\\someone\\secret.txt",
            "C:/Windows/win.ini",
            "D:\\other-project\\file.txt",
            "c:relative-to-drive.txt",
            "\\\\server\\share\\file.txt",
            "src/app.py\x00.txt",
        ]
        for attack in attacks:
            with self.subTest(path=attack):
                with self.assertRaises(PathOutsideWorkspaceError):
                    self.workspace.resolve(attack)

    def test_rejects_symlink_escaping_workspace(self) -> None:
        link = self.workspace.root / "escape"
        try:
            os.symlink(self.secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not permitted on this system.")
        with self.assertRaises(PathOutsideWorkspaceError):
            self.workspace.resolve("escape")


if __name__ == "__main__":
    unittest.main()
