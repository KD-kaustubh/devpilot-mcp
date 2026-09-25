# DevPilot MCP

DevPilot MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI model explore and understand a software repository.

It is being built in phases:

- **Phase 1: read-only filesystem access.** List, read and search files.
- **Phase 2: code search.** `search_code` searches source files only and can be limited to a subdirectory or file.
- **Phase 3: repository understanding.** `analyze_repository` returns a deterministic, factual overview of the repository.
- **Phase 4 (this release): Git intelligence.** `git_status`, `git_log`, `git_diff` and `git_branch` give read-only, structured Git information.

The server never touches anything outside one configured workspace directory.

## Tools

| Tool | Input | Returns |
|------|-------|---------|
| `list_directory` | `path` (default `"."`; `""` also means the workspace root) | The directory's entries (`name`, `path`, `type`, `size_bytes`), directories first. Capped at 500 entries, with a `truncated` flag. |
| `read_file` | `path` | `content`, `size_bytes`, `line_count`. Rejects binary files, non-UTF-8 files and files over 1 MB. |
| `search_files` | `query` | Case-insensitive substring matches (`path`, `line_number`, `line`), plus `files_with_matches` and `files_searched`. Skips binary files, files over 1 MB and dependency/VCS folders (`.git`, `node_modules`, `.venv`, …). Capped at 100 matches. |
| `search_code` | `query`, optional `path` (default `"."`; a directory or a single source file) | `matches` as `{file, line, text}`, plus `path`, `case_sensitive`, `files_searched`, `files_skipped` and `truncated`. See [Code search](#code-search). |
| `analyze_repository` | none | Language counts, top directories, documentation, configuration, dependency manifests, lock files and tests. Possible entry points and framework indicators are kept separately under `heuristics`. See [Repository analysis](#repository-analysis). |
| `git_status` | none | Branch, HEAD commit, upstream and ahead/behind counts, `clean`, and sorted `staged`, `unstaged`, `untracked`, `deleted` and `conflicted` lists. See [Git tools](#git-tools). |
| `git_log` | `limit` (1–50, default 10) | Recent commits, newest first: hashes, author, date, parents, subject and body, plus `has_more`. |
| `git_diff` | `staged` (default `false`), `path` (default `"."`) | Changed files with change type and line counts, plus unified diff text capped at 60,000 bytes (`truncated`, `warnings`). |
| `git_branch` | none | The current branch (`detached` flag) and the local branches with short commit and upstream. |

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

## Repository analysis

`analyze_repository()` collects **objective facts** about the whole workspace so an AI client can orient itself before reading code. It works only from file names, paths and the fields declared in dependency manifests. It never executes repository code, package managers or tests, and it never writes anything.

Output from the scratch full-stack repository used to test this phase (FastAPI backend, React/Express frontend), shortened:

```json
{
  "total_files": 25,
  "scan_complete": true,
  "languages": {"JSON": 5, "Python": 5, "TypeScript": 4, "Markdown": 2, "YAML": 2, "Dockerfile": 1, "TOML": 1, "Text": 1},
  "unclassified_files": 4,
  "directories": [
    {"path": "backend", "file_count": 10},
    {"path": "frontend", "file_count": 8},
    {"path": "backend/app", "file_count": 3}
  ],
  "documentation_files": ["LICENSE", "README.md", "docs/architecture.md"],
  "configuration_files": [".gitignore", "docker-compose.yml", "backend/Dockerfile", "backend/pyproject.toml", "frontend/tsconfig.json", ".github/workflows/ci.yml"],
  "dependency_manifests": ["backend/pyproject.toml", "backend/requirements.txt", "frontend/package.json"],
  "lock_files": ["backend/uv.lock", "frontend/package-lock.json"],
  "tests": {
    "directories": ["backend/tests"],
    "files": ["backend/tests/test_orders.py", "frontend/src/components/Cart.test.tsx"],
    "file_count": 2
  },
  "heuristics": {
    "possible_entry_points": [
      {"file": "backend/pyproject.toml", "kind": "manifest", "detail": "[project.scripts] shop-api = app.main:run"},
      {"file": "frontend/package.json", "kind": "manifest", "detail": "\"main\": \"src/index.tsx\""},
      {"file": "backend/app/main.py", "kind": "filename", "detail": "conventional entry-point filename 'main.py'"}
    ],
    "framework_indicators": [
      {"name": "FastAPI", "file": "backend/pyproject.toml", "evidence": "declares dependency 'fastapi'"},
      {"name": "React", "file": "frontend/package.json", "evidence": "declares dependency 'react'"}
    ]
  },
  "truncated_fields": [],
  "warnings": []
}
```

### Facts

These are derived purely from paths:

- **`languages`**: file counts by language. The language comes from the file extension (case-insensitive, e.g. `.py` → Python, `.cpp`/`.cc`/`.cxx` → C++, `.yml` → YAML) or from well-known names (`Dockerfile`, `Makefile`). It is ordered by count, then by name. Files with no recognized language are counted in `unclassified_files`.
- **`directories`**: directories up to 2 levels deep with their recursive file counts, shallowest first. A directory is listed only if it contains at least one counted file.
- **`documentation_files`**: `README`, `LICENSE`, `CHANGELOG`, `CONTRIBUTING` and similar files (any doc extension or none), plus `.md`, `.rst`, `.adoc` and `.txt` files under `docs/` or `doc/`.
- **`configuration_files`**: known tool, build, container, CI and environment files, such as `pyproject.toml`, `tsconfig.json`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/*.yml`, `.gitignore` and `.env.example`. A file can be both configuration and a manifest (e.g. `pyproject.toml`).
- **`dependency_manifests`**: `pyproject.toml`, `requirements*.txt`, `setup.py`, `Pipfile`, `package.json`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `Gemfile` and others.
- **`lock_files`**: `package-lock.json`, `yarn.lock`, `poetry.lock`, `Pipfile.lock`, `uv.lock`, `Cargo.lock`, `go.sum` and others.
- **`tests`**:
  - Test directories are the outermost `tests/`, `test/`, `__tests__/` or `spec/` directories.
  - Test files match naming conventions: `test_*.py`, `*_test.py`, `*.test.js`/`.ts`/`.tsx`, `*.spec.js`/`.ts`/`.tsx`, `*_test.go`, `*Test.java`, `*_spec.rb` and similar.
  - Helpers such as `conftest.py` are not counted as test files.

### Heuristics

These are grouped separately because they are not confirmed facts:

- **`possible_entry_points`**: entry points declared in manifests come first, then conventional filenames.
  - Manifest entries come from `[project.scripts]`, `[project.gui-scripts]` and `[tool.poetry.scripts]` in `pyproject.toml`; `main`, `bin` and `scripts.start` in `package.json`; and `[[bin]]` in `Cargo.toml`.
  - Conventional filenames include `main.py`, `app.py`, `server.py`, `manage.py`, `__main__.py`, `index.js`/`.ts`/`.tsx`, `main.go` and `main.rs`. Files inside test directories are excluded.
- **`framework_indicators`**: a framework is reported **only when a manifest declares it as a dependency**. Each indicator names the manifest and the evidence.
  - Python dependencies (normalized per PEP 503) are read from `pyproject.toml` (PEP 621, PEP 735 dependency groups and Poetry), `requirements*.txt` and `Pipfile`.
  - JavaScript dependencies come from all `package.json` dependency sections, and Rust dependencies from `Cargo.toml`.
  - `go.mod`, `pom.xml` and `build.gradle` are matched on exact module coordinates, e.g. `github.com/gin-gonic/gin` or `org.springframework.boot`.
  - A file called `django.py` or a folder called `flask/` is never evidence.

### Bounds and failures

- Directories that `search_code` skips are skipped here too: VCS, dependency, cache and build output (`.git`, `.venv`, `node_modules`, `__pycache__`, `dist`, `build`, `*.egg-info`, …). Symlinks are not followed.
- At most **20,000 files** are considered. If the scan stops early, `scan_complete` is `false`.
- Every list holds at most **50 items**, shallowest paths first. `truncated_fields` names any list that was cut, and `tests.file_count` always gives the full count. A 3,000-file tree produces roughly 12 KB of output.
- At most 20 manifests (up to 512 KB each) are parsed. `setup.py` is listed but never read, because it is code.
- A manifest that is malformed, binary or unreadable becomes a `warnings` entry instead of failing the call. If the workspace root has disappeared, the call returns a tool error.
- TOML manifests are parsed with the standard-library `tomllib` (Python 3.11+). On Python 3.10 they are listed with a warning but not parsed.

## Git tools

The four Git tools describe the repository's state and recent history. They **never change anything**: there is no add, commit, checkout, reset, fetch or push, and no index or file is written.

**The workspace must be the repository root.** The tools run against `DEVPILOT_WORKSPACE` itself. If that directory is not the top level of a Git work tree, every Git tool returns *"The workspace is not a Git repository"*. Parent directories are never searched, and no repository is ever initialized. The default `./workspace` is a plain folder, so to use the Git tools, point `DEVPILOT_WORKSPACE` at a repository root, e.g. this project's own directory.

### git_status

```json
{
  "branch": "main", "detached": false,
  "head_commit": "82502ce6780975cea2a9923b8de95c9228033543",
  "upstream": "origin/main", "ahead": 0, "behind": 0,
  "clean": false,
  "staged": [{"path": "staged_module.py", "change": "added", "original_path": null}],
  "unstaged": [{"path": "devpilot_mcp/config.py", "change": "modified", "original_path": null}],
  "untracked": ["untracked_note.txt"],
  "deleted": [],
  "conflicted": [],
  "counts": {"staged": 1, "unstaged": 1, "untracked": 1, "deleted": 0, "conflicted": 0},
  "complete": true,
  "truncated_fields": []
}
```

- `staged` is the index compared to HEAD, and `unstaged` is the working tree compared to the index. The same file can appear in both.
- `change` is one of `added`, `modified`, `deleted`, `renamed` (with `original_path`), `copied`, `type_changed` or `unmerged`. `deleted` collects deletions from both lists.
- `branch` is `null` when HEAD is detached, and `head_commit` is `null` in a repository with no commits yet.
- The data comes from `git status --porcelain=v2 -z`, which is machine-readable and handles any filename, including ones with spaces and non-ASCII characters.

### git_log

`git_log(limit=2)` on this repository:

```json
{
  "limit": 2,
  "commits": [
    {
      "hash": "82502ce6780975cea2a9923b8de95c9228033543", "short_hash": "82502ce",
      "author_name": "KD-kaustubh", "author_email": "…", "date": "2026-09-25T15:36:33+05:30",
      "parents": ["e19e1eab33be024049668491db632c9d4da597f2"],
      "subject": "Implement Phase 3 repository understanding", "body": "", "body_truncated": false
    },
    {"hash": "e19e1ea…", "short_hash": "e19e1ea", "subject": "Implement Phase 2 code search", "…": "…"}
  ],
  "has_more": true
}
```

- Commits are listed newest first, following Git's own order from HEAD.
- `limit` must be an integer from 1 to 50. Anything else is rejected before Git runs.
- `has_more` says whether older history exists. Git is asked for `limit + 1` commits only to work this out, so the full history is never read or returned.

### git_diff

- `git_diff()` shows **unstaged** changes (working tree compared to the index).
- `git_diff(staged=true)` shows **staged** changes (index compared to HEAD).
- `path` narrows either one to a file or directory, which is validated by the same workspace rules as every other tool. Untracked files are not part of a diff; see `git_status`.

```json
{
  "staged": false, "path": ".",
  "files": [
    {"path": "README.md", "change": "modified", "original_path": null, "additions": 3000, "deletions": 0, "binary": false},
    {"path": "devpilot_mcp/config.py", "change": "modified", "original_path": null, "additions": 2, "deletions": 0, "binary": false}
  ],
  "files_changed": 2, "additions": 3002, "deletions": 0,
  "diff": "diff --git a/README.md b/README.md\n…",
  "diff_bytes": 59972,
  "truncated": true,
  "warnings": ["Diff text exceeds 60,000 bytes and was truncated; call git_diff with a narrower path to see the rest."]
}
```

- File statistics are always complete, even when the diff text is cut.
- Binary files have `binary: true` and `null` line counts.

### git_branch

```json
{
  "current_branch": "main", "detached": false,
  "branches": [{"name": "main", "commit": "82502ce", "upstream": "origin/main", "current": true}],
  "total_branches": 1, "truncated": false
}
```

Only local branches are listed, sorted by name. Nothing is ever created, deleted or switched.

### Bounded output

| Limit | Value |
|-------|-------|
| Commits per `git_log` call | 1–50 (default 10) |
| Commit body | 2,000 characters (`body_truncated`) |
| Diff text | 60,000 bytes. Reading stops at the cap, so a huge diff is never loaded into memory. |
| File lists in `git_status` / `git_diff` | 200 each (`counts`, `files_changed`, `truncated_fields`, `warnings`) |
| Branches | 100 (`total_branches`, `truncated`) |
| Any Git process | 15-second timeout and at most 2 MB of output read |

These values are constants at the top of `devpilot_mcp/tools/git.py`.

### Read-only execution boundary

Git runs as a subprocess, so every invocation goes through a single function, `_run_git`:

- **Allowlist.** Only `status`, `log`, `diff`, `diff-files`, `branch` and `rev-parse` can run, and each in a fixed read-only form. `diff-files` is the plumbing command used for unstaged diffs, because porcelain `git diff` rewrites `.git/index`. `rev-parse` only checks the repository. Any other subcommand, such as `add`, `commit`, `push`, `checkout`, `reset` or `config`, is refused before a process starts.
- **No command injection.** Tool input is limited to an integer (`limit`), a boolean (`staged`) and a workspace-validated `path`. The path is passed after `--` with `--literal-pathspecs`, so even `path="--output=x"` is just a filename to Git. Commands are built as argument lists and run with `shell=False`.
- **Fixed repository.** `cwd` is always the workspace root. Inherited `GIT_*` environment variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG_PARAMETERS`, …) are dropped, and `GIT_CEILING_DIRECTORIES` stops Git from climbing to a parent repository. No tool argument names a repository or directory.
- **No launched programs.** Repository config can make even read-only commands run programs. The tools disable the fsmonitor hook (`core.fsmonitor=false`), external diff drivers (`--no-ext-diff`), textconv filters (`--no-textconv`), the pager, GPG signature checks, network protocols and lazy fetches. The tests prove each blocked route is live with plain `git` and blocked in DevPilot.
- **No writes.** `GIT_OPTIONAL_LOCKS=0` stops `git status` from refreshing the index. A test snapshots every file in the repository, `.git` included, and checks that nothing changes.
- **Errors.** Git failures, a missing Git executable, timeouts and unexpected output become tool errors. Git's error message is reduced to one line with the absolute workspace path masked.

**Not covered:** clean/smudge filter drivers (`filter.<name>.clean`) configured in the repository's own `.git/config` cannot be disabled generically. Only run the Git tools on repositories whose `.git/config` you trust, as with running `git status` yourself.

## Security model

All paths are relative to the workspace root. `Workspace.resolve()` in `devpilot_mcp/workspace.py` is the only way a tool turns a path into a filesystem location. It:

1. rejects null bytes, absolute paths (`/etc/passwd`), drive-letter paths (`C:\Users\…`, `D:\other-project\…`, `c:foo`) and UNC paths (`\\server\share`);
2. joins the path to the root and calls `resolve()`, which collapses `..` and follows symlinks;
3. checks that the result is still inside the root. If it isn't, the request is rejected (`../../secret.txt` fails here).

A symlink inside the workspace that points outside it is hidden from `list_directory`, skipped by `search_files`, `search_code` and `analyze_repository`, and rejected by `read_file`. `analyze_repository` takes no path at all. It always analyzes the workspace root, and any extra arguments are dropped. Error messages never include absolute host paths.

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
│       ├── code_search.py # search_code
│       ├── repository.py  # analyze_repository
│       └── git.py         # git_status, git_log, git_diff, git_branch
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
4. Click **Connect**, open the **Tools** tab, then click **List Tools**. The nine tools should appear.
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
   - `git_status`, `git_log`, `git_diff` and `git_branch`. Against the default `./workspace` these return *"The workspace is not a Git repository"*, because `./workspace` is a plain folder. Set `DEVPILOT_WORKSPACE` to a repository root, e.g. this project's directory, and reconnect. Then try `git_log` with `limit` = `3`, and `git_diff` with and without `staged` = `true` after editing or staging a file.
   - `analyze_repository` with no arguments. Against the sample project, expect 4 files (`Python: 3`, `Markdown: 1`), the test directory `sample_project/tests` and empty `heuristics`, because the sample has no manifests. To analyze a real repository, set `DEVPILOT_WORKSPACE` in step 3 to that repository's path and reconnect.

### Inspector CLI (scriptable)

```powershell
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/list
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_files --tool-arg query=TODO
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=class
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name search_code --tool-arg query=def path=sample_project/src
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name read_file --tool-arg path=../../secret.txt
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe --method tools/call --tool-name analyze_repository
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/other-repo --method tools/call --tool-name analyze_repository
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_status
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_log --tool-arg limit=3
npx @modelcontextprotocol/inspector --cli .venv\Scripts\devpilot-mcp.exe -e DEVPILOT_WORKSPACE=D:/path/to/a-git-repo --method tools/call --tool-name git_diff --tool-arg staged=true
```

Launch the server through the `devpilot-mcp` executable rather than `python -m devpilot_mcp`. The Inspector CLI parses flags such as `-m` and `-e` itself, so they never reach the server.

## Running the tests

```powershell
python -m unittest discover -s tests -t . -v
```

The suite builds a temporary workspace with a `secret.txt` just outside it. It checks the path-escape attempts listed under [Security model](#security-model), every tool's normal and error cases, and full round trips through an in-process MCP client. The Git tests need `git` on `PATH`. They build throwaway repositories with an isolated Git config, and are skipped if Git is not installed. On Windows the symlink-escape test is skipped unless Developer Mode is on, because creating symlinks requires it.

## Roadmap

Phase 1 added read-only filesystem access, Phase 2 added code search, Phase 3 added repository analysis and Phase 4 added read-only Git intelligence. Later phases will be designed separately.
