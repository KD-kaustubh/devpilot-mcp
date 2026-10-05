"""Tests for DevPilot Studio's backend (devpilot_ui): no network, a scripted fake AI model."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from devpilot_mcp.server import create_server
from devpilot_ui import agent as agent_module
from devpilot_ui.app import create_app
from devpilot_ui.config import UISettings
from devpilot_ui.mcp_session import McpSession, stdio_server
from tests.helpers import WorkspaceTestCase

ORIGIN = {"origin": "http://127.0.0.1:8765"}
API_KEY = "sk-aipipe-NEVER-SHOW-THIS"


def text(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))])


def call(index: int, *, id: str | None = None, name: str | None = None, args: str | None = None) -> SimpleNamespace:
    tool_call = SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[tool_call]))])


class FakeLLM:
    """Stands in for openai.AsyncOpenAI: each create() plays the next scripted list of stream chunks."""

    def __init__(self, *scripts: list[SimpleNamespace] | Exception) -> None:
        self.scripts = list(scripts)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request: Any):
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else [text("done")]
        if isinstance(script, Exception):
            raise script

        async def stream():
            for chunk in script:
                yield chunk

        return stream()


class StudioTestCase(WorkspaceTestCase):
    def client(self, llm: Any = None, *, api_key: str | None = API_KEY) -> TestClient:
        settings = UISettings(workspace_root=self.workspace.root, api_key=api_key)
        app = create_app(settings, server=create_server(self.workspace), llm_client=llm, allowed_hosts=["testserver"])
        return TestClient(app)

    @staticmethod
    def events_until(ws: Any, *stop: str, approve: bool | None = None) -> list[dict[str, Any]]:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "approval_required" and approve is not None:
                ws.send_json({"type": "approval", "call_id": event["call_id"], "approved": approve})
            if event["type"] in stop:
                return events


class StatusAndSafetyTests(StudioTestCase):
    def test_status_reports_workspace_and_tools_but_never_the_key(self) -> None:
        with self.client(FakeLLM()) as client:
            response = client.get("/api/status")
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["workspace"]["name"], "workspace")
        self.assertEqual(len(data["tools"]), 18)
        access = [t["access"] for t in data["tools"]]
        self.assertEqual((access.count("read"), access.count("write"), access.count("execute")), (14, 2, 2))
        self.assertEqual(data["ai"], {"configured": True, "model": "gpt-4.1-mini", "provider": "aipipe.org"})
        self.assertFalse(data["git"]["available"])  # the test workspace is not a Git repository
        self.assertNotIn(API_KEY, response.text)

    def test_other_websites_cannot_connect(self) -> None:
        with self.client(FakeLLM()) as client:
            for headers in ({"origin": "http://evil.example"}, {"origin": "null"}, {}):
                with self.subTest(headers=headers):
                    with self.assertRaises(WebSocketDisconnect):
                        with client.websocket_connect("/ws", headers=headers) as ws:
                            ws.receive_json()

    def test_foreign_host_header_is_rejected(self) -> None:
        # DNS-rebinding protection: a page on another domain resolving to 127.0.0.1 sends its own Host header.
        with self.client(FakeLLM()) as client:
            response = client.get("/api/status", headers={"host": "evil.example"})
        self.assertEqual(response.status_code, 400)

    def test_settings_repr_hides_the_key(self) -> None:
        self.assertNotIn(API_KEY, repr(UISettings(workspace_root=self.workspace.root, api_key=API_KEY)))


class AgentTests(StudioTestCase):
    def test_tool_round_trip_streams_events_and_feeds_results_back(self) -> None:
        llm = FakeLLM(
            [call(0, id="c1", name="search_code", args='{"query": '), call(0, args='"print"}')],
            [text("It is printed in "), text("`src/app.py`.")],
        )
        with self.client(llm) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "Where is Hello World printed?"})
            events = self.events_until(ws, "answer_done")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["tool_started", "tool_finished", "assistant_delta", "assistant_delta", "answer_done"])
        self.assertEqual(events[0]["name"], "search_code")
        self.assertEqual(events[0]["source"], "agent")
        self.assertEqual(events[1]["status"], "ok")
        self.assertEqual(events[1]["structured"]["matches"][0]["file"], "src/app.py")
        self.assertEqual(len(llm.requests[0]["tools"]), 18)
        tool_message = llm.requests[1]["messages"][-1]
        self.assertEqual((tool_message["role"], tool_message["tool_call_id"]), ("tool", "c1"))
        self.assertIn("src/app.py", tool_message["content"])
        self.assertTrue(llm.requests[0]["messages"][0]["content"].startswith("You are DevPilot Studio"))

    def test_write_tools_wait_for_approval_and_can_be_denied(self) -> None:
        patch = '{"patch": "--- /dev/null\\n+++ b/new.txt\\n@@ -0,0 +1 @@\\n+hello\\n"}'
        llm = FakeLLM(
            [call(0, id="c1", name="apply_patch", args=patch)], [text("You declined.")],
            [call(0, id="c2", name="apply_patch", args=patch)], [text("Created.")],
        )  # fmt: skip
        new_file = self.workspace.root / "new.txt"
        with self.client(llm) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "Create new.txt"})
            denied = self.events_until(ws, "answer_done", approve=False)
            self.assertFalse(new_file.exists())
            ws.send_json({"type": "ask", "text": "Create it, I approve"})
            approved = self.events_until(ws, "answer_done", approve=True)
        self.assertIn("approval_required", [e["type"] for e in denied])
        self.assertEqual(next(e for e in denied if e["type"] == "tool_finished")["status"], "denied")
        self.assertIn("DENIED", llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(next(e for e in approved if e["type"] == "tool_finished")["status"], "ok")
        self.assertEqual(new_file.read_text(encoding="utf-8"), "hello\n")

    def test_security_refusals_are_marked_blocked(self) -> None:
        llm = FakeLLM([call(0, id="c1", name="read_file", args='{"path": "../secret.txt"}')], [text("Refused.")])
        with self.client(llm) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "Read the secret"})
            events = self.events_until(ws, "answer_done")
        finished = next(e for e in events if e["type"] == "tool_finished")
        self.assertEqual(finished["status"], "blocked")
        self.assertNotIn("TOP SECRET", str(events))
        self.assertTrue(llm.requests[1]["messages"][-1]["content"].startswith("BLOCKED BY DEVPILOT SECURITY"))

    def test_invalid_tool_arguments_never_reach_the_tool(self) -> None:
        llm = FakeLLM([call(0, id="c1", name="apply_patch", args="{not json")], [text("Sorry.")])
        with self.client(llm) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "x"})
            events = self.events_until(ws, "answer_done")
        finished = next(e for e in events if e["type"] == "tool_finished")
        self.assertEqual(finished["status"], "error")
        self.assertNotIn("approval_required", [e["type"] for e in events])

    def test_tool_rounds_are_bounded(self) -> None:
        loop = [[call(0, id=f"c{i}", name="git_status", args="{}")] for i in range(agent_module.MAX_TOOL_ROUNDS + 2)]
        llm = FakeLLM(*loop)
        with self.client(llm) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "loop forever"})
            events = self.events_until(ws, "answer_done")
        self.assertEqual(len(llm.requests), agent_module.MAX_TOOL_ROUNDS + 1)
        self.assertEqual(llm.requests[-1]["tool_choice"], "none")
        self.assertEqual([e["type"] for e in events].count("tool_started"), agent_module.MAX_TOOL_ROUNDS)

    def test_ai_errors_are_reported_without_the_key(self) -> None:
        failure = RuntimeError("401 Unauthorized")
        failure.status_code = 401  # type: ignore[attr-defined]
        with self.client(FakeLLM(failure)) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "hello"})
            events = self.events_until(ws, "answer_done")
        error = next(e for e in events if e["type"] == "error")
        self.assertIn("AIPIPE_TOKEN", error["message"])
        self.assertNotIn(API_KEY, str(events))

    def test_long_results_are_capped_for_the_model(self) -> None:
        capped = agent_module._for_model("ok", "x" * 50_000)
        self.assertLess(len(capped), agent_module.MAX_RESULT_CHARS_FOR_MODEL + 100)
        self.assertIn("more characters omitted", capped)


class QuickActionTests(StudioTestCase):
    def test_quick_actions_work_without_ai(self) -> None:
        with self.client(None, api_key=None) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "ask", "text": "hello"})
            self.assertIn("not configured", ws.receive_json()["message"])
            ws.send_json({"type": "run_tool", "name": "analyze_repository", "arguments": {}})
            events = self.events_until(ws, "action_done")
        finished = next(e for e in events if e["type"] == "tool_finished")
        self.assertEqual((finished["status"], finished["structured"]["total_files"]), ("ok", 4))
        self.assertEqual(events[0]["source"], "user")

    def test_quick_actions_also_need_approval_to_run_tests(self) -> None:
        with self.client(None, api_key=None) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "run_tool", "name": "run_tests", "arguments": {}})
            events = self.events_until(ws, "action_done", approve=False)
            ws.send_json({"type": "run_tool", "name": "validate_repository", "arguments": {}})
            report = self.events_until(ws, "action_done", approve=False)
        self.assertEqual(next(e for e in events if e["type"] == "tool_finished")["status"], "denied")
        # validate_repository without run_tests executes nothing, so it runs without a prompt.
        self.assertNotIn("approval_required", [e["type"] for e in report])
        self.assertEqual(next(e for e in report if e["type"] == "tool_finished")["status"], "ok")


class QueryTests(StudioTestCase):
    """Silent queries power the file explorer and the changes panel: read-only tools only, no timeline events."""

    def test_status_includes_tool_schemas_for_the_tool_runner(self) -> None:
        with self.client(FakeLLM()) as client:
            tools = {t["name"]: t for t in client.get("/api/status").json()["tools"]}
        self.assertEqual(tools["search_code"]["input_schema"]["required"], ["query"])
        self.assertIn("path", tools["search_code"]["input_schema"]["properties"])
        self.assertTrue(tools["apply_patch"]["description"])

    def test_read_only_query_returns_the_result_without_timeline_events(self) -> None:
        with self.client(None, api_key=None) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "query", "request_id": "q1", "name": "list_directory", "arguments": {"path": "src"}})
            result = ws.receive_json()
            ws.send_json({"type": "query", "request_id": "q2", "name": "read_file", "arguments": {"path": "../secret.txt"}})
            blocked = ws.receive_json()
        self.assertEqual((result["type"], result["request_id"], result["status"]), ("query_result", "q1", "ok"))
        self.assertEqual([e["name"] for e in result["structured"]["entries"]], ["app.py", "util.py"])
        self.assertEqual((blocked["request_id"], blocked["status"]), ("q2", "blocked"))
        self.assertNotIn("TOP SECRET", blocked["text"])

    def test_queries_never_run_write_or_execute_tools(self) -> None:
        patch = "--- /dev/null\n+++ b/sneaky.txt\n@@ -0,0 +1 @@\n+x\n"
        attempts = [("apply_patch", {"patch": patch}), ("revert_patch", {"change_id": "chg_0123456789abcdef"}),
                    ("run_tests", {}), ("validate_repository", {}), ("no_such_tool", {}), ("read_file", "not-a-dict")]
        with self.client(None, api_key=None) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            replies = []
            for i, (name, args) in enumerate(attempts):
                ws.send_json({"type": "query", "request_id": f"r{i}", "name": name, "arguments": args})
                replies.append(ws.receive_json())
        self.assertTrue(all(r["type"] == "query_result" and r["status"] == "refused" for r in replies), replies)
        self.assertFalse((self.workspace.root / "sneaky.txt").exists())

    def test_queries_work_while_an_action_is_waiting_for_approval(self) -> None:
        with self.client(None, api_key=None) as client, client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "run_tool", "name": "run_tests", "arguments": {}})
            self.events_until(ws, "approval_required")
            ws.send_json({"type": "query", "request_id": "q", "name": "list_directory", "arguments": {}})
            reply = ws.receive_json()
            self.assertEqual((reply["type"], reply["status"]), ("query_result", "ok"))
            ws.send_json({"type": "run_tool", "name": "git_status", "arguments": {}})
            self.assertTrue(ws.receive_json().get("refused"))  # the approval-waiting action keeps the tab busy


class StdioSessionTests(StudioTestCase):
    def test_real_server_process_over_stdio(self) -> None:
        import asyncio

        async def main() -> dict[str, str]:
            settings = UISettings(workspace_root=self.workspace.root, api_key=API_KEY)
            params = stdio_server(settings)
            self.assertNotIn(API_KEY, str(params.env))  # the AI key never reaches the MCP server
            async with McpSession(params) as session:
                is_error, _, structured = await session.call("read_file", {"path": "src/app.py"})
                self.assertFalse(is_error)
                self.assertIn("Hello World", structured["content"])
                return {t.name: t.access for t in session.tools.values()}

        tools = asyncio.run(main())
        self.assertEqual(len(tools), 18)
        self.assertEqual({n for n, a in tools.items() if a != "read"},
                         {"apply_patch", "revert_patch", "run_tests", "validate_repository"})  # fmt: skip


if __name__ == "__main__":
    unittest.main()
