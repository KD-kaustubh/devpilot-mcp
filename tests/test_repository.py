"""Tests for the analyze_repository tool logic."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devpilot_mcp.config import ConfigError, load_settings
from devpilot_mcp.tools import repository
from devpilot_mcp.tools.repository import analyze_repository, detect_language
from devpilot_mcp.workspace import PathNotFoundError, Workspace
from tests.helpers import WorkspaceTestCase

PYPROJECT = """\
[project]
name = "shop"
dependencies = ["Flask>=3.0", "SQLAlchemy[asyncio]~=2.0", "python-dotenv"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
shop = "shop.cli:main"
"""

PACKAGE_JSON = {
    "name": "shop-web",
    "main": "src/index.js",
    "bin": {"shop-cli": "bin/cli.js"},
    "scripts": {"start": "node src/server.js", "test": "jest"},
    "dependencies": {"express": "^4.19.0", "react": "^18.0.0"},
    "devDependencies": {"jest": "^29.0.0"},
}


class RepositoryTestCase(unittest.TestCase):
    """Builds a throwaway repository from a {relative path: content} mapping."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, files: dict[str, str | bytes]) -> None:
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")

    def analyze(self):
        return analyze_repository(Workspace(self.root))


class PythonRepositoryTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            {
                "README.md": "# Shop\n",
                "LICENSE": "MIT\n",
                "docs/usage.md": "Usage\n",
                "pyproject.toml": PYPROJECT,
                "requirements-dev.txt": "# dev tools\n-r requirements.txt\ndjango-debug-toolbar==4.0\n",
                "poetry.lock": "",
                ".env.example": "DATABASE_URL=\n",
                "Dockerfile": "FROM python:3.12\n",
                ".github/workflows/ci.yml": "on: push\n",
                "shop/__init__.py": "",
                "shop/app.py": "app = None\n",
                "shop/cli.py": "def main(): ...\n",
                "shop/models/order.py": "class Order: ...\n",
                "shop/static/site.css": "body {}\n",
                "tests/conftest.py": "",
                "tests/test_orders.py": "def test_x(): ...\n",
                "tests/api/order_test.py": "def test_y(): ...\n",
                "tests/app.py": "# fixture app, not an entry point\n",
            }
        )

    def test_basic_analysis(self) -> None:
        result = self.analyze()
        self.assertEqual(result.total_files, 18)
        self.assertTrue(result.scan_complete)
        self.assertEqual(result.truncated_fields, [])
        self.assertEqual(result.warnings, [])

    def test_language_counts(self) -> None:
        result = self.analyze()
        self.assertEqual(
            result.languages,
            {"Python": 8, "Markdown": 2, "CSS": 1, "Dockerfile": 1, "TOML": 1, "Text": 1, "YAML": 1},
        )
        # LICENSE, poetry.lock and .env.example have no recognized language.
        self.assertEqual(result.unclassified_files, 3)
        # Ordered by count (descending), then name.
        self.assertEqual(list(result.languages)[:2], ["Python", "Markdown"])

    def test_directories_are_two_levels_deep_with_counts(self) -> None:
        result = self.analyze()
        dirs = {d.path: d.file_count for d in result.directories}
        self.assertEqual(dirs["shop"], 5)
        self.assertEqual(dirs["shop/models"], 1)
        self.assertEqual(dirs["tests"], 4)
        self.assertEqual(dirs["tests/api"], 1)
        self.assertEqual(dirs[".github"], 1)
        self.assertNotIn(".github/workflows", {d.path for d in result.directories[:4]})  # shallow ones first
        self.assertTrue(all(d.path.count("/") <= 1 for d in result.directories))

    def test_important_files(self) -> None:
        result = self.analyze()
        self.assertEqual(result.documentation_files, ["LICENSE", "README.md", "docs/usage.md"])
        self.assertEqual(result.dependency_manifests, ["pyproject.toml", "requirements-dev.txt"])
        self.assertEqual(result.lock_files, ["poetry.lock"])
        self.assertEqual(
            result.configuration_files, [".env.example", "Dockerfile", "pyproject.toml", ".github/workflows/ci.yml"]
        )

    def test_tests_detection(self) -> None:
        tests = self.analyze().tests
        self.assertEqual(tests.directories, ["tests"])
        self.assertEqual(tests.files, ["tests/test_orders.py", "tests/api/order_test.py"])
        self.assertEqual(tests.file_count, 2)  # conftest.py and tests/app.py don't match test-file patterns

    def test_entry_points(self) -> None:
        entry_points = self.analyze().heuristics.possible_entry_points
        self.assertEqual(
            [(e.file, e.kind, e.detail) for e in entry_points],
            [
                ("pyproject.toml", "manifest", "[project.scripts] shop = shop.cli:main"),
                ("shop/app.py", "filename", "conventional entry-point filename 'app.py'"),
            ],
        )  # tests/app.py is excluded because it lives in a test directory

    def test_framework_indicators_come_from_declared_dependencies(self) -> None:
        indicators = self.analyze().heuristics.framework_indicators
        self.assertEqual(
            [(i.name, i.file, i.evidence) for i in indicators],
            [
                ("Flask", "pyproject.toml", "declares dependency 'flask'"),
                ("pytest", "pyproject.toml", "declares dependency 'pytest'"),
            ],
        )  # 'django-debug-toolbar' is not 'django', so Django is not claimed


class JavaScriptRepositoryTests(RepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            {
                "package.json": json.dumps(PACKAGE_JSON),
                "package-lock.json": "{}",
                "tsconfig.json": "{}",
                "src/index.js": "",
                "src/server.ts": "",
                "src/components/Button.tsx": "",
                "src/components/Button.test.tsx": "",
                "src/api.spec.ts": "",
                "__tests__/app.test.js": "",
            }
        )

    def test_languages(self) -> None:
        self.assertEqual(self.analyze().languages, {"JSON": 3, "TypeScript": 4, "JavaScript": 2})

    def test_manifest_and_lock_file(self) -> None:
        result = self.analyze()
        self.assertEqual(result.dependency_manifests, ["package.json"])
        self.assertEqual(result.lock_files, ["package-lock.json"])
        self.assertEqual(result.configuration_files, ["tsconfig.json"])

    def test_test_patterns(self) -> None:
        tests = self.analyze().tests
        self.assertEqual(tests.directories, ["__tests__"])
        self.assertEqual(
            tests.files, ["__tests__/app.test.js", "src/api.spec.ts", "src/components/Button.test.tsx"]
        )

    def test_package_json_entry_points_and_frameworks(self) -> None:
        heuristics = self.analyze().heuristics
        self.assertEqual(
            [(e.file, e.detail) for e in heuristics.possible_entry_points],
            [
                ("package.json", '"main": "src/index.js"'),
                ("package.json", '"bin.shop-cli": "bin/cli.js"'),
                ("package.json", '"scripts.start": "node src/server.js"'),
                ("src/index.js", "conventional entry-point filename 'index.js'"),
                ("src/server.ts", "conventional entry-point filename 'server.ts'"),
            ],
        )
        self.assertEqual(
            [i.name for i in heuristics.framework_indicators], ["Express", "Jest", "React"]  # sorted by dependency name
        )


class OtherEcosystemTests(RepositoryTestCase):
    def test_languages_by_extension_and_name(self) -> None:
        cases = {
            "a.py": "Python", "a.js": "JavaScript", "a.ts": "TypeScript", "A.java": "Java", "a.go": "Go",
            "a.rs": "Rust", "a.cpp": "C++", "a.cc": "C++", "a.cxx": "C++", "a.c": "C", "a.html": "HTML",
            "a.css": "CSS", "a.json": "JSON", "a.yaml": "YAML", "a.yml": "YAML", "dir/A.PY": "Python",
            "Dockerfile": "Dockerfile", "Makefile": "Makefile",
        }  # fmt: skip
        for path, language in cases.items():
            with self.subTest(path=path):
                self.assertEqual(detect_language(path), language)
        for path in ("LICENSE", "data.bin", "image.png", ".gitignore"):
            with self.subTest(path=path):
                self.assertIsNone(detect_language(path))

    def test_go_rust_java_manifests(self) -> None:
        self.write(
            {
                "go.mod": "module x\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
                "go.sum": "",
                "cmd/api/main.go": "package main\n",
                "Cargo.toml": '[package]\nname = "x"\n\n[dependencies]\naxum = "0.7"\n\n[[bin]]\nname = "tool"\npath = "src/tool.rs"\n',
                "pom.xml": "<parent><groupId>org.springframework.boot</groupId></parent>",
            }
        )
        result = self.analyze()
        self.assertEqual(result.dependency_manifests, ["Cargo.toml", "go.mod", "pom.xml"])
        self.assertEqual(result.lock_files, ["go.sum"])
        self.assertEqual(
            [(i.name, i.file) for i in result.heuristics.framework_indicators],
            [("Axum", "Cargo.toml"), ("Gin", "go.mod"), ("Spring Boot", "pom.xml")],
        )
        self.assertEqual(
            [(e.file, e.detail) for e in result.heuristics.possible_entry_points],
            [("Cargo.toml", "[[bin]] tool = src/tool.rs"), ("cmd/api/main.go", "conventional entry-point filename 'main.go'")],
        )

    def test_jsx_tsx_entry_point_filenames(self) -> None:
        self.write({"web/src/main.tsx": "", "admin/index.jsx": "", "web/src/Cart.tsx": ""})
        self.assertEqual(
            [e.file for e in self.analyze().heuristics.possible_entry_points], ["admin/index.jsx", "web/src/main.tsx"]
        )

    def test_framework_not_claimed_from_filenames(self) -> None:
        # Files named after frameworks, with no manifest declaring them, prove nothing.
        self.write({"django.py": "", "flask/app.py": "", "react.js": "", "manage.py": ""})
        self.assertEqual(self.analyze().heuristics.framework_indicators, [])

    def test_invalid_manifests_produce_warnings_not_errors(self) -> None:
        self.write({"package.json": "{not json", "pyproject.toml": "[project\n", "sub/package.json": "[1, 2]"})
        result = self.analyze()
        self.assertEqual(result.dependency_manifests, ["package.json", "pyproject.toml", "sub/package.json"])
        self.assertEqual(
            result.warnings,
            [
                "package.json: could not be parsed (JSONDecodeError).",
                "pyproject.toml: could not be parsed (TOMLDecodeError).",
                "sub/package.json: unexpected top-level structure.",
            ],
        )

    def test_setup_py_is_listed_but_never_inspected(self) -> None:
        self.write({"setup.py": "raise SystemExit('must not run')\ninstall_requires=['flask']\n"})
        result = self.analyze()
        self.assertEqual(result.dependency_manifests, ["setup.py"])
        self.assertEqual(result.heuristics.framework_indicators, [])
        self.assertEqual(result.warnings, [])


class IgnoredDirectoryTests(RepositoryTestCase):
    def test_ignored_and_generated_dirs_are_not_counted(self) -> None:
        self.write(
            {
                "src/main.py": "",
                ".git/config": "",
                ".venv/lib/site.py": "",
                "node_modules/react/package.json": '{"dependencies": {"express": "1"}}',
                "__pycache__/main.cpython-312.pyc": b"\x00\x01",
                "build/lib/main.py": "",
                "dist/bundle.js": "",
                "shop.egg-info/PKG-INFO": "",
                ".pytest_cache/README.md": "",
            }
        )
        result = self.analyze()
        self.assertEqual(result.total_files, 1)
        self.assertEqual(result.languages, {"Python": 1})
        self.assertEqual([d.path for d in result.directories], ["src"])
        self.assertEqual(result.dependency_manifests, [])
        self.assertEqual(result.heuristics.framework_indicators, [])


class MinimalRepositoryTests(RepositoryTestCase):
    def test_empty_repository(self) -> None:
        result = self.analyze()
        self.assertEqual(result.total_files, 0)
        self.assertTrue(result.scan_complete)
        self.assertEqual(result.languages, {})
        self.assertEqual(result.unclassified_files, 0)
        self.assertEqual(result.directories, [])
        self.assertEqual(result.documentation_files, [])
        self.assertEqual(result.configuration_files, [])
        self.assertEqual(result.dependency_manifests, [])
        self.assertEqual(result.lock_files, [])
        self.assertEqual((result.tests.directories, result.tests.files, result.tests.file_count), ([], [], 0))
        self.assertEqual(result.heuristics.possible_entry_points, [])
        self.assertEqual(result.heuristics.framework_indicators, [])

    def test_single_file_repository(self) -> None:
        self.write({"hello.go": "package main\n"})
        result = self.analyze()
        self.assertEqual(result.total_files, 1)
        self.assertEqual(result.languages, {"Go": 1})
        self.assertEqual(result.directories, [])

    def test_binary_files_are_counted_but_never_read(self) -> None:
        self.write({"logo.png": b"\x89PNG\x00\x00", "requirements.txt": b"\x00\xff binary"})
        result = self.analyze()
        self.assertEqual(result.total_files, 2)
        self.assertEqual(result.unclassified_files, 1)
        self.assertEqual(result.warnings, ["requirements.txt: could not be parsed (NotATextFileError)."])

    def test_output_is_deterministic(self) -> None:
        self.write({"b/x.py": "", "a/y.py": "", "README.md": "", "c.js": ""})
        self.assertEqual(self.analyze(), self.analyze())


class LimitTests(RepositoryTestCase):
    def test_lists_are_capped_and_reported(self) -> None:
        self.write({f"pkg{i:02d}/test_{i:02d}.py": "" for i in range(repository.MAX_LIST_ITEMS + 5)})
        result = self.analyze()
        self.assertEqual(len(result.tests.files), repository.MAX_LIST_ITEMS)
        self.assertEqual(result.tests.file_count, repository.MAX_LIST_ITEMS + 5)
        self.assertEqual(len(result.directories), repository.MAX_LIST_ITEMS)
        self.assertEqual(result.truncated_fields, ["directories", "tests.files"])

    def test_file_scan_limit(self) -> None:
        self.write({f"f{i}.py": "" for i in range(10)})
        with mock.patch.object(repository, "MAX_FILES_SCANNED", 4):
            result = self.analyze()
        self.assertEqual(result.total_files, 4)
        self.assertFalse(result.scan_complete)


class WorkspaceSafetyTests(WorkspaceTestCase):
    """Uses the shared fixture, which has a secret.txt just outside the workspace."""

    def test_nothing_outside_the_workspace_is_counted(self) -> None:
        (self.secret.parent / "package.json").write_text('{"dependencies": {"express": "1"}}', encoding="utf-8")
        result = analyze_repository(self.workspace)
        every_path = (
            result.documentation_files + result.configuration_files + result.dependency_manifests
            + [d.path for d in result.directories]
        )  # fmt: skip
        self.assertTrue(all(not p.startswith("..") and not os.path.isabs(p) for p in every_path))
        self.assertEqual(result.dependency_manifests, [])
        self.assertEqual(result.heuristics.framework_indicators, [])

    def test_symlinked_directory_outside_workspace_not_followed(self) -> None:
        outside = self.secret.parent / "outside_repo"
        outside.mkdir()
        (outside / "package.json").write_text('{"dependencies": {"express": "1"}}', encoding="utf-8")
        try:
            os.symlink(outside, self.workspace.root / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not permitted on this system.")
        result = analyze_repository(self.workspace)
        self.assertEqual(result.dependency_manifests, [])
        self.assertNotIn("linked", [d.path for d in result.directories])

    def test_analysis_does_not_modify_the_workspace(self) -> None:
        def snapshot() -> dict[str, float]:
            return {str(p): p.stat().st_mtime for p in self.workspace.root.rglob("*")}

        before = snapshot()
        analyze_repository(self.workspace)
        self.assertEqual(snapshot(), before)

    def test_missing_workspace_root(self) -> None:
        shutil.rmtree(self.workspace.root)
        with self.assertRaises(PathNotFoundError):
            analyze_repository(self.workspace)

    def test_config_rejects_nonexistent_workspace(self) -> None:
        missing = self.secret.parent / "does-not-exist"
        with mock.patch.dict(os.environ, {"DEVPILOT_WORKSPACE": str(missing)}):
            with self.assertRaises(ConfigError):
                load_settings()

    def test_config_rejects_file_as_workspace(self) -> None:
        with mock.patch.dict(os.environ, {"DEVPILOT_WORKSPACE": str(self.secret)}):
            with self.assertRaises(ConfigError):
                load_settings()


if __name__ == "__main__":
    unittest.main()
