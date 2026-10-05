"""DevPilot Studio: a local web UI that drives the DevPilot MCP server through an AI model.

The UI backend is an MCP host: it starts the DevPilot server over stdio and uses
only MCP tool calls, so the server's security boundary is unchanged. Install
with `pip install "devpilot-mcp[ui]"` and start with `devpilot-ui`.
"""
