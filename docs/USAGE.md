# Usage Guide

How to install, configure, run and try DevPilot MCP.

## Quick Start

Requires **Python 3.10+** (3.11+ recommended: TOML manifests are only parsed on 3.11+) and **Git** on `PATH` for the Git and GitHub tools. From a terminal:

```powershell
git clone https://github.com/KD-kaustubh/devpilot-mcp.git
cd devpilot-mcp
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -e .
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

### Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `GITHUB_TOKEN` | *(unset)* | Optional fine-grained token with read-only Metadata, Issues and Pull requests permissions, for private repositories or a higher rate limit. Keep it in `.env` (git-ignored) or the environment; never commit it. |
| `DEVPILOT_TEST_PYTHON` | *(unset)* | Optional **absolute** path to the Python interpreter `run_tests` uses, e.g. a project's own venv `python.exe`. Default: the interpreter running DevPilot. Set only by the server operator; no tool argument can change it. |
| `DEVPILOT_WORKSPACE` | `./workspace` | The directory the tools can access. A relative path is resolved against the **project root**, not the current directory, so the server behaves the same whichever directory a client launches it from. |

If the directory doesn't exist, the server exits at startup with an error on stderr.

### Choosing the workspace

- The default `./workspace` contains a small `sample_project` to explore with the file, search and analysis tools. It is a plain folder, so the Git and GitHub tools report *"The workspace is not a Git repository"* there.
- To use every tool, set `DEVPILOT_WORKSPACE` to the **root** of a Git repository, for example this project's own directory. The GitHub tools also need an `origin` remote on github.com.
- Point `apply_patch` at a disposable copy of a repository while experimenting, and use `run_tests` only on repositories whose test code you are willing to execute.

### Starting the server

The server uses the stdio transport, so an MCP client normally starts it. To start it by hand:

```powershell
devpilot-mcp
# or
python -m devpilot_mcp
```

It then waits silently for MCP messages on stdin. Press Ctrl+C to stop it.

## MCP Inspector

Requires Node.js. Run these commands from the project root with the virtual environment activated.

> **Each separate Inspector CLI invocation starts a new DevPilot server process.** The `revert_patch` registry lives in that process's memory, so a `change_id` returned by one CLI call cannot be reverted by another. Use the web UI, which keeps one server session, to apply and revert.

### Inspector web UI

```powershell
npx @modelcontextprotocol/inspector
```

In the page that opens:

1. Set **Transport Type** to `STDIO`.
2. Set **Command** to the full path of `.venv\Scripts\devpilot-mcp.exe`. On macOS/Linux use `.venv/bin/devpilot-mcp`. Leave **Arguments** empty.
3. Optionally, add `DEVPILOT_WORKSPACE` under **Environment Variables** to point the server at a different directory.
4. Click **Connect**, open the **Tools** tab, then click **List Tools**. The eighteen tools should appear.
5. Try these calls:
   - `list_directory` with `path` = `sample_project`
   - `read_file` with `path` = `sample_project/src/inventory.py`
   - `search_files` with `query` = `TODO`, then again with `path` = `sample_project/src` to limit the search to that folder. `path` = `../..` is rejected.
   - `search_code` with `query` = `format_price`. Expect 3 matches in `src/inventory.py` and `src/utils.py`.
   - `search_code` with `query` = `def` and `path` = `sample_project/src`. The search is limited to that folder.
   - `search_code` with `query` = `Inventory`. The search is case-sensitive, so the lowercase variable `inventory` doesn't match.
   - `search_code` with `query` = `no_such_symbol`. Expect an empty `matches` list.
   - `search_code` with `query` = `x` and `path` = `../../secret`. The call should be rejected.
   - `read_file` with `path` = `../../secret.txt`. The call should be rejected with *"Path escapes the workspace root"*.
   - `git_status`, `git_log`, `git_diff` and `git_branch`. Against the default `./workspace` these return *"The workspace is not a Git repository"*, because `./workspace` is a plain folder. Set `DEVPILOT_WORKSPACE` to a repository root, e.g. this project's directory, and reconnect. Then try `git_log` with `limit` = `3`, and `git_diff` with and without `staged` = `true` after editing or staging a file.
   - `investigate_repository` with `query` = `Where is GitHub integration implemented?`. The result is evidence (ranked files with reasons, matching lines, excerpts, Git and GitHub context), not an answer. Try also `How is this project structured?`, `How are MCP tools registered?` and `How is workspace security implemented?`. A whitespace-only query is rejected.
   - `apply_patch`, **against a disposable copy of a repository**. Paste a small unified diff, apply it, and check the file with `read_file` and `git_status`. Then call `revert_patch` with the returned `change_id` in the same session, and confirm the original content is back. Also try a stale patch (apply the same one twice) and a patch for `.env`; both are rejected and nothing changes.
   - `get_test_commands`. Nothing is run. Then `validate_repository` with the defaults: `test_execution` is `skipped`. Then, **against a repository whose tests you are willing to execute**, `run_tests` with `timeout_seconds` = `60`, and `validate_repository` with `run_tests` = `true`. Try `framework` = `; whoami`; the call is rejected.
   - `github_repository`, `github_issues` with `limit` = `5`, and `github_pull_requests` with `limit` = `5`. Like the Git tools, these need `DEVPILOT_WORKSPACE` to be a repository root whose `origin` points to github.com, e.g. this project's directory. A repository with no issues or pull requests returns empty lists. Also try `state` = `merged` or `limit` = `51`; both are rejected.
   - `analyze_repository` with no arguments. Against the sample project, expect 4 files (`Python: 3`, `Markdown: 1`), the test directory `sample_project/tests` and empty `heuristics`, because the sample has no manifests. To analyze a real repository, set `DEVPILOT_WORKSPACE` in step 3 to that repository's path and reconnect.

### Inspector CLI (scriptable)

```powershell
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/list
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_files --tool-arg query=TODO
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_files --tool-arg query=TODO path=sample_project/src
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=class
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=def path=sample_project/src
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name read_file --tool-arg path=../../secret.txt
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name analyze_repository
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/other-repo --method tools/call --tool-name analyze_repository
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_status
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_log --tool-arg limit=3
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_diff --tool-arg staged=true
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-github-clone --method tools/call --tool-name github_repository
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-github-clone --method tools/call --tool-name github_issues --tool-arg state=all limit=5
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-github-clone --method tools/call --tool-name github_pull_requests --tool-arg limit=5
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-repo --method tools/call --tool-name investigate_repository --tool-arg "query=How is authentication implemented?"
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-disposable-copy --method tools/call --tool-name apply_patch --tool-arg "patch=\"--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Old title\n+# New title\n\""
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-repo --method tools/call --tool-name get_test_commands
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-repo --method tools/call --tool-name run_tests --tool-arg timeout_seconds=60
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-repo --method tools/call --tool-name validate_repository --tool-arg run_tests=true
```

The Inspector CLI mangles raw multi-line values, so pass a patch as a **JSON-encoded string** (with `\n` escapes), as above. Each Inspector CLI call starts a new server process with an empty patch registry, so `revert_patch` from a separate CLI call finds no change. Use the web UI, or any client that keeps one session, to apply and revert.

Launch the server through the `devpilot-mcp` executable rather than `python -m devpilot_mcp`. The Inspector CLI parses flags such as `-m` and `-e` itself, so they never reach the server.

## Gemini / MCP Client Usage

DevPilot is a standard MCP server on the **stdio** transport. An MCP client, the component that connects an AI model such as Gemini to MCP servers, starts DevPilot as a local process and talks to it over stdin/stdout. DevPilot itself never calls a model.

To register DevPilot with a client that can launch local stdio servers, give it:

| Setting | Value |
|---------|-------|
| Command | the absolute path of `.venv\Scripts\devpilot-mcp.exe` (macOS/Linux: `.venv/bin/devpilot-mcp`) |
| Arguments | none |
| Environment | `DEVPILOT_WORKSPACE` (the repository to expose), and optionally `GITHUB_TOKEN` and `DEVPILOT_TEST_PYTHON`. Values in the project's `.env` are used when the client does not set them. |

For example, with Claude Code:

```powershell
claude mcp add devpilot -e DEVPILOT_WORKSPACE=D:/path/to/repo -- D:/path/to/devpilot-mcp/.venv/Scripts/devpilot-mcp.exe
```

For other clients, where these values go depends on the client; consult its documentation for the configuration format. No client-specific configuration files are shipped. DevPilot has been exercised through MCP Inspector (web UI and CLI), over stdio, and through the MCP Python SDK client in the test suite.

Once connected, the client lists the 18 tools with their descriptions, input and output schemas and annotations. A client that honours annotations can ask for confirmation before the **Writes files** and **Executes code** tools run. As with the Inspector, the `revert_patch` registry lives in the server process, so a client must keep one server session to revert a change.

## Developer Workflows

These are typical tool sequences. The AI model (or a person) chooses each call and interprets the results; DevPilot never chains tools or makes changes on its own.

### 1. Understand an unfamiliar repository

1. `analyze_repository`: languages, top directories, manifests, tests, possible entry points and frameworks.
2. `list_directory` and `read_file` on the entry points and documentation it lists.
3. `search_code` for key names, e.g. `query="register"` with `path="src"`.
4. `git_log` and `git_status` for recent activity and uncommitted work.

### 2. Investigate an issue

1. `github_issues` (or `github_pull_requests`) to see what has been reported.
2. `investigate_repository` with a concrete question, e.g. *"Where is CSV export implemented?"*. It returns ranked files with reasons, matching lines, excerpts and related commits, issues and pull requests.
3. `read_file` and `search_code` on the top-ranked files; `git_diff` if relevant files have uncommitted changes.
4. The model explains the cause from that evidence.

### 3. Apply and revert an explicit patch

1. Read the target files with `read_file`, so the diff is written against the current content.
2. The model or the person writes a unified diff. DevPilot does not write or alter it.
3. `apply_patch` validates the whole diff and applies it atomically, returning a `change_id`.
4. Check the result with `read_file`, `git_diff` and, if wanted, `run_tests`.
5. If the change is not wanted, `revert_patch(change_id)` in the same server session restores the exact previous bytes. Nothing is committed; the change stays in the working tree for the user to review.

### 4. Validate a repository

1. `get_test_commands`: which framework was detected and the fixed command, without running anything.
2. `validate_repository()`: repository facts, Git state, test discovery and Python syntax. No code is executed.
3. Only for a repository whose tests you are willing to run: `validate_repository(run_tests=true)` or `run_tests`, then review `failures`, `warnings` and `side_effects`.
