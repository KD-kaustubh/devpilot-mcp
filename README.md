# DevPilot MCP

DevPilot MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI model explore and understand a software repository.

It is being built in phases:

- **Phase 1: read-only filesystem access.** List, read and search files.
- **Phase 2 (this release): code search.** `search_code` searches source files only and can be limited to a subdirectory or file.

The server never touches anything outside one configured workspace directory.

## Tools

| Tool | Input | Returns |
|------|-------|---------|
| `list_directory` | `path` (default `"."`; `""` also means the workspace root) | The directory's entries (`name`, `path`, `type`, `size_bytes`), directories first. Capped at 500 entries, with a `truncated` flag. |
| `read_file` | `path` | `content`, `size_bytes`, `line_count`. Rejects binary files, non-UTF-8 files and files over 1 MB. |
| `search_files` | `query` | Case-insensitive substring matches (`path`, `line_number`, `line`), plus `files_with_matches` and `files_searched`. Skips binary files, files over 1 MB and dependency/VCS folders (`.git`, `node_modules`, `.venv`, …). Capped at 100 matches. |
| `search_code` | `query`, optional `path` (default `"."`; a directory or a single source file) | `matches` as `{file, line, text}`, plus `path`, `case_sensitive`, `files_searched`, `files_skipped` and `truncated`. See [Code search](#code-search). |

Every tool returns **structured output**, and its JSON schema is published to the client. Every tool is also marked `readOnlyHint: true`. An expected failure, such as a missing file or a rejected path, comes back as a tool error (`isError: true`) with a clear message instead of crashing the server.

## Code search

`search_code` is the developer-oriented search. It differs from `search_files` in four ways:

- **Source files only.** Files are recognized by extension (`.py`, `.ts`, `.java`, `.go`, `.rs`, `.c`, `.sql`, `.json`, `.yaml`, `.toml`, …) or by well-known name (`Dockerfile`, `Makefile`, …). Prose such as `.md` and `.txt` is skipped; use `search_files` for that. Minified bundles (`*.min.js`, `*.min.css`) and `package-lock.json` are skipped too.
- **Generated directories are skipped.** On top of the VCS and dependency folders that `search_files` skips, it prunes `build`, `dist`, `target`, `.next`, `coverage`, `*.egg-info` and similar. The skip list applies only below the search path, so an explicit `path="node_modules/some-lib"` still works.
- **Smart case.** A query in lowercase matches regardless of case. A query containing any uppercase letter is matched case-sensitively, so `Inventory` finds the class but not the variable `inventory`. The result's `case_sensitive` field says which mode was used.
- **Scoped search.** `path` limits the search to a directory or one file. The returned `file` paths stay relative to the workspace root, so they can go straight into `read_file`.

Example result for `search_code("format_price")` against the sample project:

```json
{
  "query": "format_price",
  "path": ".",
  "case_sensitive": false,
  "matches": [
    {"file": "sample_project/src/inventory.py", "line": 3, "text": "from utils import format_price"},
    {"file": "sample_project/src/inventory.py", "line": 26, "text": "f\"{name}: {qty} @ {format_price(price)}\" for name, (qty, price) in sorted(self._items.items())"},
    {"file": "sample_project/src/utils.py", "line": 4, "text": "def format_price(value: float) -> str:"}
  ],
  "files_searched": 3,
  "files_skipped": 0,
  "truncated": false
}
```

**Limits** are constants at the top of `devpilot_mcp/tools/code_search.py`:

- `MAX_CODE_MATCHES = 100`: when more matches exist, the search stops and `truncated` is `true`.
- `MAX_CODE_FILE_BYTES = 512_000`: larger files are usually generated, so they are skipped.

Oversized, binary (a NUL byte in the first 8 KB), non-UTF-8 and unreadable files are counted in `files_skipped` instead of causing an error. A query with no matches returns an empty `matches` list. An empty query, a missing path, a non-source file path, or a path outside the workspace returns a tool error.

## Security model

All paths are relative to the workspace root. `Workspace.resolve()` in `devpilot_mcp/workspace.py` is the only way a tool turns a path into a filesystem location. It:

1. rejects null bytes, absolute paths (`/etc/passwd`), drive-letter paths (`C:\Users\…`, `D:\other-project\…`, `c:foo`) and UNC paths (`\\server\share`);
2. joins the path to the root and calls `resolve()`, which collapses `..` and follows symlinks;
3. checks that the result is still inside the root. If it isn't, the request is rejected (`../../secret.txt` fails here).

A symlink inside the workspace that points outside it is hidden from `list_directory`, skipped by `search_files` and rejected by `read_file`. Error messages never include absolute host paths.

## Project structure

```
DevPilot-MCP/
├── devpilot_mcp/
│   ├── __main__.py        # enables `python -m devpilot_mcp`
│   ├── server.py          # builds the MCPServer, registers tool groups, stdio entry point
│   ├── config.py          # loads DEVPILOT_WORKSPACE from the environment / .env
│   ├── workspace.py       # path sandboxing (the security boundary)
│   ├── text_search.py     # shared file walking, text detection and line matching
│   └── tools/
│       ├── common.py      # shared MCP helpers (read-only annotations, error mapping)
│       ├── filesystem.py  # list_directory, read_file, search_files
│       └── code_search.py # search_code
├── tests/                 # unittest suite (sandboxing, tool logic, in-process MCP client)
├── workspace/
│   └── sample_project/    # small demo repo to explore with the tools
├── .env.example
└── pyproject.toml
```

To add a tool group in a later phase, create a new module in `devpilot_mcp/tools/` with a `register(server, workspace)` function, then call it from `create_server()` in `server.py`.

## Setup

Requires Python 3.10+. From the project root:

```powershell
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -e .
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

### Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DEVPILOT_WORKSPACE` | `./workspace` | The directory the tools can access. A relative path is resolved against the **project root**, not the current directory, so the server behaves the same whichever directory a client launches it from. |

If the directory doesn't exist, the server exits at startup with an error on stderr.

## Running the server

The server uses the stdio transport, so an MCP client normally starts it. To start it by hand:

```powershell
devpilot-mcp
# or
python -m devpilot_mcp
```

It then waits silently for MCP messages on stdin. Press Ctrl+C to stop it.

## Testing with MCP Inspector

Requires Node.js. Run these commands from the project root with the virtual environment activated.

### Inspector web UI

```powershell
npx @modelcontextprotocol/inspector
```

In the page that opens:

1. Set **Transport Type** to `STDIO`.
2. Set **Command** to the full path of `.venv\Scripts\devpilot-mcp.exe`. On macOS/Linux use `.venv/bin/devpilot-mcp`. Leave **Arguments** empty.
3. Optionally, add `DEVPILOT_WORKSPACE` under **Environment Variables** to point the server at a different directory.
4. Click **Connect**, open the **Tools** tab, then click **List Tools**. The four tools should appear.
5. Try these calls:
   - `list_directory` with `path` = `sample_project`
   - `read_file` with `path` = `sample_project/src/inventory.py`
   - `search_files` with `query` = `TODO`
   - `search_code` with `query` = `format_price`. Expect 3 matches in `src/inventory.py` and `src/utils.py`.
   - `search_code` with `query` = `def` and `path` = `sample_project/src`. The search is limited to that folder.
   - `search_code` with `query` = `Inventory`. The search is case-sensitive, so the lowercase variable `inventory` doesn't match.
   - `search_code` with `query` = `no_such_symbol`. Expect an empty `matches` list.
   - `search_code` with `query` = `x` and `path` = `../../secret`. The call should be rejected.
   - `read_file` with `path` = `../../secret.txt`. The call should be rejected with *"Path escapes the workspace root"*.

### Inspector CLI (scriptable)

```powershell
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/list
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_files --tool-arg query=TODO
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=class
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=def path=sample_project/src
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name read_file --tool-arg path=../../secret.txt
```

Launch the server through the `devpilot-mcp` executable rather than `python -m devpilot_mcp`. The Inspector CLI parses flags such as `-m` and `-e` itself, so they never reach the server.

## Running the tests

```powershell
python -m unittest discover -s tests -t . -v
```

The suite builds a temporary workspace with a `secret.txt` just outside it. It checks the path-escape attempts listed under [Security model](#security-model), every tool's normal and error cases, and full round trips through an in-process MCP client. On Windows the symlink-escape test is skipped unless Developer Mode is on, because creating symlinks requires it.

## Roadmap

Phase 1 added read-only filesystem access and Phase 2 added code search. Later phases will be designed separately.
