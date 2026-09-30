# DevPilot MCP

[![Tests](https://github.com/KD-kaustubh/devpilot-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/KD-kaustubh/devpilot-mcp/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/LICENSE)

An [MCP](https://modelcontextprotocol.io) server that gives an AI model structured, security-conscious access to one software repository. It can read and search code, describe the repository, inspect Git and GitHub, gather evidence for developer questions, apply patches **you** supply (and revert them), and run the repository's Python tests.

DevPilot collects evidence and performs tightly bounded actions. The AI model does the reasoning.

```
AI model (e.g. Gemini) ──► MCP client ──stdio──► DevPilot MCP server ──► your repository
```

## Features

- **Explore:** list, read and search files; source-only code search.
- **Understand:** languages, structure, dependency manifests, tests, entry points and frameworks.
- **Git and GitHub:** status, log, diff and branches; repository, issues and pull requests (all read-only).
- **Investigate:** ranked evidence for questions like *"Where is authentication implemented?"*
- **Patch:** apply an explicit unified diff atomically, and revert it.
- **Test:** detect pytest/unittest, run tests with a fixed command, and produce a validation report.

## Tools

| Tool | What it does | Access |
|------|--------------|--------|
| `list_directory` | List a directory | Read-only |
| `read_file` | Read a text file | Read-only |
| `search_files` | Search text in all files, optionally in one folder | Read-only |
| `search_code` | Smart-case search in source files | Read-only |
| `analyze_repository` | Languages, structure, manifests, tests, entry points | Read-only |
| `git_status` | Branch, staged, unstaged and untracked files | Read-only |
| `git_log` | Recent commits | Read-only |
| `git_diff` | Staged or unstaged changes | Read-only |
| `git_branch` | Local branches | Read-only |
| `github_repository` | GitHub metadata for `origin` | Read-only |
| `github_issues` | Recent issues | Read-only |
| `github_pull_requests` | Recent pull requests | Read-only |
| `investigate_repository` | Ranked evidence for a developer question | Read-only |
| `apply_patch` | Apply a unified diff you supply, atomically | Writes files |
| `revert_patch` | Undo an applied patch | Writes files |
| `get_test_commands` | Detect pytest/unittest (runs nothing) | Read-only |
| `run_tests` | Run the detected test command | Executes code |
| `validate_repository` | Validation report; tests only on request | Executes code |

## Quick Start

Requires Python 3.10+ (3.11+ recommended) and Git.

**Install from PyPI:**

```powershell
pip install devpilot-mcp
```

When installed this way, always set `DEVPILOT_WORKSPACE` to the **absolute** path of the repository DevPilot should work on, for example in your MCP client's server settings.

**Or clone the repository** (includes the tests and a sample workspace):

```powershell
git clone https://github.com/KD-kaustubh/devpilot-mcp.git
cd devpilot-mcp
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -e .
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

In `.env`, set `DEVPILOT_WORKSPACE` to the repository DevPilot should work on. The default is the bundled `./workspace` sample, which is not a Git repository. `GITHUB_TOKEN` is optional.

## Try It

**1. Run the test suite**

```powershell
python -m unittest discover -s tests -t .
```

**2. Explore it with MCP Inspector** (needs Node.js)

```powershell
npx @modelcontextprotocol/inspector
```

Set **Transport** to `STDIO` and **Command** to the full path of `.venv\Scripts\devpilot-mcp.exe`, then click **Connect** and **List Tools**. Try `search_code` with `query` = `format_price`.

**3. Connect an AI client.** Register DevPilot as a stdio server. For example, with Claude Code:

```powershell
claude mcp add devpilot -e DEVPILOT_WORKSPACE=D:/path/to/repo -- D:/path/to/devpilot-mcp/.venv/Scripts/devpilot-mcp.exe
```

Then ask: *"Use DevPilot to analyze this repository and explain its structure."* More in the [usage guide](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/USAGE.md).

## Security at a Glance

- Every path stays inside the workspace. `..`, absolute, drive and UNC paths, and escaping symlinks and junctions are rejected.
- Only two places start processes: allowlisted read-only Git commands, and two fixed Python test commands. There is no shell and no caller-supplied command.
- `.env` files, private keys, credential files and `.git` internals are never read, searched or written.
- Patches are validated in full and applied atomically.
- GitHub access is HTTPS GET to `api.github.com` only, and the token never appears in output.
- Every tool is annotated as read-only, writes-files or executes-code, so clients can ask before risky calls.

**It is not a sandbox.** Tests run with DevPilot's own permissions and network access, and secret redaction is best-effort. See [Security](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/SECURITY.md) for the full model and known limitations.

## Documentation

| Document | Contents |
|----------|----------|
| [Usage guide](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/USAGE.md) | Configuration, MCP Inspector, client setup, example workflows |
| [Tool reference](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/TOOLS.md) | Every tool's inputs, output examples and limits |
| [Security](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/SECURITY.md) | Security model, known limitations, security tests |
| [Development](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/docs/DEVELOPMENT.md) | Architecture, running tests, CI, phase history |

## License

[MIT](https://github.com/KD-kaustubh/devpilot-mcp/blob/main/LICENSE)
