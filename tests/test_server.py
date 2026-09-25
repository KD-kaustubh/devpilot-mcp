"""End-to-end tests through a real MCP client connected in-process."""

import unittest

from mcp import Client

from devpilot_mcp.server import create_server
from tests.helpers import WorkspaceTestCase


class ServerTests(WorkspaceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_discoverable_and_read_only(self) -> None:
        async with Client(create_server(self.workspace)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(set(tools), {"list_directory", "read_file", "search_files", "search_code"})
        for tool in tools.values():
            self.assertTrue(tool.annotations.read_only_hint)

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


if __name__ == "__main__":
    unittest.main()
