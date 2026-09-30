"""End-to-end tests through a real MCP client connected in-process."""

import unittest

from mcp import Client

from devpilot_mcp.server import create_server
from tests.helpers import WorkspaceTestCase


class ServerTests(WorkspaceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_discoverable_and_read_only(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(
            set(tools),
            {"list_directory", "read_file", "search_files", "search_code", "analyze_repository",
             "git_status", "git_log", "git_diff", "git_branch",
             "github_repository", "github_issues", "github_pull_requests", "investigate_repository",
             "apply_patch", "revert_patch", "get_test_commands", "run_tests", "validate_repository"},
        )  # fmt: skip
        # Phase 7 writes files; Phase 8 run_tests (and validate_repository on request) executes test code.
        write_tools = {"apply_patch", "revert_patch", "run_tests", "validate_repository"}
        for name, tool in tools.items():
            if name in write_tools:
                self.assertFalse(tool.annotations.read_only_hint)
                self.assertTrue(tool.annotations.destructive_hint)
            else:
                self.assertTrue(tool.annotations.read_only_hint)

    async def test_annotation_profiles_are_intentional(self) -> None:
        """Every tool's full annotation profile, pinned (see the semantics in tools/common.py).

        openWorldHint is true only where repository test code can run; DevPilot's own network
        access goes to one fixed host (api.github.com), so GitHub-reading tools are closed-world.
        """
        read_only = (True, False, True, False)  # (readOnly, destructive, idempotent, openWorld)
        writes_files = (False, True, False, False)
        executes_code = (False, True, False, True)
        expected = {
            "list_directory": read_only, "read_file": read_only, "search_files": read_only,
            "search_code": read_only, "analyze_repository": read_only,
            "git_status": read_only, "git_log": read_only, "git_diff": read_only, "git_branch": read_only,
            "github_repository": read_only, "github_issues": read_only, "github_pull_requests": read_only,
            "investigate_repository": read_only, "get_test_commands": read_only,
            "apply_patch": writes_files, "revert_patch": writes_files,
            "run_tests": executes_code, "validate_repository": executes_code,
        }  # fmt: skip
        async with Client(create_server(self.workspace)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(len(tools), 18)
        actual = {
            name: (t.annotations.read_only_hint, t.annotations.destructive_hint,
                   t.annotations.idempotent_hint, t.annotations.open_world_hint)
            for name, t in tools.items()
        }  # fmt: skip
        self.assertEqual(actual, expected)
        self.assertEqual({n for n, a in actual.items() if a[3]}, {"run_tests", "validate_repository"})

    async def test_search_files_path_round_trip(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = tools["search_files"].input_schema
            scoped = await client.call_tool("search_files", {"query": "hello", "path": "src"})
            whole = await client.call_tool("search_files", {"query": "hello"})
            escapes = [
                await client.call_tool("search_files", {"query": "TOP SECRET", "path": attack})
                for attack in ("../", str(self.secret), "C:\\Windows", "\\\\server\\share")
            ]
        self.assertEqual(set(schema["properties"]), {"query", "path"})
        self.assertEqual(schema["properties"]["path"]["default"], ".")
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual(scoped.structured_content["files_with_matches"], ["src/app.py"])
        self.assertEqual(whole.structured_content["files_with_matches"], ["README.md", "src/app.py"])
        for result in escapes:
            self.assertTrue(result.is_error)
            self.assertNotIn("TOP SECRET", result.content[0].text)

    async def test_secret_files_never_reach_the_client(self) -> None:
        secret_value = "sk-live-NEVER-LEAK-ME"
        (self.workspace.root / ".env").write_text(f"API_KEY={secret_value}\n", encoding="utf-8")
        async with Client(create_server(self.workspace)) as client:
            calls = [
                await client.call_tool("read_file", {"path": ".env"}),
                await client.call_tool("search_files", {"query": "NEVER-LEAK"}),  # the query itself is echoed back
                await client.call_tool("search_code", {"query": "api_key"}),
                await client.call_tool("search_files", {"query": "API_KEY", "path": ".env"}),
            ]
        self.assertTrue(calls[0].is_error)
        self.assertIn("never returns its contents", calls[0].content[0].text)
        self.assertEqual((calls[1].structured_content["matches"], calls[2].structured_content["matches"]), ([], []))
        self.assertTrue(calls[3].is_error)
        for result in calls:
            self.assertNotIn(secret_value, result.content[0].text)

    async def test_successful_calls_return_structured_content(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            listing = await client.call_tool("list_directory", {"path": "."})
            content = await client.call_tool("read_file", {"path": "src/app.py"})
            search = await client.call_tool("search_files", {"query": "TODO"})

        self.assertFalse(listing.is_error)
        self.assertIn("src", [e["name"] for e in listing.structured_content["entries"]])
        self.assertFalse(content.is_error)
        self.assertIn("Hello World", content.structured_content["content"])
        self.assertFalse(search.is_error)
        self.assertEqual(search.structured_content["files_with_matches"], ["src/util.py"])

    async def test_errors_reach_the_client_as_tool_errors(self) -> None:
        cases = {
            ("read_file", "src/missing.py"): "File not found",
            ("read_file", "../secret.txt"): "escapes the workspace",
            ("read_file", str(self.secret)): "Absolute paths are not allowed",
            ("list_directory", "../.."): "escapes the workspace",
        }
        async with Client(create_server(self.workspace)) as client:
            for (tool, path), expected in cases.items():
                with self.subTest(tool=tool, path=path):
                    result = await client.call_tool(tool, {"path": path})
                    self.assertTrue(result.is_error)
                    text = result.content[0].text
                    self.assertIn(expected, text)
                    self.assertNotIn("TOP SECRET", text)

    async def test_search_code_round_trip(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            everywhere = await client.call_tool("search_code", {"query": "print"})
            scoped = await client.call_tool("search_code", {"query": "VALUE", "path": "src/util.py"})
            nothing = await client.call_tool("search_code", {"query": "no_such_symbol"})

        self.assertFalse(everywhere.is_error)
        self.assertEqual(
            everywhere.structured_content["matches"],
            [{"file": "src/app.py", "line": 2, "text": "print('Hello World')"}],
        )
        self.assertEqual(scoped.structured_content["matches"], [{"file": "src/util.py", "line": 2, "text": "VALUE = 42"}])
        self.assertFalse(nothing.is_error)
        self.assertEqual(nothing.structured_content["matches"], [])

    async def test_search_code_errors_reach_the_client(self) -> None:
        cases = [
            ({"query": "x", "path": "../"}, "escapes the workspace"),
            ({"query": "x", "path": "C:\\Windows"}, "Absolute paths are not allowed"),
            ({"query": "x", "path": "nope"}, "Search path not found"),
            ({"query": "  "}, "must not be empty"),
        ]
        async with Client(create_server(self.workspace)) as client:
            for args, expected in cases:
                with self.subTest(args=args):
                    result = await client.call_tool("search_code", args)
                    self.assertTrue(result.is_error)
                    self.assertIn(expected, result.content[0].text)

    async def test_analyze_repository_is_registered_without_parameters(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = tools["analyze_repository"]
        self.assertEqual(tool.input_schema.get("properties", {}), {})
        self.assertIn("heuristics", tool.output_schema["properties"])

    async def test_analyze_repository_round_trip(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            result = await client.call_tool("analyze_repository", {})

        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual(data["total_files"], 4)  # node_modules/dep.js is ignored
        self.assertEqual(data["languages"], {"Python": 2, "Markdown": 1})
        self.assertEqual(data["documentation_files"], ["README.md"])
        self.assertEqual(data["directories"], [{"path": "src", "file_count": 2}])
        self.assertEqual(
            data["heuristics"]["possible_entry_points"],
            [{"file": "src/app.py", "kind": "filename", "detail": "conventional entry-point filename 'app.py'"}],
        )

    async def test_analyze_repository_ignores_path_arguments(self) -> None:
        # The tool takes no path, so a smuggled one must not widen its reach.
        (self.secret.parent / "package.json").write_text('{"main": "outside.js"}', encoding="utf-8")
        async with Client(create_server(self.workspace)) as client:
            result = await client.call_tool("analyze_repository", {"path": "../"})
        # The SDK drops unknown arguments, so this analyzes the workspace as usual.
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["dependency_manifests"], [])
        self.assertEqual(result.structured_content["total_files"], 4)

    async def test_analyze_repository_missing_root_is_a_tool_error(self) -> None:
        server = create_server(self.workspace)
        self.tearDown()  # delete the workspace after the server was created
        async with Client(server) as client:
            result = await client.call_tool("analyze_repository", {})
        self.assertTrue(result.is_error)
        self.assertIn("workspace root no longer exists", result.content[0].text)
        self.setUp()  # recreate so the normal tearDown succeeds


if __name__ == "__main__":
    unittest.main()
