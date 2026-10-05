"""The DevPilot Studio web server (Starlette): status API, chat/tool WebSocket, built frontend.

Safety: the server listens on 127.0.0.1 only; requests must carry a localhost
Host header (DNS-rebinding protection) and the WebSocket must come from the
UI's own origin, so other websites cannot drive it. Tools that write files or
run code wait for an explicit approval message for that exact call.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from devpilot_ui.agent import Agent
from devpilot_ui.config import UISettings
from devpilot_ui.mcp_session import MAX_UI_RESULT_CHARS, McpSession, ToolRunner, is_security_block, stdio_server

STATIC_DIR = Path(__file__).parent / "static"
MAX_MESSAGE_BYTES = 256_000  # a question or tool call from the browser; patches can be up to 256 KB

_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>DevPilot Studio</title>
<body style="font-family:system-ui;background:#0b1020;color:#e2e8f0;display:grid;place-items:center;height:100vh">
<div style="max-width:560px"><h1>DevPilot Studio</h1><p>The web interface has not been built yet.</p>
<pre style="background:#111a33;padding:12px;border-radius:8px">cd ui
npm install
npm run build</pre><p>Then restart <code>devpilot-ui</code>. The API is running.</p></div>"""


def _version() -> str:
    try:
        return importlib.metadata.version("devpilot-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class _Connection:
    """One browser tab: its conversation, its running task and its pending approvals."""

    def __init__(self, websocket: WebSocket, runner: ToolRunner, agent: Agent | None) -> None:
        self.websocket = websocket
        self.runner = runner
        self.agent = agent
        self.task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.queries: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()

    async def emit(self, event: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(event)

    async def approve(self, call_id: str) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        try:
            return await future
        finally:
            self.pending.pop(call_id, None)

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "approval":
            future = self.pending.get(str(message.get("call_id")))
            if future is not None and not future.done():
                future.set_result(message.get("approved") is True)
            return
        if kind == "query":
            # Silent read-only lookups (file tree, file viewer, current diff): no timeline events, never "busy".
            task = asyncio.create_task(self._query(message))
            self.queries.add(task)
            task.add_done_callback(self.queries.discard)
            return
        if kind == "reset":
            if self.agent is not None and not self.busy():
                self.agent.reset()
            await self.emit({"type": "reset_done"})
            return
        if kind not in ("ask", "run_tool"):
            await self.emit({"type": "error", "message": "Unknown request.", "refused": True})
            return
        if self.busy():
            await self.emit({"type": "error", "message": "DevPilot is still working on the previous request.", "refused": True})
            return
        if kind == "ask":
            question = str(message.get("text") or "").strip()
            if not question:
                return
            if self.agent is None:
                await self.emit({"type": "error", "message": "AI chat is not configured. Set AIPIPE_TOKEN and restart "
                                 "devpilot-ui. Quick actions work without it.", "refused": True})  # fmt: skip
                return
            self.task = asyncio.create_task(self.agent.ask(question, self.emit, self.approve))
        else:
            name, arguments = str(message.get("name") or ""), message.get("arguments") or {}
            self.task = asyncio.create_task(self._run_tool(name, arguments))

    async def _query(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id") or "")[:64]
        name, arguments = str(message.get("name") or ""), message.get("arguments") or {}
        tool = self.runner.session.tools.get(name)
        if tool is None or tool.access != "read" or not isinstance(arguments, dict):
            # Anything that can write files or run code must go through run_tool and its approval gate.
            await self.emit({"type": "query_result", "request_id": request_id, "status": "refused",
                             "text": "Only read-only tools can be queried.", "structured": None})  # fmt: skip
            return
        try:
            is_error, text, structured = await self.runner.session.call(name, arguments)
        except Exception as exc:  # the server process died or the transport failed
            is_error, text, structured = True, f"The DevPilot server could not run the tool: {type(exc).__name__}.", None
        status = ("blocked" if is_security_block(text) else "error") if is_error else "ok"
        await self.emit({"type": "query_result", "request_id": request_id, "status": status,
                         "text": text[:MAX_UI_RESULT_CHARS],
                         "structured": structured if len(text) <= MAX_UI_RESULT_CHARS else None})  # fmt: skip

    async def _run_tool(self, name: str, arguments: Any) -> None:
        await self.runner.execute(name, arguments, source="user", emit=self.emit, approve=self.approve)
        await self.emit({"type": "action_done"})

    async def close(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_result(False)  # a closed tab never approves anything
        if self.task is not None and not self.task.done():
            self.task.cancel()
        for task in list(self.queries):
            task.cancel()


def create_app(
    settings: UISettings,
    *,
    server: Any = None,
    llm_client: Any = None,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    """Build the app. `server` and `llm_client` are injectable for tests (defaults: stdio + AI Pipe)."""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with McpSession(server if server is not None else stdio_server(settings)) as session:
            app.state.runner = ToolRunner(session)
            client = llm_client
            if client is None and settings.ai_configured:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
            app.state.llm_client = client
            yield

    async def status(request: Request) -> JSONResponse:
        runner: ToolRunner = request.app.state.runner
        is_error, text, structured = await runner.session.call("git_status", {})
        git = (
            {"available": False, "reason": text.split(": ", 1)[-1]}
            if is_error or not structured
            else {"available": True, "branch": structured.get("branch"), "clean": structured.get("clean"),
                  "counts": structured.get("counts"), "head": (structured.get("head_commit") or "")[:7]}
        )  # fmt: skip
        tools = [
            {"name": t.name, "summary": t.summary, "description": t.description, "access": t.access,
             "input_schema": t.input_schema}
            for t in runner.session.tools.values()
        ]  # fmt: skip
        return JSONResponse({
            "version": _version(),
            "workspace": {"name": settings.workspace_root.name, "path": str(settings.workspace_root)},
            "git": git,
            "tools": tools,
            "ai": {"configured": settings.ai_configured, "model": settings.model,
                   "provider": urlparse(settings.base_url).hostname},
        })  # fmt: skip

    async def chat(websocket: WebSocket) -> None:
        if websocket.headers.get("origin") not in settings.allowed_origins:
            await websocket.close(code=1008)  # policy violation: not the DevPilot Studio page
            return
        await websocket.accept()
        state = websocket.app.state
        agent = Agent(state.llm_client, settings.model, state.runner) if state.llm_client is not None else None
        connection = _Connection(websocket, state.runner, agent)
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > MAX_MESSAGE_BYTES:
                    await connection.emit({"type": "error", "message": "Message too large.", "refused": True})
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    message = None
                if isinstance(message, dict):
                    await connection.handle(message)
        except WebSocketDisconnect:
            pass
        finally:
            await connection.close()

    async def not_built(request: Request) -> Response:
        return HTMLResponse(_NOT_BUILT)

    routes: list[Any] = [Route("/api/status", status), WebSocketRoute("/ws", chat)]
    if (STATIC_DIR / "index.html").is_file():
        routes.append(Mount("/", StaticFiles(directory=STATIC_DIR, html=True)))
    else:
        routes.append(Route("/", not_built))

    hosts = allowed_hosts or ["127.0.0.1", "localhost"]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=hosts)])
