"""Tests for apply_patch / revert_patch: explicit patches, validated and applied atomically."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from devpilot_mcp.patching import changes
from devpilot_mcp.patching.changes import (
    ChangeRegistry,
    PatchLimitError,
    PatchPathError,
    PatchWriteError,
    ProtectedPathError,
    RevertConflictError,
    UnknownChangeError,
)
from devpilot_mcp.patching.unified_diff import PatchConflictError, PatchFormatError
from devpilot_mcp.server import create_server
from devpilot_mcp.tools import git
from devpilot_mcp.tools.patch import apply_patch, revert_patch
from devpilot_mcp.workspace import PathOutsideWorkspaceError, Workspace, WorkspaceError
from tests.test_git import GitRepoTestCase

APP = "def main():\n    print('hello')\n    return 0\n"
UTIL = "VALUE = 1\nNAME = 'x'\n"
SECRET_VALUE = "supersecret-value-123"
FILES: dict[str, str | bytes] = {
    "src/app.py": APP,
    "src/util.py": UTIL,
    "README.md": "# Demo\n",
    ".env": f"API_KEY={SECRET_VALUE}\n",
    ".env.example": "API_KEY=\n",
    ".git/config": "[core]\n",
    "keys/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
}


def udiff(rel: str, old: str, new: str, *, create: bool = False, delete: bool = False) -> str:
    """A standard unified diff, as produced by difflib (the same format as `diff -u`)."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(True),
            new.splitlines(True),
            "/dev/null" if create else f"a/{rel}",
            "/dev/null" if delete else f"b/{rel}",
        )
    )


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under root with its mtime and bytes (directories are implied by paths)."""
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def contents(root: Path) -> dict[str, bytes]:
    return {path: data for path, (_, data) in snapshot(root).items()}


class PatchTestCase(unittest.TestCase):
    """A workspace at <tmp>/repo with a secret file just outside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.secret = self.base / "secret.txt"
        self.secret.write_text("TOP SECRET\n", encoding="utf-8")
        self.root = self.base / "repo"
        write_files(self.root, FILES)
        self.workspace = Workspace(self.root)
        self.registry = ChangeRegistry()

    def apply(self, patch: str):
        return apply_patch(self.workspace, self.registry, patch)

    def revert(self, change_id: str):
        return revert_patch(self.workspace, self.registry, change_id)

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def assert_rejected(self, patch: str, error: type[Exception] = WorkspaceError, pattern: str = "") -> str:
        """The patch must fail and leave every file (inside and outside the workspace) untouched."""
        before = snapshot(self.base)
        with self.assertRaises(error) as ctx:
            self.apply(patch)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.registry.ids(), [])
        message = str(ctx.exception)
        if pattern:
            self.assertRegex(message, pattern)
        return message


# --- Applying ------------------------------------------------------------------


class ApplyTests(PatchTestCase):
    def test_single_file_modification(self) -> None:
        new = APP.replace("print('hello')", "print('hello, world')\n    log('done')")
        patch = udiff("src/app.py", APP, new)
        result = self.apply(patch)
        self.assertEqual(self.read("src/app.py").decode(), new)
        self.assertEqual((result.files_changed_count, result.additions, result.deletions), (1, 2, 1))
        changed = result.files_changed[0]
        self.assertEqual((changed.path, changed.change, changed.additions, changed.deletions), ("src/app.py", "modified", 2, 1))
        self.assertEqual(changed.sha256_before, hashlib.sha256(APP.encode()).hexdigest())
        self.assertEqual(changed.sha256_after, hashlib.sha256(new.encode()).hexdigest())
        self.assertEqual(result.patch_sha256, hashlib.sha256(patch.encode()).hexdigest())
        self.assertRegex(result.change_id, r"^chg_[0-9a-f]{16}$")
        self.assertRegex(result.applied_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(result.reversible)
        self.assertEqual(self.registry.ids(), [result.change_id])

    def test_multi_file_modification(self) -> None:
        patch = udiff("src/app.py", APP, APP.replace("return 0", "return 1")) + udiff(
            "src/util.py", UTIL, UTIL.replace("VALUE = 1", "VALUE = 2")
        )
        result = self.apply(patch)
        self.assertEqual([f.path for f in result.files_changed], ["src/app.py", "src/util.py"])
        self.assertEqual((result.additions, result.deletions), (2, 2))
        self.assertIn(b"return 1", self.read("src/app.py"))
        self.assertIn(b"VALUE = 2", self.read("src/util.py"))

    def test_file_creation_with_new_directories(self) -> None:
        result = self.apply(udiff("src/pkg/sub/new.py", "", "A = 1\nB = 2\n", create=True))
        self.assertEqual(self.read("src/pkg/sub/new.py"), b"A = 1\nB = 2\n")
        self.assertEqual((result.files_changed[0].change, result.files_changed[0].sha256_before), ("created", None))

    def test_file_deletion(self) -> None:
        result = self.apply(udiff("src/util.py", UTIL, "", delete=True))
        self.assertFalse((self.root / "src/util.py").exists())
        changed = result.files_changed[0]
        self.assertEqual((changed.change, changed.deletions, changed.sha256_after), ("deleted", 2, None))

    def test_multiple_hunks(self) -> None:
        original = "".join(f"line {i}\n" for i in range(1, 41))
        write_files(self.root, {"src/long.txt": original})
        new = original.replace("line 3\n", "line three\n").replace("line 35\n", "line 35\nline 35b\n")
        patch = udiff("src/long.txt", original, new)
        self.assertEqual(patch.count("@@ -"), 2)
        self.apply(patch)
        self.assertEqual(self.read("src/long.txt").decode(), new)

    def test_crlf_bom_and_missing_final_newline_are_preserved(self) -> None:
        write_files(self.root, {"win.txt": b"\xef\xbb\xbfone\r\ntwo\r\nthree"})
        patch = (
            "--- a/win.txt\n+++ b/win.txt\n@@ -1,3 +1,4 @@\n one\n-two\n+TWO\n+two and a half\n three\n"
            "\\ No newline at end of file\n"
        )
        self.apply(patch)
        self.assertEqual(self.read("win.txt"), b"\xef\xbb\xbfone\r\nTWO\r\ntwo and a half\r\nthree")

    def test_adding_a_final_newline(self) -> None:
        write_files(self.root, {"end.txt": b"a\nb"})
        self.apply("--- a/end.txt\n+++ b/end.txt\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b\n")
        self.assertEqual(self.read("end.txt"), b"a\nb\n")

    def test_git_style_headers_and_unprefixed_paths(self) -> None:
        patch = (
            "diff --git a/src/util.py b/src/util.py\nindex 1a2b3c4..5d6e7f8 100644\n"
            + udiff("src/util.py", UTIL, "VALUE = 3\nNAME = 'x'\n")
            + "diff --git a/src/brand_new.py b/src/brand_new.py\nnew file mode 100644\nindex 0000000..e69de29\n"
            + udiff("src/brand_new.py", "", "X = 1\n", create=True)
        )
        self.apply(patch)
        self.apply("--- README.md\n+++ README.md\n@@ -1 +1 @@\n-# Demo\n+# Demo project\n")
        self.assertEqual((self.read("src/util.py")[:9], self.read("src/brand_new.py")), (b"VALUE = 3", b"X = 1\n"))
        self.assertEqual(self.read("README.md"), b"# Demo project\n")

    def test_env_example_is_allowed(self) -> None:
        self.apply(udiff(".env.example", "API_KEY=\n", "API_KEY=\nDEBUG=false\n"))
        self.assertEqual(self.read(".env.example"), b"API_KEY=\nDEBUG=false\n")


class FormatValidationTests(PatchTestCase):
    def test_empty_and_non_string_patches(self) -> None:
        for patch in ("", "   \n\n"):
            self.assert_rejected(patch, PatchFormatError, "empty")
        with self.assertRaises(PatchFormatError):
            self.apply(None)  # type: ignore[arg-type]

    def test_malformed_patches(self) -> None:
        header = "--- a/src/util.py\n+++ b/src/util.py\n"
        cases = {
            "plain text": "please change VALUE to 2\n",
            "missing +++": "--- a/src/util.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
            "bad hunk header": header + "@@ -a +b @@\n-VALUE = 1\n+VALUE = 2\n",
            "no hunks": header,
            "hunk too short": header + "@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 2\n",
            "hunk too long": header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n+EXTRA = 3\n",
            "bad hunk line": header + "@@ -1 +1 @@\n*VALUE = 1\n+VALUE = 2\n",
            "marker first": header + "@@ -1 +1 @@\n\\ No newline at end of file\n-VALUE = 1\n+VALUE = 2\n",
            "preamble": "From: someone\nSubject: change\n" + header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
            "trailing junk": header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\nrm -rf /\n",
            "quoted path": '--- "a/src/util.py"\n+++ "b/src/util.py"\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n',
            "both dev null": "--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+x\n",
            "rename by paths": "--- a/src/util.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
            "mixed prefixes": "--- a/src/util.py\n+++ src/util.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
            "git header mismatch": "diff --git a/src/app.py b/src/app.py\n" + header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
            "metadata only": "diff --git a/empty b/empty\nnew file mode 100644\nindex 0000000..e69de29\n",
            "overlapping hunks": header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 3\n",
            "duplicate file": (header + "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n") * 2,
        }
        for name, patch in cases.items():
            with self.subTest(case=name):
                self.assert_rejected(patch, PatchFormatError)

    def test_unsupported_git_features(self) -> None:
        for header, pattern in (
            ("GIT binary patch\nliteral 3\n", "Binary"),
            ("Binary files a/logo.png and b/logo.png differ\n", "Binary"),
            ("old mode 100644\nnew mode 100755\n", "mode"),
            ("similarity index 90%\nrename from src/util.py\nrename to src/u.py\n", "Rename"),
            ("copy from src/util.py\ncopy to src/u.py\n", "Cop"),
        ):
            with self.subTest(header=header.split()[0]):
                self.assert_rejected("diff --git a/src/util.py b/src/util.py\n" + header, PatchFormatError, pattern)

    def test_binary_and_non_utf8_targets(self) -> None:
        write_files(self.root, {"logo.bin": b"\x89PNG\x00\x00", "latin.txt": "café\n".encode("latin-1")})
        self.assert_rejected("--- a/logo.bin\n+++ b/logo.bin\n@@ -1 +1 @@\n-x\n+y\n", PatchFormatError, "binary")
        self.assert_rejected("--- a/latin.txt\n+++ b/latin.txt\n@@ -1 +1 @@\n-x\n+y\n", PatchFormatError, "UTF-8")

    def test_null_byte(self) -> None:
        self.assert_rejected(udiff("src/util.py", UTIL, "VALUE = 1\x00\nNAME = 'x'\n"), PatchFormatError, "null byte")


class LimitTests(PatchTestCase):
    def test_patch_too_large(self) -> None:
        patch = udiff("src/app.py", APP, APP + "# " + "x" * 300 + "\n")
        with mock.patch.object(changes, "MAX_PATCH_BYTES", 200):
            self.assert_rejected(patch, PatchLimitError, r"bytes; the limit is 200")

    def test_too_many_files_additions_deletions_and_hunks(self) -> None:
        three_files = "".join(udiff(f"src/new{i}.py", "", "x = 1\n", create=True) for i in range(3))
        many_lines = "".join(f"line {i}\n" for i in range(30))
        write_files(self.root, {"src/many.txt": many_lines})
        cases = [
            ("MAX_FILES_PER_PATCH", 2, three_files, "3 files"),
            ("MAX_ADDITIONS", 5, udiff("src/big.py", "", many_lines, create=True), "30 added lines"),
            ("MAX_DELETIONS", 5, udiff("src/many.txt", many_lines, "", delete=True), "30 deleted lines"),
            ("MAX_HUNKS_PER_PATCH", 1,
             udiff("src/many.txt", many_lines, many_lines.replace("line 1\n", "L1\n").replace("line 25\n", "L25\n")),
             "2 hunks"),
        ]  # fmt: skip
        for constant, value, patch, pattern in cases:
            with self.subTest(limit=constant), mock.patch.object(changes, constant, value):
                self.assert_rejected(patch, PatchLimitError, pattern)

    def test_file_size_limits(self) -> None:
        with mock.patch.object(changes, "MAX_FILE_BYTES", 10):
            self.assert_rejected(udiff("src/app.py", APP, APP + "#\n"), PatchLimitError, "larger than 10 bytes")
        with mock.patch.object(changes, "MAX_RESULT_FILE_BYTES", 50):
            self.assert_rejected(udiff("src/app.py", APP, APP + "# padding padding\n"), PatchLimitError, "would exceed 50")


class StalePatchTests(PatchTestCase):
    def test_stale_modification(self) -> None:
        patch = udiff("src/app.py", APP, APP.replace("return 0", "return 1"))
        write_files(self.root, {"src/app.py": APP.replace("hello", "hi")})
        message = self.assert_rejected(patch, PatchConflictError, "does not match the current file at line 2")
        self.assertNotIn("print", message)  # contents are never echoed

    def test_other_conflicts(self) -> None:
        self.assert_rejected(udiff("README.md", "", "# New\n", create=True), PatchConflictError, "already exists")
        self.assert_rejected(udiff("src/missing.py", "x\n", "y\n"), PatchConflictError, "does not exist")
        self.assert_rejected(udiff("src/util.py", "VALUE = 1\n", "", delete=True), PatchConflictError, "every line")
        self.assert_rejected(
            "--- a/src/util.py\n+++ b/src/util.py\n@@ -9,1 +9,1 @@\n-x\n+y\n", PatchConflictError, "past the end"
        )
        self.assert_rejected(udiff("README.md/child.py", "", "x\n", create=True), PatchConflictError, "not a directory")

    def test_missing_newline_mismatch_is_stale(self) -> None:
        self.assert_rejected(
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Demo\n\\ No newline at end of file\n+# X\n",
            PatchConflictError,
        )


# --- Security ------------------------------------------------------------------


class PathSecurityTests(PatchTestCase):
    def patch_for(self, rel: str) -> str:
        return f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1 @@\n+pwned = True\n"

    def test_paths_outside_the_workspace(self) -> None:
        cases = [
            "../secret.txt", "../../secret.txt", "src/../../secret.txt", self.secret.as_posix(), "/etc/passwd",
            "C:/Windows/win.ini", "C:\\Windows\\win.ini", "D:\\other-project\\x.py", "c:relative.txt",
            "\\\\server\\share\\x.py", "//server/share/x.py",
        ]  # fmt: skip
        for rel in cases:
            with self.subTest(path=rel):
                self.assert_rejected(self.patch_for(rel), PathOutsideWorkspaceError)
                modify = f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-TOP SECRET\n+changed\n"
                self.assert_rejected(modify, PathOutsideWorkspaceError)

    def test_unsafe_path_forms(self) -> None:
        for rel in ("src/./new.py", "src//new.py", "src/sub/../new.py", "src/new.py:stream", "src\\new.py",
                    "CON.txt", "src/aux.py", "src/trailing.", "src/space ", "src/new\x01.py"):  # fmt: skip
            with self.subTest(path=rel):
                self.assert_rejected(self.patch_for(rel), (PatchPathError, PatchFormatError, PathOutsideWorkspaceError))

    def test_symlink_escapes_and_links_inside(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "target.py").write_text("x = 1\n")
        try:
            os.symlink(outside, self.root / "linked_dir", target_is_directory=True)
            os.symlink(self.root / ".git" / "config", self.root / "innocent.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not permitted on this system.")
        self.assert_rejected(self.patch_for("linked_dir/new.py"), (PathOutsideWorkspaceError, PatchPathError))
        self.assert_rejected(udiff("linked_dir/target.py", "x = 1\n", "x = 2\n"), (PathOutsideWorkspaceError, PatchPathError))
        # A link that stays inside the workspace but points at .git/config is also refused.
        self.assert_rejected(udiff("innocent.txt", "[core]\n", "[core]\n\thooksPath = /tmp\n"), PatchPathError, "link")

    def test_windows_junction_escapes(self) -> None:
        try:
            import _winapi  # Windows only; junctions need no special privileges
        except ImportError:
            self.skipTest("Directory junctions are Windows-only.")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "target.py").write_bytes(b"x = 1\n")
        _winapi.CreateJunction(str(outside), str(self.root / "junction"))
        _winapi.CreateJunction(str(self.root / ".git"), str(self.root / "gitlink"))
        self.assert_rejected(self.patch_for("junction/new.py"), (PathOutsideWorkspaceError, PatchPathError))
        self.assert_rejected(udiff("junction/target.py", "x = 1\n", "x = 2\n"), (PathOutsideWorkspaceError, PatchPathError))
        # A junction that stays inside the workspace but leads into .git is refused as a link.
        self.assert_rejected(udiff("gitlink/config", "[core]\n", "[core]\n\thooksPath = x\n"), PatchPathError, "link")
        self.assertEqual((outside / "target.py").read_bytes(), b"x = 1\n")

    def test_protected_files(self) -> None:
        protected = [
            (".git/config", "[core]\n"), (".git/hooks/pre-commit", None), ("sub/.GIT/objects/x", None),
            (".env", f"API_KEY={SECRET_VALUE}\n"), (".env.local", None), ("config/.env.production", None),
            ("keys/id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\n"), ("certs/server.pem", None), ("deploy.key", None),
            ("credentials.json", None), ("secrets.yaml", None), (".npmrc", None), (".ENV", None),
        ]  # fmt: skip
        for rel, existing in protected:
            with self.subTest(path=rel):
                patch = udiff(rel, existing, existing + "extra = 1\n") if existing else self.patch_for(rel)
                message = self.assert_rejected(patch, ProtectedPathError, "protected")
                self.assertNotIn(SECRET_VALUE, message)
                self.assertNotIn("PRIVATE KEY", message)

    def test_one_bad_path_rejects_the_whole_patch(self) -> None:
        good = udiff("src/app.py", APP, APP.replace("return 0", "return 1"))
        for bad in (self.patch_for("../escape.py"), self.patch_for(".git/hooks/post-checkout"),
                    udiff("src/util.py", "WRONG\n", "x\n"), self.patch_for("README.md")):  # fmt: skip
            with self.subTest(bad=bad.splitlines()[1]):
                self.assert_rejected(good + bad)


class AtomicityTests(PatchTestCase):
    def multi_file_patch(self) -> str:
        return (
            udiff("src/app.py", APP, APP.replace("return 0", "return 1"))
            + udiff("src/util.py", UTIL, "", delete=True)
            + udiff("src/pkg/new.py", "", "NEW = 1\n", create=True)
            + udiff("README.md", "# Demo\n", "# Changed\n")
        )

    def assert_no_staging_files(self) -> None:
        self.assertEqual([p for p in self.base.rglob("*") if p.name.startswith(".devpilot-staging-")], [])

    def test_validation_failure_changes_nothing(self) -> None:
        stale_last = self.multi_file_patch().replace("-# Demo", "-# Something else")
        self.assert_rejected(stale_last, PatchConflictError)
        self.assertFalse((self.root / "src/pkg").exists())

    def test_write_failure_midway_rolls_back_every_file(self) -> None:
        before = contents(self.base)
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:  # after two files were already swapped in
                raise PermissionError("simulated failure")
            return real_replace(src, dst)

        with mock.patch.object(changes.os, "replace", side_effect=failing_replace):
            with self.assertRaisesRegex(PatchWriteError, "every file was restored"):
                self.apply(self.multi_file_patch())
        self.assertEqual(contents(self.base), before)
        self.assertFalse((self.root / "src/pkg").exists())
        self.assert_no_staging_files()
        self.assertEqual(self.registry.ids(), [])

    def test_staging_failure_changes_nothing(self) -> None:
        before = snapshot(self.base)
        real_mkstemp = changes.tempfile.mkstemp
        calls = {"n": 0}

        def failing_mkstemp(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(changes.tempfile, "mkstemp", side_effect=failing_mkstemp):
            with self.assertRaises(PatchWriteError):
                self.apply(self.multi_file_patch())
        self.assertEqual(snapshot(self.base), before)  # not even modification times changed
        self.assert_no_staging_files()


# --- Registry and revert ---------------------------------------------------------


class RevertTests(PatchTestCase):
    def test_registry_entry(self) -> None:
        patch = udiff("src/app.py", APP, APP.replace("return 0", "return 1"))
        result = self.apply(patch)
        record = self.registry.get(result.change_id)
        self.assertEqual(record.patch_sha256, hashlib.sha256(patch.encode()).hexdigest())
        self.assertEqual([(f.rel, f.kind, f.original) for f in record.files], [("src/app.py", "modified", APP.encode())])
        self.assertEqual(record.files[0].result_sha256, hashlib.sha256(self.read("src/app.py")).hexdigest())

    def test_change_ids_are_unique(self) -> None:
        ids = set()
        text = APP
        for i in range(20):
            new = text + f"# {i}\n"
            ids.add(self.apply(udiff("src/app.py", text, new)).change_id)
            text = new
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(re.fullmatch(r"chg_[0-9a-f]{16}", i) for i in ids))

    def test_revert_restores_exact_bytes(self) -> None:
        write_files(self.root, {"win.txt": b"\xef\xbb\xbfone\r\ntwo"})
        before = contents(self.root)
        patch = (
            "--- a/win.txt\n+++ b/win.txt\n@@ -1,2 +1,2 @@\n one\n-two\n\\ No newline at end of file\n+TWO\n"
            + AtomicityTests.multi_file_patch(self)  # type: ignore[arg-type]
        )
        result = self.apply(patch)
        self.assertNotEqual(contents(self.root), before)
        reverted = self.revert(result.change_id)
        self.assertEqual(contents(self.root), before)
        self.assertFalse((self.root / "src/pkg").exists())  # directories the patch created are removed
        self.assertEqual(reverted.files_restored_count, 5)
        self.assertEqual(
            [(f.path, f.action) for f in reverted.files_restored],
            [("win.txt", "restored"), ("src/app.py", "restored"), ("src/util.py", "recreated"),
             ("src/pkg/new.py", "removed"), ("README.md", "restored")],
        )  # fmt: skip
        self.assertEqual(self.registry.ids(), [])
        with self.assertRaisesRegex(UnknownChangeError, "No active change"):
            self.revert(result.change_id)  # only once

    def test_revert_refused_after_external_modification(self) -> None:
        result = self.apply(AtomicityTests.multi_file_patch(self))  # type: ignore[arg-type]
        (self.root / "README.md").write_bytes(b"# Edited by someone else\n")
        before = snapshot(self.base)
        with self.assertRaisesRegex(RevertConflictError, r"README\.md changed after the patch was applied"):
            self.revert(result.change_id)
        self.assertEqual(snapshot(self.base), before)  # no file reverted, not even the untouched ones
        self.assertEqual(self.registry.ids(), [result.change_id])  # still revertable once resolved
        (self.root / "README.md").write_bytes(b"# Changed\n")
        self.revert(result.change_id)
        self.assertEqual(self.read("src/app.py").decode(), APP)

    def test_revert_refused_when_created_or_deleted_files_changed(self) -> None:
        result = self.apply(udiff("src/util.py", UTIL, "", delete=True) + udiff("src/n.py", "", "N = 1\n", create=True))
        (self.root / "src/util.py").write_text("recreated by someone\n")
        with self.assertRaisesRegex(RevertConflictError, "src/util.py"):
            self.revert(result.change_id)
        (self.root / "src/util.py").unlink()
        (self.root / "src/n.py").unlink()
        with self.assertRaisesRegex(RevertConflictError, "src/n.py"):
            self.revert(result.change_id)

    def test_revert_write_failure_leaves_the_patched_state(self) -> None:
        result = self.apply(AtomicityTests.multi_file_patch(self))  # type: ignore[arg-type]
        after_apply = contents(self.base)
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise PermissionError("simulated failure")
            return real_replace(src, dst)

        with mock.patch.object(changes.os, "replace", side_effect=failing_replace):
            with self.assertRaises(PatchWriteError):
                self.revert(result.change_id)
        self.assertEqual(contents(self.base), after_apply)
        self.assertEqual(self.registry.ids(), [result.change_id])

    def test_invalid_and_unknown_change_ids(self) -> None:
        for bad in ("", "chg_", "chg_XYZ", "../../etc", "chg_0123456789abcdeg", None, 42):
            with self.subTest(change_id=bad):
                with self.assertRaisesRegex(UnknownChangeError, "must be 'chg_'"):
                    self.revert(bad)  # type: ignore[arg-type]
        with self.assertRaisesRegex(UnknownChangeError, "No active change"):
            self.revert("chg_0123456789abcdef")

    def test_registry_bounds(self) -> None:
        self.registry = ChangeRegistry(max_changes=2)
        text, ids = APP, []
        for i in range(3):
            new = text + f"# {i}\n"
            result = self.apply(udiff("src/app.py", text, new))
            ids.append(result.change_id)
            text = new
        self.assertEqual(result.evicted_change_ids, [ids[0]])
        self.assertEqual(self.registry.ids(), ids[1:])
        with self.assertRaises(UnknownChangeError):
            self.revert(ids[0])
        self.registry = ChangeRegistry(max_bytes=10)
        self.assertFalse(self.apply(udiff("src/app.py", text, text + "#\n")).reversible)


class NoExecutionTests(PatchTestCase):
    def test_no_process_is_ever_started(self) -> None:
        forbidden = AssertionError("a process was started")
        with mock.patch.object(subprocess, "Popen", side_effect=forbidden), mock.patch.object(
            os, "system", side_effect=forbidden
        ), mock.patch.object(os, "popen", side_effect=forbidden):
            result = self.apply(AtomicityTests.multi_file_patch(self))  # type: ignore[arg-type]
            self.revert(result.change_id)


class GitIntegrationTests(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("src/app.py", APP)
        self.write("src/util.py", UTIL)
        self.commit("Initial commit")
        self.registry = ChangeRegistry()

    def git_snapshot(self) -> dict[str, tuple[int, bytes]]:
        return snapshot(self.root / ".git")

    def test_git_generated_patch_round_trip(self) -> None:
        # Produce a real `git diff` (test setup only), undo it, then apply it through DevPilot.
        self.write("src/app.py", APP.replace("return 0", "return 42"))
        (self.root / "src/util.py").unlink()
        self.write("src/created.py", "CREATED = True\n")
        self.run_git("add", "-N", "src/created.py")
        patch = self.run_git("diff") + "\n"
        self.run_git("reset", "-q")
        self.run_git("checkout", "-q", "--", ".")
        (self.root / "src/created.py").unlink()
        self.assertIn("new file mode", patch)
        self.assertIn("deleted file mode", patch)

        git_before = self.git_snapshot()
        with mock.patch.object(git.subprocess, "Popen", side_effect=AssertionError("Git was invoked")):
            result = apply_patch(self.workspace, self.registry, patch)
        self.assertEqual(
            sorted((f.path, f.change) for f in result.files_changed),
            [("src/app.py", "modified"), ("src/created.py", "created"), ("src/util.py", "deleted")],
        )
        self.assertEqual(self.git_snapshot(), git_before)  # .git (index, HEAD, refs) untouched

        status = git.git_status(self.workspace)
        self.assertEqual(
            sorted((c.path, c.change) for c in status.unstaged), [("src/app.py", "modified"), ("src/util.py", "deleted")]
        )
        self.assertEqual(status.untracked, ["src/created.py"])

        with mock.patch.object(git.subprocess, "Popen", side_effect=AssertionError("Git was invoked")):
            revert_patch(self.workspace, self.registry, result.change_id)
        self.assertTrue(git.git_status(self.workspace).clean)
        self.assertEqual(self.git_snapshot(), git_before)


class SecretLeakTests(PatchTestCase):
    def test_token_and_secret_contents_never_appear(self) -> None:
        token = "ghp_" + "Z9y8X7w6" * 5
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": token}):
            patch = udiff("src/app.py", APP, APP + f"TOKEN = '{token}'\n")
            output = self.apply(patch).model_dump_json()
            stale = patch.replace("return 0", f"return '{token}'")
            messages = [str(self._error(stale)), str(self._error(udiff(".env", f"API_KEY={SECRET_VALUE}\n", "API_KEY=\n")))]
        for text in [output, *messages]:
            self.assertNotIn(token, text)
            self.assertNotIn(SECRET_VALUE, text)

    def _error(self, patch: str) -> Exception:
        try:
            self.apply(patch)
        except WorkspaceError as exc:
            return exc
        raise AssertionError("patch unexpectedly applied")


# --- MCP -----------------------------------------------------------------------


class PatchMcpTests(PatchTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_registration_and_annotations(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        self.assertEqual(len(tools), 15)
        apply_tool, revert_tool = tools["apply_patch"], tools["revert_patch"]
        self.assertEqual(apply_tool.input_schema["required"], ["patch"])
        self.assertEqual(set(apply_tool.input_schema["properties"]), {"patch"})
        self.assertEqual(apply_tool.input_schema["properties"]["patch"]["maxLength"], changes.MAX_PATCH_BYTES)
        self.assertEqual(set(revert_tool.input_schema["properties"]), {"change_id"})
        self.assertEqual(revert_tool.input_schema["properties"]["change_id"]["pattern"], changes.CHANGE_ID_PATTERN)
        for tool in (apply_tool, revert_tool):
            self.assertFalse(tool.annotations.read_only_hint)
            self.assertTrue(tool.annotations.destructive_hint)
            self.assertIn("YOU supply" if tool is apply_tool else "apply_patch", tool.description)
        for name, tool in tools.items():  # every other tool is still read-only
            if name not in ("apply_patch", "revert_patch"):
                self.assertTrue(tool.annotations.read_only_hint, name)

    async def test_round_trip_through_one_server(self) -> None:
        patch = udiff("src/app.py", APP, APP.replace("return 0", "return 7"))
        async with Client(create_server(self.workspace)) as client:
            applied = await client.call_tool("apply_patch", {"patch": patch})
            self.assertFalse(applied.is_error)
            read = await client.call_tool("read_file", {"path": "src/app.py"})
            self.assertIn("return 7", read.structured_content["content"])
            change_id = applied.structured_content["change_id"]
            reverted = await client.call_tool("revert_patch", {"change_id": change_id})
            self.assertFalse(reverted.is_error)
            self.assertTrue(reverted.structured_content["reverted"])
            again = await client.call_tool("revert_patch", {"change_id": change_id})
            self.assertTrue(again.is_error)
        self.assertEqual(self.read("src/app.py").decode(), APP)

    async def test_invalid_arguments(self) -> None:
        before = snapshot(self.base)
        async with Client(create_server(self.workspace)) as client:
            for tool, args in (("apply_patch", {"patch": ""}), ("apply_patch", {}), ("apply_patch", {"patch": 42}),
                               ("apply_patch", {"patch": "   "}), ("apply_patch", {"patch": "not a diff"}),
                               ("revert_patch", {}), ("revert_patch", {"change_id": "bad"}),
                               ("revert_patch", {"change_id": "chg_0123456789abcdef"})):  # fmt: skip
                with self.subTest(tool=tool, args=args):
                    self.assertTrue((await client.call_tool(tool, args)).is_error)
        self.assertEqual(snapshot(self.base), before)

    async def test_extra_arguments_do_not_expand_capabilities(self) -> None:
        patch = udiff("src/app.py", APP, APP.replace("return 0", "return 8"))
        extras = {"workspace": self.base.as_posix(), "path": "../secret.txt", "force": True, "allow_protected": True,
                  "shell": "rm -rf /", "unsafe": True}  # fmt: skip
        async with Client(create_server(self.workspace)) as client:
            ok = await client.call_tool("apply_patch", {"patch": patch, **extras})
            protected = await client.call_tool(
                "apply_patch", {"patch": udiff(".env", f"API_KEY={SECRET_VALUE}\n", "API_KEY=x\n"), **extras}
            )
        self.assertFalse(ok.is_error)
        self.assertTrue(protected.is_error)
        self.assertEqual(self.secret.read_text(), "TOP SECRET\n")
        self.assertEqual(self.read(".env").decode(), f"API_KEY={SECRET_VALUE}\n")


if __name__ == "__main__":
    unittest.main()
