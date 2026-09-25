"""Shared fixtures: a throwaway workspace with a secret file just outside it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devpilot_mcp.workspace import Workspace


class WorkspaceTestCase(unittest.TestCase):
    """Creates `<tmp>/workspace/` (the sandbox) and `<tmp>/secret.txt` (must stay unreachable)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.secret = base / "secret.txt"
        self.secret.write_text("TOP SECRET", encoding="utf-8")

        root = base / "workspace"
        (root / "src").mkdir(parents=True)
        (root / "src" / "app.py").write_text("def main():\n    print('Hello World')\n", encoding="utf-8")
        (root / "src" / "util.py").write_text("# TODO: refactor\nVALUE = 42\n", encoding="utf-8")
        (root / "README.md").write_text("# Demo\nhello from the readme\n", encoding="utf-8")
        (root / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00hello")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.js").write_text("hello from a dependency\n", encoding="utf-8")

        self.workspace = Workspace(root)

    def tearDown(self) -> None:
        self._tmp.cleanup()
