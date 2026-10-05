"""The UI's MCP client session and the single path every tool call takes.

`McpSession` keeps one long-lived MCP client connection to the DevPilot server
(over stdio in production), so a change applied with apply_patch can be
reverted later in the same UI session. `ToolRunner.execute` is the only way the
UI runs a tool, whether the AI or a quick action asked for it: it emits the
events the browser animates, and it refuses to run a tool that writes files or
executes code until the user has explicitly approved that exact call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp import Client, StdioServerParameters

from devpilot_ui.config import UISettings

Access = Literal["read", "write", "execute"]
Status = Literal["ok", "error", "blocked", "denied"]
Emit = Callable[[dict[str, Any]], Awaitable[None]]
Approve = Callable[[str], Awaitable[bool]]

MAX_UI_RESULT_CHARS = 200_000  # what the browser receives; the model gets far less (agent.py)

# Server-side variables the DevPilot server may need. The MCP SDK passes only a small safe set of
# system variables to the child process, so nothing else (such as AIPIPE_TOKEN) reaches it.
_SERVER_ENV_PASSTHROUGH = ("GITHUB_TOKEN", "DEVPILOT_TEST_PYTHON")

# Messages DevPilot uses when its security boundary refuses a request; shown with a shield in the UI.
_SECURITY_MARKERS = (
    "escapes the workspace",
    "absolute paths are not allowed",
    "is protected",
    "never returns its contents",
    "symbolic link or junction",
)


def stdio_server(settings: UISettings) -> StdioServerParameters:
    """Launch parameters for the DevPilot MCP server, pointed at the configured workspace."""
    env = {"DEVPILOT_WORKSPACE": str(settings.workspace_root)}
    env.update({k: os.environ[k] for k in _SERVER_ENV_PASSTHROUGH if os.environ.get(k)})
    return StdioServerParameters(command=sys.executable, args=["-m", "devpilot_mcp"], env=env)


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    access: Access

    @property
    def summary(self) -> str:
        return self.description.strip().split("\n", 1)[0]


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    name: str
    status: Status
    text: str
    structured: dict[str, Any] | None
    duration_ms: int


def access_of(annotations: Any) -> Access:
    """Map a tool's MCP annotations to the three profiles DevPilot uses (tools/common.py)."""
    if annotations is not None and annotations.read_only_hint:
        return "read"
    if annotations is not None and annotations.open_world_hint:
        return "execute"
    return "write"


def requires_approval(tool: ToolInfo, arguments: dict[str, Any]) -> bool:
    """Every call that can write files or run code needs the user's explicit approval.

    validate_repository only executes code when run_tests is true; without it the report runs
    nothing, so it is treated as read-only.
    """
    if tool.access == "read":
        return False
    if tool.name == "validate_repository" and arguments.get("run_tests") is not True:
        return False
    return True


def is_security_block(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SECURITY_MARKERS)


class McpSession:
    """One MCP client connection, used as an async context manager for the UI's lifetime."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self.tools: dict[str, ToolInfo] = {}

    async def __aenter__(self) -> McpSession:
        self._client = Client(self._server)
        await self._client.__aenter__()
        listed = await self._client.list_tools()
        self.tools = {
            t.name: ToolInfo(t.name, t.description or "", dict(t.input_schema or {}), access_of(t.annotations))
            for t in listed.tools
        }
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        """Call a tool; returns (is_error, text, structured_content)."""
        if self._client is None:
            raise RuntimeError("The MCP session is not running.")
        async with self._lock:
            result = await self._client.call_tool(name, arguments)
        text = "\n".join(getattr(block, "text", "") for block in result.content).strip()
        return bool(result.is_error), text, result.structured_content


class ToolRunner:
    """Runs tools for the UI: events for every step, and the approval gate."""

    def __init__(self, session: McpSession) -> None:
        self.session = session

    async def execute(
        self, name: str, arguments: Any, *, source: Literal["agent", "user"], emit: Emit, approve: Approve
    ) -> ToolOutcome:
        call_id = uuid.uuid4().hex[:12]
        tool = self.session.tools.get(name)
        access: Access = tool.access if tool else "read"
        await emit({"type": "tool_started", "call_id": call_id, "name": name, "arguments": arguments,
                    "access": access, "source": source})  # fmt: skip

        if tool is None:
            return await self._finish(emit, call_id, name, "error", f"Unknown tool '{name}'.", None, 0)
        if not isinstance(arguments, dict):
            return await self._finish(emit, call_id, name, "error", "Tool arguments must be a JSON object.", None, 0)
        if requires_approval(tool, arguments):
            await emit({"type": "approval_required", "call_id": call_id, "name": name, "arguments": arguments,
                        "access": access})  # fmt: skip
            if not await approve(call_id):
                return await self._finish(emit, call_id, name, "denied", "The user denied this action.", None, 0)

        started = time.monotonic()
        try:
            is_error, text, structured = await self.session.call(name, arguments)
        except Exception as exc:  # the server process died or the transport failed
            is_error, text, structured = True, f"The DevPilot server could not run the tool: {type(exc).__name__}.", None
        duration_ms = int((time.monotonic() - started) * 1000)
        status: Status = ("blocked" if is_security_block(text) else "error") if is_error else "ok"
        return await self._finish(emit, call_id, name, status, text, structured, duration_ms)

    @staticmethod
    async def _finish(
        emit: Emit, call_id: str, name: str, status: Status, text: str, structured: dict[str, Any] | None, duration_ms: int
    ) -> ToolOutcome:
        await emit({"type": "tool_finished", "call_id": call_id, "name": name, "status": status,
                    "duration_ms": duration_ms, "text": text[:MAX_UI_RESULT_CHARS],
                    "structured": structured if len(text) <= MAX_UI_RESULT_CHARS else None})  # fmt: skip
        return ToolOutcome(call_id, name, status, text, structured, duration_ms)
