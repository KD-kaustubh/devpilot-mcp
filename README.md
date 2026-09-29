# DevPilot MCP

DevPilot MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI model structured, security-conscious access to one software repository. Its 18 tools read and search files, describe the repository's structure, report Git and GitHub facts, gather evidence for developer questions, apply an explicit patch **written by the caller** (and revert it), and run the repository's Python tests through two fixed commands. DevPilot collects evidence and carries out tightly bounded actions; the connected AI model does the reasoning.

## What It Does

```
User
  │
  ▼
AI model (e.g. Gemini)
  │
  ▼
MCP client
  │  stdio
  ▼
DevPilot MCP server
  ├── Filesystem intelligence    list_directory, read_file, search_files
  ├── Code search                search_code
  ├── Repository understanding   analyze_repository
  ├── Git intelligence           git_status, git_log, git_diff, git_branch
  ├── GitHub intelligence        github_repository, github_issues, github_pull_requests
  ├── Developer investigation    investigate_repository
  ├── Controlled patching        apply_patch, revert_patch
  └── Testing & validation       get_test_commands, run_tests, validate_repository
  │
  ▼
Repository (one configured workspace directory)
```

The server never touches anything outside that workspace directory. Every tool returns **structured output**, and its JSON schema is published to the client. Results are bounded, and anything cut short is reported (`truncated`, `truncated_fields`, `warnings`). An expected failure, such as a missing file or a rejected path, comes back as a tool error (`isError: true`) with a clear message instead of crashing the server.

## Why This Project

An AI assistant working on a codebase needs two things: accurate context about the repository, and a small set of actions whose effects are explicit and checkable. DevPilot follows an **evidence-first, controlled-action** model:

- **Evidence first.** Most tools only read. They return objective facts (files, matches, manifests, Git state, GitHub metadata) and never answer the question or call an LLM. `investigate_repository` ranks evidence for a question and leaves the interpretation to the model.
- **Controlled actions.** There are only two kinds of action. `apply_patch` applies exactly the unified diff the caller supplies, atomically, and `revert_patch` undoes it. `run_tests` runs one of two fixed Python test commands, chosen from repository evidence. DevPilot never generates code, commits, pushes or runs a caller-supplied command.
- **Explicit boundaries.** Every path goes through one workspace check, every process starts at one of two allowlisted launch points, and every GitHub request goes through one fixed-endpoint client.

## Key Features

- **Filesystem intelligence:** list, read and search text files, optionally scoped to a directory, with binary, size and dependency-folder handling.
- **Code search:** source-only, smart-case search that can be scoped to a directory or a single file.
- **Repository understanding:** languages, directories, documentation, configuration, dependency manifests, lock files and tests, with possible entry points and framework indicators kept apart as heuristics.
- **Git intelligence:** status, log, diff and branches through an allowlisted, read-only Git boundary.
- **GitHub intelligence:** repository metadata, issues and pull requests for the `origin` repository, over HTTPS GET to `api.github.com` only. `GITHUB_TOKEN` is optional.
- **Developer investigation:** deterministic, ranked evidence for a developer question: files with reasons, matching lines, excerpts, and Git and GitHub context.
- **Controlled patching:** strict unified-diff validation, protected files, atomic apply with rollback, and hash-checked revert.
- **Testing and validation:** framework detection that runs nothing, fixed-command test runs with a sanitized environment, a timeout, bounded output and side-effect reporting, and a validation report that executes no code by default.

## Security Model

DevPilot limits what an MCP client can reach and do. It is **not a sandbox**: the gaps are listed under [Known Limitations](#known-limitations).

### Workspace boundary

All paths are relative to the workspace root. `Workspace.resolve()` in `devpilot_mcp/workspace.py` is the only way a tool turns a path into a filesystem location. It:

1. rejects null bytes, absolute paths (`/etc/passwd`), drive-letter paths (`C:\Users\…`, `D:\other-project\…`, `c:foo`) and UNC paths (`\\server\share`);
2. joins the path to the root and calls `resolve()`, which collapses `..` and follows symlinks;
3. checks that the result is still inside the root. If it isn't, the request is rejected (`../../secret.txt` fails here).

A symlink inside the workspace that points outside it is hidden from `list_directory`, skipped by `search_files`, `search_code` and `analyze_repository`, and rejected by `read_file`. `analyze_repository` takes no path at all. It always analyzes the workspace root, and any extra arguments are dropped. Error messages never include absolute host paths.

### Symlink and junction protection

- **Reads:** a symlink that leads outside the workspace is hidden, skipped or rejected, as described above. Symlinks are not followed when walking directories.
- **Writes:** a patch target may not contain a symlink or junction **anywhere on its path**, even one that points back inside the workspace, and the final path must resolve to its literal location. See [Validation pipeline](#validation-pipeline).

### Read-only, write and execute tools

Every tool carries one of three MCP annotation profiles, all defined in `devpilot_mcp/tools/common.py`:

| Profile | Tools | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---------|-------|----------------|-------------------|------------------|-----------------|
| Read-only | the other 14 tools | true | false | true | false |
| Writes files | `apply_patch`, `revert_patch` | false | true | false | false |
| Executes code | `run_tests`, `validate_repository` | false | true | false | true |

**`openWorldHint` means one thing here:** the tool may cause interactions or effects whose external targets DevPilot does not bound. That is true only of the two tools that can run the repository's test code, because test code can reach anything. All network access DevPilot performs itself goes to one fixed host (`api.github.com`, HTTPS, GET only, three endpoints), so the GitHub tools and `investigate_repository` are `openWorldHint: false`. `validate_repository` runs tests only when `run_tests` is true, but it carries the execute profile because annotations describe what a tool can do. A test pins every tool's profile.

### Execution boundaries

- **Exactly two subprocess launch points:** `_execute` in `devpilot_mcp/tools/git.py` (Git) and `run_process` in `devpilot_mcp/testing/runner.py` (tests). Both take an argument list, use `shell=False` and closed stdin, and enforce a timeout and an output cap.
- **Git command allowlist:** only fixed read-only forms of `status`, `log`, `diff`, `diff-files`, `branch`, `rev-parse` and `remote get-url` can run, with the fsmonitor hook, external diff drivers, textconv filters, the pager and network protocols disabled. See [Read-only execution boundary](#read-only-execution-boundary).
- **No arbitrary shell executor.** No tool accepts a command, executable, argument list or shell string. The only test commands are `python -m pytest -p no:cacheprovider` and `python -m unittest` (optionally `discover -s <dir>`), launched by absolute interpreter path. See [Security restrictions](#security-restrictions).

### Controlled patching

- **Strict parser:** only standard unified diffs are accepted. Binary patches, renames, mode changes, quoted paths and anything that is not part of a diff are rejected, and hunks must match exactly at their stated lines.
- **Atomic apply and rollback:** every file is validated and staged first, then swapped in with `os.replace`. Any failure rolls back every completed step, so a patch applies completely or not at all.
- **Protected files:** `.git` contents, `.env` files (except `.env.example`), private keys and credential files can never be written.
- **Safe revert:** a revert is refused, touching nothing, if any file changed since the patch was applied.

See [Controlled code modification](#controlled-code-modification).

### Secrets and environment

- **GitHub token:** `GITHUB_TOKEN` is read at request time and placed only in the `Authorization` header. It is never returned, logged or included in error text.
- **Secret files:** likely secret files (`.env*`, keys, credentials) are never used as investigation evidence and can never be patched.
- **Test environment:** interpreter and pytest injection variables, every `GIT_*` variable and every variable whose name looks secret are removed before tests run, so test code cannot read `GITHUB_TOKEN`. This is a name-based blocklist, not a sandbox.
- **Output redaction (best-effort, not guaranteed):** test output and investigation evidence are scanned for known secret patterns and the values of removed variables, which are replaced with `[REDACTED]`. Secrets in other forms can still appear.

### GitHub access

- HTTPS to `api.github.com` only, GET only, three fixed endpoints. Tools cannot supply a URL, host, path, method or header.
- **Redirects are never followed**, so the `Authorization` header cannot reach another host.
- See [Read-only HTTP boundary](#read-only-http-boundary).

### Bounded operations

Every Git and test process has a timeout and an output cap, every HTTP request has a 15-second timeout and a response-size cap, and every list, scan, excerpt and diff has a documented limit. Limits are constants at the top of each module.

### What is not protected

- Test code run by `run_tests` runs with the permissions of the DevPilot process. It can read and write files and use the network; there is **no network or filesystem isolation**.
- Secret redaction and environment sanitization are **best-effort**; they do not guarantee that no secret reaches a client.
- Clean/smudge filter drivers configured in a repository's own `.git/config` cannot be disabled generically.

The full list is under [Known Limitations](#known-limitations).

## MCP Tool Catalogue

| Tool | Purpose | Access | External / Execution |
|------|---------|--------|----------------------|
| `list_directory` | List a directory's entries (`path`, default `"."`) | Read-only | None |
| `read_file` | Read a UTF-8 text file up to 1 MB (`path`) | Read-only | None |
| `search_files` | Case-insensitive text search in all text files (`query`, optional `path`, default `"."`) | Read-only | None |
| `search_code` | Smart-case search in source files only (`query`, optional `path`) | Read-only | None |
| `analyze_repository` | Factual repository overview: languages, directories, manifests, tests, heuristics (no inputs) | Read-only | None |
| `git_status` | Branch, HEAD, upstream, and staged, unstaged and untracked files (no inputs) | Read-only | Allowlisted `git` |
| `git_log` | Recent commits, newest first (`limit` 1–50) | Read-only | Allowlisted `git` |
| `git_diff` | Unstaged or staged changes with line counts and bounded diff text (`staged`, `path`) | Read-only | Allowlisted `git` |
| `git_branch` | Current branch and local branches (no inputs) | Read-only | Allowlisted `git` |
| `github_repository` | GitHub metadata for the `origin` repository (no inputs) | Read-only | `git remote get-url`; HTTPS GET to `api.github.com` |
| `github_issues` | Issues, pull requests excluded (`state`, `limit` 1–50) | Read-only | `git remote get-url`; HTTPS GET to `api.github.com` |
| `github_pull_requests` | Pull requests with branches and reviewers (`state`, `limit` 1–50) | Read-only | `git remote get-url`; HTTPS GET to `api.github.com` |
| `investigate_repository` | Ranked evidence for a developer question, not an answer (`query`, 1–500 characters) | Read-only | Allowlisted `git`; HTTPS GET to `api.github.com` (best effort) |
| `apply_patch` | Validate and atomically apply a caller-supplied unified diff (`patch`, up to 256 KB) | **Writes files** | None; no commands run |
| `revert_patch` | Restore the files of an earlier `apply_patch` (`change_id`) | **Writes files** | None; no commands run |
| `get_test_commands` | Detect pytest/unittest and the fixed command DevPilot would use (no inputs) | Read-only | None; runs nothing |
| `run_tests` | Run the detected fixed test command (`framework`, `timeout_seconds` 1–600) | **Executes code** | Runs repository test code; allowlisted `git status` for side-effect checks |
| `validate_repository` | Validation report: analysis, Git state, test discovery, syntax, optional test run (`run_tests`, `timeout_seconds`) | **Executes code** (only when `run_tests` is true) | Allowlisted `git`; runs test code only on request |

Every input is validated before anything runs; extra MCP arguments are dropped. Detailed behaviour, output examples and limits for each tool are in [Tool Reference](#tool-reference).

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

Where these values go depends on the client; consult its documentation for the configuration format. No client-specific configuration files are shipped. DevPilot has been exercised through MCP Inspector (web UI and CLI), over stdio, and through the MCP Python SDK client in the test suite.

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

## Known Limitations

DevPilot limits what a client can reach, but it is not a sandbox. These are the known gaps, in one place.

### Execution and isolation

- **Python-only test execution.** Only pytest and unittest are supported. No tox, nox, make or package-manager scripts are run, even when configured.
- **Not network-isolated.** Test code run by `run_tests` (or `validate_repository` with `run_tests=true`), including `conftest.py` and the repository's pytest configuration, runs with the permissions of the DevPilot process. It can read and write files and use the network. There is no sandbox, network isolation or filesystem isolation beyond the sanitized environment.
- **Environment sanitization is blocklist-based, not a sandbox.** Variables are removed by name: interpreter and pytest injection variables, `GIT_*`, and names that look secret (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, …). A secret in a variable with an unremarkable name is still passed to test code.
- **Secret redaction is best-effort, not guaranteed.** Test output and investigation evidence are scanned for GitHub tokens, AWS access key IDs, private-key headers, the configured `GITHUB_TOKEN` and, for test output, the values of removed variables. Secrets in any other form can appear in output.
- **Timeouts kill one process.** On timeout, only the launched test process is killed. Processes the tests started themselves may outlive it.
- **Side-effect detection is bounded.** It compares file sizes and modification times, not content hashes, skips `.git`, dependency and cache folders, and covers up to 20,000 files. Changes are reported, never reverted, and there is no content sandboxing.
- **Interpreter and dependencies.** Tests run with DevPilot's own interpreter unless `DEVPILOT_TEST_PYTHON` is set. A project whose dependencies (including pytest) are not installed there reports `status: "error"` with a warning.
- **Counts come from summary text.** Heavily customised pytest output can leave counts `null`.
- **Syntax is checked with DevPilot's Python grammar**, so code for a newer Python may be reported as a syntax error.
- **Git filter drivers.** Clean/smudge filter drivers configured in the repository's own `.git/config` cannot be disabled generically. Only run the Git tools on repositories whose `.git/config` you trust.

### Patching

- **The registry lives in memory, for the lifetime of the server process.** A restart, or a new server process, forgets every `change_id`. MCP Inspector's CLI mode starts a new server for each call, so to revert across processes, apply an explicit reverse patch. The web UI and normal MCP clients keep one session.
- **Exact hunks, not every diff form.** Hunks must match at their stated lines, so patches with wrong line numbers or counts are rejected, not guessed at. Renames (use a deletion plus a creation), mode changes, binary files, non-UTF-8 files, empty-file creation, quoted paths and patches in any other format are not supported.
- **Revert checks hashes, not stored copies.** To keep memory bounded, the post-apply state is verified by SHA-256 and size rather than a stored copy of the new bytes. The original bytes needed for restoring are always stored.
- **One server at a time.** Applies and reverts are serialized within one server. Two DevPilot servers editing the same workspace do not coordinate, although stale-patch and revert checks still refuse to overwrite changes they did not make.

### GitHub integration

- **github.com only.** GitHub Enterprise Server hosts are rejected.
- **Only the `origin` remote is used.** For a fork you work on, `origin` is usually the fork.
- **Bounded results.** Pages beyond the ones described above are not reachable, so only the most recent items up to `limit` are returned.
- **Issues in pull-request-heavy repositories.** With only 3 pages scanned, `github_issues` can return fewer than `limit` issues, with `has_more: true`.
- **Proxies.** HTTPS proxies set through the standard `HTTPS_PROXY` environment variable are used, as with any `urllib` client.

### Investigation

- **Keyword heuristic, not understanding.** Synonyms outside the small table, typos and conceptual questions ("why is it slow?") are not bridged. Ask with concrete names for better evidence.
- **Substring matching** can over-match short terms, e.g. `auth` in `author`. Ranking by rarity reduces, but does not remove, this noise.
- **Self-reference.** When investigating DevPilot itself, `tools/investigation.py` matches many queries, because it contains the heuristic's own vocabulary and example queries.
- **Scan budget.** In very large repositories the 2,000-line scan budget can run out; `truncated_fields` then includes `search_scan`.
- **Recent items only.** Only the newest 30 open GitHub issues and PRs, and the last 50 commits, are checked for relevance.

### Platform

- **Python 3.10.** TOML manifests are listed with a warning but not parsed, because `tomllib` needs Python 3.11+. The test suite exercises TOML parsing, so CI runs on Python 3.11 and 3.12.
- **Windows symlinks.** On Windows, the symlink-escape tests are skipped unless Developer Mode is on.

## Architecture

```
MCP client ──stdio──► server.py            builds the MCPServer and registers each tool group
                          │
                          ▼
                      tools/               MCP tool definitions, schemas, annotation profiles, error mapping
                          │
      ┌──────────────┬────┴─────────┬──────────────┬──────────────┐
      ▼              ▼              ▼              ▼              ▼
 text_search.py   github/        patching/      testing/       tools/git.py
 manifests.py     remote.py      unified_diff   detection      Git boundary
 sensitive.py     client.py      changes        runner         (subprocess)
                  (HTTPS GET)                   (subprocess)
      └──────────────┴──────────────┴──────┬───────┴──────────────┘
                                           ▼
                                     workspace.py          Workspace.resolve: the path boundary
```

- **Three boundaries:** all paths go through `workspace.py`, all processes start in `tools/git.py` or `testing/runner.py`, and all network access goes through `github/client.py`.
- **Shared leaf modules** (`text_search.py`, `manifests.py`, `sensitive.py`) import nothing else from DevPilot, so every layer can use them without cycles.
- **Lower layers use only public functions of tools/:** `github/remote.py` reads the origin URL through `tools.git.read_remote_url`, and `testing/` reuses `tools.git.git_status` and `tools.repository.analyze_repository`. This keeps every Git call inside the one Git boundary.

### Package structure

```
DevPilot-MCP/
├── devpilot_mcp/
│   ├── __main__.py        # enables `python -m devpilot_mcp`
│   ├── server.py          # builds the MCPServer, registers tool groups, stdio entry point
│   ├── config.py          # loads DEVPILOT_WORKSPACE from the environment / .env
│   ├── workspace.py       # path sandboxing (the security boundary)
│   ├── text_search.py     # shared file walking, text detection, line matching and skipped directories
│   ├── manifests.py       # shared Python dependency-manifest parsing (requirements*.txt, pyproject.toml)
│   ├── sensitive.py       # shared secret-file and secret-value patterns
│   ├── testing/
│   │   ├── detection.py   # test-framework detection and the fixed commands (nothing executed)
│   │   ├── runner.py      # the controlled, shell-free, bounded test execution
│   │   └── syntax.py      # Python syntax validation by parsing only
│   ├── patching/
│   │   ├── unified_diff.py # strict unified-diff parser and in-memory hunk application
│   │   └── changes.py     # validation, atomic commit/rollback, change registry, revert
│   ├── github/
│   │   ├── remote.py      # discovers owner/repo from the origin remote
│   │   └── client.py      # read-only GitHub REST client (the only code that calls GitHub)
│   └── tools/
│       ├── common.py      # the three annotation profiles and error mapping
│       ├── filesystem.py  # list_directory, read_file, search_files
│       ├── code_search.py # search_code
│       ├── repository.py  # analyze_repository
│       ├── git.py         # git_status, git_log, git_diff, git_branch (the Git execution boundary)
│       ├── github.py      # github_repository, github_issues, github_pull_requests
│       ├── investigation.py # investigate_repository
│       ├── patch.py       # apply_patch, revert_patch (the only write tools)
│       └── testing.py     # get_test_commands, run_tests, validate_repository
├── tests/                 # unittest suite (sandboxing, tool logic, in-process MCP client)
├── workspace/
│   └── sample_project/    # small demo repo to explore with the tools
├── .github/workflows/test.yml # CI: runs the unittest suite
├── .env.example
├── LICENSE
└── pyproject.toml
```

To add a tool group, create a new module in `devpilot_mcp/tools/` with a `register(server, workspace)` function, pick one of the annotation profiles in `tools/common.py`, then call it from `create_server()` in `server.py`.

## Tool Reference

Detailed behaviour, output examples and limits for each tool group.

### Filesystem tools

- **`list_directory(path=".")`**: the directory's entries (`name`, `path`, `type`, `size_bytes`), directories first. `""` also means the workspace root. Capped at 500 entries, with a `truncated` flag and `total_entries`.
- **`read_file(path)`**: `content`, `size_bytes` and `line_count`. Binary files, non-UTF-8 files and files over 1 MB are rejected.
- **`search_files(query, path=".")`**: case-insensitive substring matches (`path`, `line_number`, `line`), plus `files_with_matches`, `files_searched` and `truncated`. Capped at 100 matches. Binary files, files over 1 MB and dependency/VCS folders (`.git`, `node_modules`, `.venv`, …) are skipped.

`search_files` searches the whole workspace by default. The optional `path` limits the search to a directory or a single file:

- `path` goes through the same `Workspace.resolve` check as every other path, so `..` escapes and absolute, drive-letter and UNC paths are rejected. A path that does not exist is a tool error.
- Returned paths stay relative to the workspace root, so they can go straight into `read_file`.
- Dependency and VCS folders are skipped only *below* the search path, so an explicit `path="node_modules/some-lib"` is searched.

Unlike `search_code`, `search_files` looks at every text file, including prose such as `.md` and `.txt`.

### Code search

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

### Repository analysis

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

#### Facts

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

#### Heuristics

These are grouped separately because they are not confirmed facts:

- **`possible_entry_points`**: entry points declared in manifests come first, then conventional filenames.
  - Manifest entries come from `[project.scripts]`, `[project.gui-scripts]` and `[tool.poetry.scripts]` in `pyproject.toml`; `main`, `bin` and `scripts.start` in `package.json`; and `[[bin]]` in `Cargo.toml`.
  - Conventional filenames include `main.py`, `app.py`, `server.py`, `manage.py`, `__main__.py`, `index.js`/`.ts`/`.tsx`, `main.go` and `main.rs`. Files inside test directories are excluded.
- **`framework_indicators`**: a framework is reported **only when a manifest declares it as a dependency**. Each indicator names the manifest and the evidence.
  - Python dependencies (normalized per PEP 503) are read from `pyproject.toml` (PEP 621, PEP 735 dependency groups and Poetry), `requirements*.txt` and `Pipfile`.
  - JavaScript dependencies come from all `package.json` dependency sections, and Rust dependencies from `Cargo.toml`.
  - `go.mod`, `pom.xml` and `build.gradle` are matched on exact module coordinates, e.g. `github.com/gin-gonic/gin` or `org.springframework.boot`.
  - A file called `django.py` or a folder called `flask/` is never evidence.

#### Bounds and failures

- Directories that `search_code` skips are skipped here too: VCS, dependency, cache and build output (`.git`, `.venv`, `node_modules`, `__pycache__`, `dist`, `build`, `*.egg-info`, …). Symlinks are not followed.
- At most **20,000 files** are considered. If the scan stops early, `scan_complete` is `false`.
- Every list holds at most **50 items**, shallowest paths first. `truncated_fields` names any list that was cut, and `tests.file_count` always gives the full count. A 3,000-file tree produces roughly 12 KB of output.
- At most 20 manifests (up to 512 KB each) are parsed. `setup.py` is listed but never read, because it is code.
- A manifest that is malformed, binary or unreadable becomes a `warnings` entry instead of failing the call. If the workspace root has disappeared, the call returns a tool error.
- TOML manifests are parsed with the standard-library `tomllib` (Python 3.11+). On Python 3.10 they are listed with a warning but not parsed.

### Git tools

The four Git tools describe the repository's state and recent history. They **never change anything**: there is no add, commit, checkout, reset, fetch or push, and no index or file is written.

**The workspace must be the repository root.** The tools run against `DEVPILOT_WORKSPACE` itself. If that directory is not the top level of a Git work tree, every Git tool returns *"The workspace is not a Git repository"*. Parent directories are never searched, and no repository is ever initialized. The default `./workspace` is a plain folder, so to use the Git tools, point `DEVPILOT_WORKSPACE` at a repository root, e.g. this project's own directory.

#### git_status

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

#### git_log

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

#### git_diff

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

#### git_branch

```json
{
  "current_branch": "main", "detached": false,
  "branches": [{"name": "main", "commit": "82502ce", "upstream": "origin/main", "current": true}],
  "total_branches": 1, "truncated": false
}
```

Only local branches are listed, sorted by name. Nothing is ever created, deleted or switched.

#### Bounded output

| Limit | Value |
|-------|-------|
| Commits per `git_log` call | 1–50 (default 10) |
| Commit body | 2,000 characters (`body_truncated`) |
| Diff text | 60,000 bytes. Reading stops at the cap, so a huge diff is never loaded into memory. |
| File lists in `git_status` / `git_diff` | 200 each (`counts`, `files_changed`, `truncated_fields`, `warnings`) |
| Branches | 100 (`total_branches`, `truncated`) |
| Any Git process | 15-second timeout and at most 2 MB of output read |

These values are constants at the top of `devpilot_mcp/tools/git.py`.

#### Read-only execution boundary

Git runs as a subprocess, so every invocation goes through a single function, `_run_git`:

- **Allowlist.** Only `status`, `log`, `diff`, `diff-files`, `branch`, `rev-parse` and `remote` can run, and each in a fixed read-only form. `remote` is further limited to `get-url`, which the GitHub tools use to discover the repository. `diff-files` is the plumbing command used for unstaged diffs, because porcelain `git diff` rewrites `.git/index`. `rev-parse` only checks the repository. Any other subcommand, such as `add`, `commit`, `push`, `checkout`, `reset` or `config`, is refused before a process starts.
- **No command injection.** Tool input is limited to an integer (`limit`), a boolean (`staged`) and a workspace-validated `path`. The path is passed after `--` with `--literal-pathspecs`, so even `path="--output=x"` is just a filename to Git. Commands are built as argument lists and run with `shell=False`.
- **Fixed repository.** `cwd` is always the workspace root. Inherited `GIT_*` environment variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG_PARAMETERS`, …) are dropped, and `GIT_CEILING_DIRECTORIES` stops Git from climbing to a parent repository. No tool argument names a repository or directory.
- **No launched programs.** Repository config can make even read-only commands run programs. The tools disable the fsmonitor hook (`core.fsmonitor=false`), external diff drivers (`--no-ext-diff`), textconv filters (`--no-textconv`), the pager, GPG signature checks, network protocols and lazy fetches. The tests prove each blocked route is live with plain `git` and blocked in DevPilot.
- **No writes.** `GIT_OPTIONAL_LOCKS=0` stops `git status` from refreshing the index. A test snapshots every file in the repository, `.git` included, and checks that nothing changes.
- **Errors.** Git failures, a missing Git executable, timeouts and unexpected output become tool errors. Git's error message is reduced to one line with the absolute workspace path masked.

**Not covered:** clean/smudge filter drivers (`filter.<name>.clean`) configured in the repository's own `.git/config` cannot be disabled generically. Only run the Git tools on repositories whose `.git/config` you trust, as with running `git status` yourself.

### GitHub tools

The three GitHub tools collect **objective facts** from GitHub's REST API for an AI client to reason about. They return GitHub's own values in a stable, normalized schema, never the raw API payload, and they draw no conclusions. They are **read-only**: nothing is ever created, edited, commented on, merged or deleted.

#### Which repository?

The repository is **always discovered from the workspace**, never passed as an argument:

1. The workspace must be a Git repository root, as for the Git tools. Parent directories are never searched.
2. DevPilot reads the `origin` remote with `git remote get-url origin`, the only form of `git remote` the Git execution boundary allows. It only reads configuration, applies `url.<base>.insteadOf` rewrites and never contacts the remote.
3. The URL must point to `github.com`. These forms are accepted:
   - `https://github.com/OWNER/REPO(.git)`
   - `git@github.com:OWNER/REPO(.git)`
   - `ssh://git@github.com/OWNER/REPO(.git)`
   - `ssh://git@ssh.github.com:443/…`
4. The owner and repository name are validated strictly (GitHub's allowed characters and lengths), so they can only ever form the fixed API paths below.

Each of these is a clear tool error:
- no `origin` remote;
- an origin on another host (GitLab, Bitbucket, GitHub Enterprise Server);
- an origin that is a local path;
- a malformed URL.

Error messages never repeat the remote URL, because remote URLs can embed credentials.

#### Example output

`github_repository()` for this project (unauthenticated):

```json
{
  "owner": "KD-kaustubh", "name": "devpilot-mcp", "full_name": "KD-kaustubh/devpilot-mcp",
  "description": "this is the ai developer agent software to analyse the git repos",
  "html_url": "https://github.com/KD-kaustubh/devpilot-mcp", "homepage": null,
  "default_branch": "main", "visibility": "public", "private": false, "fork": false,
  "archived": false, "disabled": false, "is_template": false,
  "created_at": "2026-09-25T07:32:24Z", "updated_at": "2026-09-25T10:39:49Z", "pushed_at": "2026-09-25T10:39:45Z",
  "language": "Python", "license": null, "topics": [],
  "stargazers_count": 0, "watchers_count": 0, "subscribers_count": 0, "forks_count": 0, "open_issues_count": 0,
  "authenticated": false,
  "rate_limit": {"limit": 60, "remaining": 53, "used": 7, "reset_at": "2026-09-25T12:04:56Z", "resource": "core"}
}
```

Some counts keep GitHub's own meaning:
- `open_issues_count` includes open pull requests.
- `watchers_count` equals the star count.
- `subscribers_count` is the number of people watching the repository.

`github_issues(limit=2)` for a busy public repository, shortened:

```json
{
  "repository": "modelcontextprotocol/python-sdk", "state": "open", "limit": 2,
  "issues": [
    {
      "number": 3579, "title": "`ClientSessionGroup` has no routed `read_resource` …", "state": "open",
      "state_reason": null, "author": "baselarw", "labels": [], "assignees": [], "comments": 2, "locked": false,
      "created_at": "2026-09-24T05:58:22Z", "updated_at": "…", "closed_at": null,
      "html_url": "https://github.com/modelcontextprotocol/python-sdk/issues/3579"
    }
  ],
  "has_more": true, "pull_requests_excluded": 6, "pages_fetched": 1, "authenticated": false, "rate_limit": {"…": "…"}
}
```

`github_pull_requests(state="closed", limit=1)`, shortened:

```json
{
  "repository": "modelcontextprotocol/python-sdk", "state": "closed", "limit": 1,
  "pull_requests": [
    {
      "number": 3582, "title": "…", "state": "closed", "draft": false, "author": "…",
      "created_at": "…", "updated_at": "…", "closed_at": "…", "merged_at": null,
      "source_branch": "fix/hide-input-in-validation-errors", "source_repository": "…/python-sdk",
      "target_branch": "main", "target_repository": "modelcontextprotocol/python-sdk",
      "labels": [], "assignees": [], "requested_reviewers": [], "requested_teams": [],
      "html_url": "https://github.com/modelcontextprotocol/python-sdk/pull/3582"
    }
  ],
  "has_more": true, "authenticated": false, "rate_limit": {"…": "…"}
}
```

#### Issues and pull requests

- **Pull requests are excluded from `github_issues`.** GitHub's issues API returns pull requests too, marked with a `pull_request` key. They are left out rather than shown as issues, and counted in `pull_requests_excluded`. Use `github_pull_requests` for them.
- **What is left out:** issue and PR bodies, comments, reviews, changed files and commits.
  - `comments` on an issue is GitHub's comment count.
  - GitHub's pull-request listing has no comment count or `merged` flag, so neither is invented. `merged_at: null` means the PR has not been merged.
- **Missing values** are `null`, or `[]` for lists, never guessed. A deleted user becomes `"author": null`, and a deleted fork becomes `"source_repository": null`.
- **Order:** results keep GitHub's order, newest first.

#### Pagination, limits and rate limits

- **`limit` is 1–50** (default 10). Anything else is rejected before any request is made.
- **`github_pull_requests`** makes exactly one request with `per_page=limit`. `has_more` comes from the presence of a `rel="next"` page in GitHub's `link` header.
- **`github_issues`** requests `per_page = max(30, limit + 1)`. Because pull requests are filtered out, it may need further pages to reach `limit`, but it never fetches more than **3 pages**.
  - `has_more: true` means further issues may exist: an extra issue was seen, or GitHub reported another page.
  - `pages_fetched` shows how many requests were made.
- **Page URLs are built by DevPilot.** URLs inside the `link` header are never requested.
- **`rate_limit`** echoes GitHub's rate-limit headers: limit, remaining, used, reset time and resource. Unauthenticated requests get **60 per hour**. Once that is used up, a tool error states when the limit resets.
- **No automatic retries.** Each request has a 15-second timeout, and at most 10 MB of a response is read.

#### Authentication

`GITHUB_TOKEN` is **optional**:

- **Public repositories** work without it (`"authenticated": false`), within the 60-requests-per-hour limit.
- **Private repositories, or a higher rate limit:** put a token in `.env` (see `.env.example`) or in the server's environment. Use a **fine-grained personal access token** restricted to the repository, with only these **read-only** repository permissions:

| Permission | Needed for |
|------------|------------|
| Metadata: Read | `github_repository` (always granted to fine-grained tokens) |
| Issues: Read | `github_issues` |
| Pull requests: Read | `github_pull_requests` |

No write permission is needed or used. Without access, GitHub answers a private repository with 404, and the error says the token is missing or lacks access.

#### Read-only HTTP boundary

`devpilot_mcp/github/client.py` is the only code that talks to GitHub:

- **Three fixed operations:**
  - `GET /repos/{owner}/{repo}`
  - `GET /repos/{owner}/{repo}/issues`
  - `GET /repos/{owner}/{repo}/pulls`

  Only `state`, `per_page` and `page` are accepted as query parameters, and they are validated. There is no generic request method, so tools cannot supply a URL, path, host, method, header or query parameter. Extra MCP arguments are dropped.
- **GET only.** The transport has no method or body parameter, and the standard-library transport hard-codes a GET with no body.
- **HTTPS to `api.github.com` only.** Anything else is refused before a connection is made. Requests send `Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2026-03-10`, the latest version listed by `GET https://api.github.com/versions`.
- **Redirects are never followed**, so the `Authorization` header can never reach another host. A redirect, for example from a renamed repository, becomes an error asking you to update `origin`.
- **The token is treated as a secret:**
  - It is read from the environment at request time and placed only in the `Authorization` header inside the client.
  - It is never stored, returned, logged or included in error text; even GitHub's own error messages are scrubbed of it.
  - The tests check tool output, every error path, captured logs, stdout/stderr and the workspace files for the token.
- **No new dependencies.** HTTP uses Python's standard library (`urllib`).

#### Errors

These become tool errors with a clear message:
- a missing workspace, a workspace that is not a repository root, or an origin with no, a non-GitHub or a malformed URL;
- HTTP 401, 403 and rate limiting (including the reset time), 404 (with a private-repository hint), other 4xx responses (GitHub's own message, e.g. *Issues are disabled*) and 5xx server errors;
- redirects, timeouts and connection failures;
- invalid JSON, a response in an unexpected shape, and invalid arguments.

### Developer investigation

`investigate_repository(query)` is the entry point for questions such as:

- *"How is authentication implemented?"*
- *"Where is CSV processing implemented?"*
- *"What parts of the repository are involved in payment processing?"*
- *"How are MCP tools registered?"*

#### DevPilot gathers evidence; the AI reasons

The tool **never answers the question**. It calls no LLM, generates no explanation or plan, and makes no claim of semantic understanding.

- **DevPilot's job:** collect objective, bounded, deterministic evidence from the repository, its Git history and GitHub.
- **The consuming AI's job** (Gemini, Claude, …): read that evidence, draw conclusions and write the explanation or plan. It can call `read_file`, `search_code` or `git_diff` to dig deeper.

Every result carries an `evidence_note` saying so.

#### How evidence is gathered

It reuses the existing read-only building blocks rather than new logic:

| Step | Reuses |
|------|--------|
| 1. Turn the query into search terms (keyword heuristic, below) | — |
| 2. Repository facts: languages, top directories, manifests, tests, entry points, frameworks | `analyze_repository` (Phase 3) |
| 3. Match the terms against file paths and file contents | the `search_code` file walker and line scanner (Phase 2), with the same ignored directories, binary detection and size limits |
| 4. Rank files, directories and matching lines | — |
| 5. Short excerpts around the strongest matches in the top files | `read_file` (Phase 1): workspace-validated, text-only |
| 6. Git context: branch, clean/dirty, relevant changed files, recent commits, commits whose message mentions a term, short diffs of relevant changed files | `git_status`, `git_log`, `git_diff` (Phase 4 execution boundary) |
| 7. GitHub context: repository summary, and open issues and PRs whose title, labels or branch mention a term | `github_repository`, `github_issues`, `github_pull_requests` (Phase 5 client) |

**Search-term heuristic.** It is deliberately simple and deterministic, and it does not understand language:

1. Lower-case the query and split it into words.
2. Drop common English and question words ("how", "is", "implemented", "where", "code", …), words shorter than 3 characters, and pure numbers.
3. Stem each word lightly. For example, `uploads` → `upload`, `processing` → `process` and `structured` → `structur`. Matching is case-insensitive and by substring, so a stem still matches every form of the word.
4. Add a few synonyms from a small fixed table, e.g. `authentication` → `auth`, `login`, and `configuration` → `config`, `settings`.

Query words come first in the order they appear, then synonyms, up to 8 terms. For example, *"How is authentication implemented?"* gives `authentication`, then `auth` and `login` as synonyms. The terms used are returned in `search_terms`.

**Relevance ranking.** Also deterministic, with no embeddings, ML or external services:

1. For each term, a file gets:
   - 10 points if its **file name** contains the term, or 4 if a **directory** in its path does;
   - 2 + min(matching lines, 5) points for **content** matches.
2. Each term's points are multiplied by:
   - its **weight**: 2 for a query word, 1 for a synonym;
   - its **rarity**, `ln(1 + files / files containing the term)`. A term found everywhere, like the package name, counts for little.
3. Each additional distinct term the file matches adds 4 points.
4. The total is scaled by **file kind**: source ×1.0, tests ×0.7, documentation ×0.6. Implementations come first, but tests and docs stay visible.
5. Files are sorted by score, then by path. Matching lines are grouped by file in that order, strongest lines first within a file's budget, and listed by line number.

Every relevant file includes `reasons`, such as `"file name contains 'github'"` or `"12 matching lines for 'auth'"`, so the ranking can be checked.

#### Output

`investigate_repository("Where is GitHub integration implemented?")` on this repository, shortened:

```json
{
  "query": "Where is GitHub integration implemented?",
  "evidence_note": "Evidence only: DevPilot extracted search terms with a keyword heuristic and collected matching repository facts. It did not interpret the question or answer it, and the evidence may be incomplete.",
  "search_terms": [{"term": "github", "source": "query"}, {"term": "integration", "source": "query"}],
  "repository": {"name": "DevPilot-MCP", "total_files": 35, "languages": {"Python": 30, "Markdown": 2, "TOML": 1},
                 "top_directories": ["devpilot_mcp", "tests", "workspace"], "framework_indicators": ["MCP Python SDK"], "…": "…"},
  "relevant_files": [
    {"path": "tests/test_github.py", "kind": "test", "score": 45, "matched_terms": ["github", "integration"],
     "reasons": ["file name contains 'github'", "217 matching lines for 'github'", "1 matching line for 'integration'"]},
    {"path": "devpilot_mcp/tools/github.py", "kind": "source", "score": 43, "matched_terms": ["github"],
     "reasons": ["file name contains 'github'", "62 matching lines for 'github'"]},
    {"path": "devpilot_mcp/github/client.py", "kind": "source", "score": 28, "…": "…"}
  ],
  "relevant_directories": [{"path": "devpilot_mcp/tools", "relevant_files": 4, "score": 84},
                           {"path": "devpilot_mcp/github", "relevant_files": 3, "score": 76}],
  "code_matches": [{"file": "devpilot_mcp/tools/github.py", "line": 1, "text": "\"\"\"Read-only GitHub tools: …", "terms": ["github"], "kind": "source"}],
  "file_context": [{"path": "devpilot_mcp/tools/github.py", "start_line": 1, "end_line": 8, "content": "…", "truncated": false}],
  "git_context": {"available": true, "branch": "main", "clean": false,
                  "relevant_commits": [{"short_hash": "377f6af", "subject": "Implement Phase 5 GitHub integration",
                                        "matched_terms": ["github", "integration"], "…": "…"}], "…": "…"},
  "github_context": {"available": true, "repository": {"full_name": "KD-kaustubh/devpilot-mcp", "…": "…"},
                     "relevant_issues": [], "relevant_pull_requests": [], "open_issues_scanned": 0, "authenticated": false},
  "files_scanned": 33,
  "warnings": [],
  "truncated_fields": ["relevant_files", "code_matches"],
  "limits": {"max_relevant_files": 10, "max_code_matches": 30, "…": "…"}
}
```

#### Bounded results

| Evidence | Limit |
|----------|-------|
| Query | 1–500 characters; empty or whitespace-only queries are rejected |
| Search terms | 8 |
| Files considered / line matches scanned | 20,000 / 2,000; files whose path matches a term are scanned first |
| Relevant files / directories | 10 / 5 |
| Matching lines | 30 in total, at most 5 per file, each line up to 200 characters |
| Source excerpts | the top 3 files with content matches, up to 2 windows each, ±4 lines around a match, lines cut at 300 characters, **12 KB in total** |
| Git | 50 commits scanned, 5 recent and 5 relevant commits, 20 relevant changed files, 2 diffs of up to 4,000 characters |
| GitHub | the 30 newest open issues and 30 newest open PRs scanned, 5 relevant of each |

- **Visible limits:** the `limits` field repeats these values. Any list that was cut is named in `truncated_fields`, and incomplete evidence (scan limits, unreadable files, Git or GitHub failures) is explained in `warnings`.
- **Size:** answers on this repository are 15–18 KB.
- **Not exhaustive:** the tool never claims to have found *all* relevant code.

#### Git and GitHub context

- **Git context:** needs the workspace to be a Git repository root, as for the Git tools. Otherwise `git_context.available` is `false` with a reason, and the local evidence is still returned. A Git failure, such as a timeout, becomes a warning, not an error.
- **GitHub context:** best effort, and only when `origin` is a github.com repository.
  - It uses the Phase 5 read-only client: three fixed GET endpoints, and `GITHUB_TOKEN` is optional.
  - If GitHub fails (offline, rate-limited, 404…), no further GitHub requests are made, a warning is added, and the local investigation is returned in full.
  - An investigation costs at most 3 GitHub requests in the common case: repository, issues and pull requests. The issues request may take up to 3 pages. Unauthenticated use is limited to 60 GitHub requests per hour.

#### Security

The tool only composes the existing read-only boundaries, so everything in [Security model](#security-model), [Read-only execution boundary](#read-only-execution-boundary) and [Read-only HTTP boundary](#read-only-http-boundary) applies:

- **Input:** the only input is `query`. It is used as search text, never as a path, command, URL or endpoint. Extra MCP arguments are dropped.
- **Nothing runs or changes:** no shell, no repository code, scripts, package managers, tests or hooks are run, and no file or Git state is modified. A test hashes every file, `.git` included.
- **Git:** only the Phase 4 allowlisted read-only subcommands run.
- **GitHub:** only the three Phase 5 GET endpoints are called.
- **Secrets:**
  - Likely secret files (`.env*` except templates such as `.env.example`, `*.pem`, `*.key`, SSH keys, `credentials.json`, `secrets.*`, `.npmrc`, `.pypirc`, `.netrc`) are never used as evidence.
  - Secret-looking values in the evidence are replaced with `[REDACTED]`, with a warning. These are GitHub tokens, AWS access key IDs, private-key headers and the configured `GITHUB_TOKEN`.
  - The token never appears in output, errors or logs.

### Controlled code modification

Phase 7 adds the only two tools that modify files. The division of labour is strict:

- **The caller (the AI client or a person) writes the patch.** DevPilot does not generate, choose, complete or "fix up" changes.
- **DevPilot validates and applies exactly that patch,** atomically, or changes nothing.
- **Every applied change can be reverted** by its `change_id` in the same server session, but only if nobody has edited the files since.

Nothing is executed: no shell, tests, linters, formatters, package managers, build systems, hooks or repository scripts. Git is never used to apply or revert changes, and no LLM or GitHub call is made. `git_status` and `git_diff` simply see the edited working tree afterwards.

#### apply_patch

Pass a standard unified diff (the format of `diff -u` and `git diff`):

```diff
--- a/src/utils.py
+++ b/src/utils.py
@@ -3,4 +3,5 @@

 def format_price(value: float) -> str:
-    # TODO: support currencies other than USD
-    return f"${value:,.2f}"
+    """Format a price in US dollars."""
+    # Other currencies are not supported yet.
+    return f"${value:,.2f}"
```

Result (from the MCP Inspector verification):

```json
{
  "change_id": "chg_7b5a4ea39ddfed72",
  "patch_sha256": "f82852a7b90760b8a2add2cae505f92d29d1d5d711103d22efe796ee67904d51",
  "applied_at": "2026-09-28T09:26:09Z",
  "files_changed": [
    {"path": "src/utils.py", "change": "modified", "additions": 3, "deletions": 2,
     "sha256_before": "d1d1e11c…", "sha256_after": "e1ca2a7a…"}
  ],
  "files_changed_count": 1, "additions": 3, "deletions": 2,
  "reversible": true, "evicted_change_ids": []
}
```

**Supported patch format:**

| | |
|---|---|
| Modify a text file | `--- a/path` / `+++ b/path` with one or more `@@ -l,c +l,c @@` hunks |
| Create a text file | `--- /dev/null` / `+++ b/path`, one hunk of `+` lines. Missing parent directories are created. |
| Delete a text file | `--- a/path` / `+++ /dev/null`, whose `-` lines must be the **entire** current file |
| Git headers | `diff --git a/p b/p`, `index …`, `new file mode 100644/100755` and `deleted file mode …` are accepted and checked for consistency |
| Path prefixes | `a/` and `b/` are stripped; unprefixed paths (`--- README.md`) work too |
| End-of-file | `\ No newline at end of file` is honoured |
| Encoding | UTF-8 text only. Existing line endings (LF, CRLF or mixed), a UTF-8 BOM and a missing final newline are preserved. |
| **Rejected** | binary patches, renames and copies, mode changes, quoted paths, empty-file or metadata-only sections, and any line that is not part of a unified diff (e-mail headers, prose, shell text) |

Hunks must match the current file **exactly at the line numbers they state**. There is no offset search and no fuzz. A patch made against different or older content is rejected as stale instead of being forced in.

#### Validation pipeline

Every step runs for **every file** in the patch before anything is written:

1. **Size and format:** the patch must be non-empty, contain no null bytes, fit the byte limit, and parse strictly as a unified diff.
2. **Resource limits:** files, hunks, added lines and deleted lines are counted against the limits below.
3. **Target paths:**
   - Each path goes through the existing `Workspace.resolve` boundary, which rejects absolute, drive, UNC and null-byte paths and `..` escapes.
   - Writes add stricter rules on top:
     - paths must be normalized and `/`-separated;
     - no `:` (which would allow NTFS alternate streams);
     - no Windows device names or trailing dots or spaces;
     - no symlink or junction **anywhere on the path**, even one pointing back inside the workspace;
     - the final path must resolve to its literal location.
4. **Protected files:** see the next section.
5. **Expected content:**
   - Each target must be an existing UTF-8 text file within the size limit; for a creation, the path must not exist yet.
   - Every context and removed line must match the file exactly.
   - The new content of every file is computed in memory and checked against the result-size limit.
6. **Atomic commit** (below). Only then is the change recorded in the registry.

Errors name the file, hunk and line number but **never quote file or patch contents**, so a rejected patch cannot echo secrets back.

#### Atomicity

- **Stage:** every new file content is fully written (and `fsync`ed) to a temporary `.devpilot-staging-*` file in the target's own directory.
- **Swap:** targets are then replaced with atomic renames (`os.replace`). Deleted files are renamed aside rather than removed.
- **Roll back:** if anything fails during either step, every completed step is undone in reverse order, temporary files are removed, and newly created directories are cleaned up. The error says whether the rollback succeeded. If a rollback itself ever failed, the error names the affected files.
- **Tested:** failures are forced partway through multi-file patches (modify, delete, create and modify at once), both while staging and after two files were already swapped in. The tests check that every original file is byte-identical afterwards and that no temporary file is left behind.

#### Protected files

A patch can never create, modify or delete:

| Protected | Examples |
|-----------|----------|
| anything inside a `.git` directory, at any depth, in any letter case | `.git/config`, `.git/hooks/pre-commit`, `sub/.GIT/…` |
| environment files | `.env`, `.env.local`, `config/.env.production`. **Exception:** `.env.example` stays editable |
| private keys and certificates | `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `*.pem`, `*.key`, `*.p12`, `*.pfx` |
| credential and secret files | `credentials.json`, `secrets.*`, `*.secrets.*`, `.npmrc`, `.pypirc`, `.netrc` |

This is the same secret-file list `investigate_repository` already excludes from evidence (Phase 6). For writes, only `.env.example` is exempt. Names are compared case-insensitively.

#### Resource limits

| Limit | Value |
|-------|-------|
| Patch size | 256 KB (256,000 bytes) |
| Files per patch | 20 |
| Hunks per patch | 100 |
| Added lines per patch | 2,000 |
| Deleted lines per patch | 2,000 |
| Size of an existing file the patch touches | 1 MB |
| Size of any resulting file | 1 MB |
| Changes kept for revert | the 50 most recent, up to 50 MB of original content |

The constants are at the top of `devpilot_mcp/patching/changes.py`. Any limit is checked before a single byte is written.

#### Change registry and revert_patch

- **What is recorded:** each successful `apply_patch` gets a random, unique `change_id` (`chg_` + 16 hex digits). The in-memory registry keeps:
  - the patch SHA-256 and the time;
  - each file's change type, path and line counts;
  - its **original bytes**, and the **SHA-256 and size of the content DevPilot wrote**;
  - any directories the patch created.
- **When a change is forgotten:** changes are dropped when the server stops, when 50 newer changes exist, or when the 50 MB budget is exceeded. `evicted_change_ids` reports any change dropped by an apply, and `reversible: false` means the change could not be recorded at all.
- **Revert pre-checks:** `revert_patch(change_id)` first checks **every** file of the change:
  - modified and created files must still exist, with exactly the recorded size and SHA-256;
  - deleted files must still be absent;
  - every path must still pass the target rules above, for example no link swapped in.
- **Refusal:** if any file differs, the whole revert is refused and **no file is touched**, so edits made after the patch are never overwritten. The error lists the changed paths, and the change stays revertable once they are restored.
- **Restoring:**
  - modified files get their original bytes back, byte for byte, including line endings and BOM;
  - created files are removed, along with any directories the patch created that are now empty;
  - deleted files are recreated.

  This uses the same atomic staging, swap and rollback engine.
- **After success:** the change is removed from the registry, so it can only be reverted once.

```json
{"change_id": "chg_fe201c7d50294e3a", "reverted": true,
 "files_restored": [{"path": "src/utils.py", "action": "restored"}, {"path": "src/currency.py", "action": "removed"}],
 "files_restored_count": 2}
```

### Testing and validation

Phase 8 adds objective validation evidence to the loop:

**repository understanding → investigation → controlled patch → testing / validation → structured evidence**

DevPilot runs the repository's tests in a tightly controlled way and reports what happened. It does not interpret results, fix failures, retry, score the repository or call an LLM. The consuming AI reasons over the evidence.

> **Arbitrary shell commands are not supported.** No tool accepts a command, executable, argument list or shell string. Only two fixed test commands exist, and DevPilot chooses between them from repository evidence.

#### Supported frameworks

Python only, with exactly these commands. `python` means the selected interpreter, described under [Security restrictions](#security-restrictions).

| Framework | Command |
|-----------|---------|
| pytest | `python -m pytest -p no:cacheprovider` (the cache plugin is disabled so DevPilot writes no `.pytest_cache`) |
| unittest | `python -m unittest`, or `python -m unittest discover -s <dir>` when the tests live in a directory without `__init__.py`. Root discovery cannot reach such directories on Python 3.11+, so `<dir>` is taken from the repository's own layout and validated before use. |

#### get_test_commands

This tool inspects files only. It never imports or runs anything.

| Evidence | Confidence |
|----------|------------|
| `pytest.ini`, `pyproject.toml [tool.pytest…]`, `setup.cfg [tool:pytest]`, `tox.ini [pytest]`, a `conftest.py` at the root or in a test directory | pytest **high** |
| pytest declared in `pyproject.toml` or `requirements*.txt`, test files that `import pytest`, module-level `def test_…` functions, or tox `testenv` commands that mention pytest (tox itself is never run) | pytest **medium** |
| `unittest.TestCase` classes in `test*.py` files | unittest **high** |
| `TestCase` classes in files unittest's default `test*.py` pattern misses, or test files that only `import unittest` | unittest **low** |

- **Primary framework:** highest confidence wins; on a tie pytest is preferred, because it also runs unittest-style tests.
- **Warnings:**
  - conflicts, such as both frameworks detected;
  - test files that the chosen command would miss;
  - malformed `pyproject.toml`, `setup.cfg` or `tox.ini` (reported, then ignored);
  - a framework not installed in the interpreter (`runner_available: false`).
- **No evidence:** with nothing detected, `detected` is empty and a warning says so. Nothing is guessed.

On DevPilot itself:

```json
{
  "detected": [
    {"framework": "unittest", "command": ["python", "-m", "unittest"], "confidence": "high",
     "evidence": ["unittest.TestCase classes in test*.py files", "test files import unittest", "tests/", "workspace/sample_project/tests/"],
     "runner_available": true},
    {"framework": "pytest", "command": ["python", "-m", "pytest", "-p", "no:cacheprovider"], "confidence": "medium",
     "evidence": ["module-level test functions (pytest style)", "tests/", "workspace/sample_project/tests/"], "runner_available": false}
  ],
  "primary": {"framework": "unittest", "…": "…"},
  "test_file_count": 11, "test_directories": ["tests", "workspace/sample_project/tests"],
  "interpreter": {"source": "devpilot", "valid": true, "python_version": "3.12.10", "problem": null},
  "warnings": ["1 test file(s) define module-level test functions, which only pytest runs.",
               "Both pytest and unittest evidence was found; 'unittest' is primary (high confidence). pytest can also run unittest-style tests."]
}
```

#### run_tests

- **Which command runs:** the primary framework's command, or the one named by `framework`, which must also have been **detected**. Anything else, such as `"; whoami"` or `"cmd /c …"`, is rejected before a process starts.
- **Timeout:** `timeout_seconds` is 1–600 (default 120). When it runs out, the test process is killed and the status is `timed_out`.
- **Result:**

```json
{
  "framework": "unittest", "command": ["python", "-m", "unittest", "discover", "-s", "tests"],
  "status": "passed", "exit_code": 0,
  "passed": 2, "failed": 0, "errors": 0, "skipped": 1, "total": 3, "other_counts": {},
  "duration_seconds": 0.109, "timed_out": false, "timeout_seconds": 60,
  "stdout": "", "stderr": ".s.\r\n----------------------------------------------------------------------\r\nRan 3 tests in 0.000s\r\n\r\nOK (skipped=1)\r\n",
  "stdout_bytes": 0, "stderr_bytes": 118, "output_truncated": false,
  "removed_environment_variables": ["GITHUB_TOKEN", "PYTHONSTARTUP"], "redactions": 0,
  "side_effects": {"checked": true, "complete": true, "files_created": [], "files_modified": [], "files_deleted": [],
                   "total_changes": 0, "git_state_changed": false},
  "warnings": []
}
```

The example above comes from the Phase 8 verification run on Windows. `removed_environment_variables` depends on the server's own environment and is abridged here.

- **Status values:**
  - `passed`, `failed` (failures or errors);
  - `no_tests` (nothing collected; exit code 5);
  - `timed_out`;
  - `error` (for example, the framework isn't installed, or a usage error).
- **Counts:** parsed from pytest's summary line or unittest's `Ran N tests … OK/FAILED (…)` lines. When there is no summary, the counts are `null`, never invented.
- **Output:** stdout and stderr are returned separately.

#### validate_repository

The checks always run in this order:

| Check | What it does | Statuses |
|-------|--------------|----------|
| `repository_analysis` | `analyze_repository` facts (Phase 3) | passed / error |
| `git_working_tree` | `git_status` (Phase 4, read-only): clean or dirty | passed (clean) / **warning** (dirty) / skipped (not a Git repository root) |
| `test_discovery` | `get_test_commands` | passed / warning (nothing detected) |
| `test_execution` | the primary command via `run_tests`, **only when `run_tests` is true** | skipped (the default) / passed / failed / warning (`no_tests`) |
| `python_syntax` | every `.py`/`.pyi` file is **parsed** with `compile(..., ast.PyCF_ONLY_AST)`. Nothing is executed or imported. | passed / failed / skipped (no Python files) |

- **Default:** executes no code at all.
- **Report sections:** `repository`, `git`, `tests` (`discovery`, `execution_requested`, `executed`, `result`), `syntax`, `checks`, `warnings`, `failures` (the failed checks), `skipped` (the skipped checks) and `complete` (every check ran to completion).
- **No verdict:** there is no score, ranking or good/bad judgement.
- **Syntax only:** syntax validation finds syntax errors, including indentation and tab errors, and reports `SyntaxWarning`s. It does *not* find import, runtime or type errors. The grammar is that of the Python running DevPilot.

A failing example (a failing assertion and a syntax error in a disposable repository):

```json
"checks": [
  {"name": "repository_analysis", "status": "passed", "detail": "4 files analyzed."},
  {"name": "git_working_tree", "status": "warning", "detail": "Working tree has changes: 0 staged, 1 unstaged, 1 untracked."},
  {"name": "test_discovery", "status": "passed", "detail": "Primary: unittest (high confidence)."},
  {"name": "test_execution", "status": "failed", "detail": "unittest: failed; exit code 1; passed=1, failed=1, errors=0, skipped=1."},
  {"name": "python_syntax", "status": "failed", "detail": "1 file(s) failed to parse (first: src/broken.py:1: SyntaxError: invalid syntax)."}
],
"failures": ["test_execution: …", "python_syntax: …"]
```

#### Security restrictions

- **No caller-supplied commands.** `run_tests` takes only an enum and an integer. Extra MCP arguments such as `command`, `executable`, `shell`, `cwd` or `env` are dropped by the server.
- **No commands from repository config.** Configuration is inspected as evidence only. The one derived argument, the `discover -s` directory, must be an existing directory inside the workspace that cannot be read as an option. Before anything runs, the argument vector is checked against the fixed command shapes.
- **Controlled interpreter.** The executable is an absolute path, so it is never looked up on `PATH` or in the workspace:
  - by default, the Python running DevPilot (`sys.executable`);
  - optionally, `DEVPILOT_TEST_PYTHON`, set by the *server operator* (see `.env.example`). It must be absolute, exist, and be named `python`/`python.exe`.

  Neither the MCP caller nor the repository can choose it.
- **No shell.** `subprocess` runs with an argument list, `shell=False`, stdin closed, and the working directory set to the validated workspace root. No `cmd.exe`, PowerShell, `bash` or `sh` is involved, and Unix tools such as `which`, `grep` or `timeout` are never used.
- **Sanitized environment.**
  - Removed: interpreter and pytest injection variables (`PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, …), every `GIT_*` variable, and every variable whose name looks secret (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`, `*CREDENTIAL*`, `*AUTH*`, …).
  - Test code therefore cannot read `GITHUB_TOKEN`. The names (never the values) of removed variables are reported.
  - Set: `PYTHONDONTWRITEBYTECODE=1`, so no `__pycache__` is written, and colour is turned off.
- **Redacted output.**
  - Values of removed secret variables and GitHub-token or private-key patterns become `[REDACTED]`.
  - The workspace path and interpreter path are shown as `<workspace>` and `<python>`.
- **One run at a time** per server. A concurrent request is refused.

#### Timeout and output limits

| Limit | Value |
|-------|-------|
| `timeout_seconds` | 1–600, default 120; the process is killed when it is exceeded |
| Output kept per stream | the first 8 KB and the last 24 KB (summaries are printed last), with an `… [N bytes omitted] …` marker and `output_truncated: true` |
| Side-effect snapshot | up to 20,000 files (`side_effects.complete`) and up to 50 listed paths per category |
| Syntax check | up to 5,000 files of at most 1 MB each; up to 50 errors and 50 warnings listed |
| Detection | up to 50 test files inspected (256 KB each) and 10 evidence items per framework |

#### Side effects

**Running tests means running repository code.** Test code, `conftest.py` and the repository's own pytest configuration run with the permissions of the DevPilot process. They can read and write files and use the network.

- **What DevPilot itself does:** it never intentionally writes files, changes Git or uses the network while running tests. It disables `.pytest_cache` and bytecode caches.
- **What it reports:** before and after each run it snapshots file sizes and modification times, skipping `.git`, `.venv`, `node_modules` and cache folders, and captures `git status`. Anything the tests created, modified or deleted is reported in `side_effects`, with a warning.
- **What it never does:** revert those changes, run tests with elevated privileges, or offer any way to run a different command.

Only run tests of repositories whose test code you are willing to execute.

#### Windows considerations

- **Absolute interpreter path.** Windows' process launcher also searches the current directory, so a bare `python` could pick up a `python.exe` planted in the repository. DevPilot always launches the interpreter by absolute path.
- **No console windows.** Processes start with `CREATE_NO_WINDOW`. Output is read by background threads, so a chatty test cannot deadlock a pipe.
- **unittest discovery.** It only descends into packages (Python 3.11+), which is why a `tests/` folder without `__init__.py` gets `discover -s tests`.
- **Test runs are verified on Windows:** during the Phase 8 verification, DevPilot ran its own suite (349 tests at the time) through `validate_repository(run_tests=true)` over MCP stdio, with 345 passed, 4 skipped, no side effects, and the repository and `.git` byte-identical afterwards.

## Development / Testing

The suite uses only the standard library's `unittest`; pytest is not a dependency and is not needed. Run everything from the project root with the virtual environment active:

```powershell
python -m unittest discover -s tests -t . -v
```

The full suite can take a few minutes, because many tests start real Git and Python processes and run real test suites in throwaway projects. While working on one area, run a single module, class or test:

```powershell
python -m unittest tests.test_filesystem_tools -v
python -m unittest tests.test_filesystem_tools.SearchFilesPathTests -v
python -m unittest tests.test_server.ServerTests.test_annotation_profiles_are_intentional -v
```

The Git tests need `git` on `PATH`. They build throwaway repositories with an isolated Git config, and are skipped if Git is not installed. The GitHub tests **never contact GitHub**: tool and client tests use a fake transport, and the HTTP transport is tested against local `127.0.0.1` servers. On Windows, the symlink-escape tests are skipped unless Developer Mode is on, because creating symlinks requires it.

Current result on Windows (Python 3.12): **359 tests, 355 passed, 4 skipped, 0 failed.** The skips are the three Windows symlink tests that need Developer Mode, and the real-pytest test, because pytest is not installed in DevPilot's environment. On Linux (Ubuntu, Python 3.12) the same suite passes with 2 skipped: the Windows-only junction test and the real-pytest test. pytest behaviour is still covered by parsing recorded output and by a real "pytest not installed" run.

**Continuous integration:** `.github/workflows/test.yml` installs the package and runs the same `unittest` command on every push and pull request, on Ubuntu and Windows with Python 3.11 and 3.12.

## Security Testing

The suite builds a temporary workspace with a `secret.txt` just outside it. It checks the path-escape attempts listed under [Workspace boundary](#workspace-boundary) for every path-taking tool, including `search_files` with a `path`, every tool's normal and error cases, and full round trips through an in-process MCP client. A test pins the annotation profile of all 18 tools.

- **Git:** each blocked route for launching programs (fsmonitor, external diff, textconv, pager, …) is shown to be live with plain `git` and blocked in DevPilot, and a snapshot of every file, `.git` included, proves that nothing is written.
- **GitHub:** tool output, every error path, captured logs, stdout/stderr and the workspace files are checked for the token, and redirects and non-`api.github.com` hosts are refused.
- **Investigation:** tests use a fake GitHub client, real throwaway Git repositories and hashes of every file before and after.
- **Patching:** the patch tests attempt the full set of malicious patches (traversal, absolute, drive and UNC paths, symlinks and Windows junctions, protected files, unexpected metadata, oversized and malformed patches). Each one takes a byte-level snapshot before and after. Failures are forced partway through multi-file patches to prove rollback.
- **Testing and validation:** the Phase 8 tests run real unittest suites in throwaway projects, covering passing, failing, skipped, timed-out, truncated, side-effecting and no-tests runs. They also try shell and command injection payloads (`; whoami`, `&& powershell …`, `cmd /c …`, `../../…`, absolute and UNC paths, tampered argument vectors) and check that no process starts, and that the environment and output leak no secrets.

## Phase History

DevPilot was built in phases, each adding one capability on top of the same boundaries:

1. **Filesystem intelligence:** read-only `list_directory`, `read_file` and `search_files`.
2. **Code search:** `search_code`, source files only, scoped to a subdirectory or file.
3. **Repository understanding:** `analyze_repository`, a deterministic, factual overview.
4. **Git intelligence:** read-only `git_status`, `git_log`, `git_diff` and `git_branch`.
5. **GitHub integration:** read-only `github_repository`, `github_issues` and `github_pull_requests` for the `origin` repository.
6. **Developer investigation:** `investigate_repository(query)`, ranked and bounded evidence for a developer question.
7. **Controlled patching:** `apply_patch` applies an explicit, caller-supplied unified diff atomically; `revert_patch` undoes it by `change_id`.
8. **Testing and validation:** `get_test_commands`, `run_tests` (two fixed commands, on request only) and `validate_repository`.
9. **Production cleanup:** optional `path` for `search_files`, shared low-level modules, one documented set of annotation profiles, CI, this README and the MIT license.

Throughout, DevPilot never decides what to change and never runs arbitrary commands: the caller supplies every patch, and the only code it runs is the repository's tests, through two fixed commands and only on request.

## License

DevPilot MCP is released under the [MIT License](LICENSE).
