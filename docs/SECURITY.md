# Security

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
- **Writes:** a patch target may not contain a symlink or junction **anywhere on its path**, even one that points back inside the workspace, and the final path must resolve to its literal location. See [Validation pipeline](TOOLS.md#validation-pipeline).

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
- **Git command allowlist:** only fixed read-only forms of `status`, `log`, `diff`, `diff-files`, `branch`, `rev-parse` and `remote get-url` can run, with the fsmonitor hook, external diff drivers, textconv filters, the pager and network protocols disabled. See [Read-only execution boundary](TOOLS.md#read-only-execution-boundary).
- **No arbitrary shell executor.** No tool accepts a command, executable, argument list or shell string. The only test commands are `python -m pytest -p no:cacheprovider` and `python -m unittest` (optionally `discover -s <dir>`), launched by absolute interpreter path. See [Security restrictions](TOOLS.md#security-restrictions).

### Controlled patching

- **Strict parser:** only standard unified diffs are accepted. Binary patches, renames, mode changes, quoted paths and anything that is not part of a diff are rejected, and hunks must match exactly at their stated lines.
- **Atomic apply and rollback:** every file is validated and staged first, then swapped in with `os.replace`. Any failure rolls back every completed step, so a patch applies completely or not at all.
- **Protected files:** `.git` contents, `.env` files (except `.env.example`), private keys and credential files can never be written.
- **Safe revert:** a revert is refused, touching nothing, if any file changed since the patch was applied.

See [Controlled code modification](TOOLS.md#controlled-code-modification).

### Secrets and environment

- **GitHub token:** `GITHUB_TOKEN` is read at request time and placed only in the `Authorization` header. It is never returned, logged or included in error text.
- **Secret files:** likely secret files (`.env*`, keys, credentials) are never used as investigation evidence and can never be patched.
- **Test environment:** interpreter and pytest injection variables, every `GIT_*` variable and every variable whose name looks secret are removed before tests run, so test code cannot read `GITHUB_TOKEN`. This is a name-based blocklist, not a sandbox.
- **Output redaction (best-effort, not guaranteed):** test output and investigation evidence are scanned for known secret patterns and the values of removed variables, which are replaced with `[REDACTED]`. Secrets in other forms can still appear.

### GitHub access

- HTTPS to `api.github.com` only, GET only, three fixed endpoints. Tools cannot supply a URL, host, path, method or header.
- **Redirects are never followed**, so the `Authorization` header cannot reach another host.
- See [Read-only HTTP boundary](TOOLS.md#read-only-http-boundary).

### Bounded operations

Every Git and test process has a timeout and an output cap, every HTTP request has a 15-second timeout and a response-size cap, and every list, scan, excerpt and diff has a documented limit. Limits are constants at the top of each module.

### What is not protected

- Test code run by `run_tests` runs with the permissions of the DevPilot process. It can read and write files and use the network; there is **no network or filesystem isolation**.
- Secret redaction and environment sanitization are **best-effort**; they do not guarantee that no secret reaches a client.
- Clean/smudge filter drivers configured in a repository's own `.git/config` cannot be disabled generically.

The full list is under [Known Limitations](#known-limitations).

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

## Security Testing

The suite builds a temporary workspace with a `secret.txt` just outside it. It checks the path-escape attempts listed under [Workspace boundary](#workspace-boundary) for every path-taking tool, including `search_files` with a `path`, every tool's normal and error cases, and full round trips through an in-process MCP client. A test pins the annotation profile of all 18 tools.

- **Git:** each blocked route for launching programs (fsmonitor, external diff, textconv, pager, …) is shown to be live with plain `git` and blocked in DevPilot, and a snapshot of every file, `.git` included, proves that nothing is written.
- **GitHub:** tool output, every error path, captured logs, stdout/stderr and the workspace files are checked for the token, and redirects and non-`api.github.com` hosts are refused.
- **Investigation:** tests use a fake GitHub client, real throwaway Git repositories and hashes of every file before and after.
- **Patching:** the patch tests attempt the full set of malicious patches (traversal, absolute, drive and UNC paths, symlinks and Windows junctions, protected files, unexpected metadata, oversized and malformed patches). Each one takes a byte-level snapshot before and after. Failures are forced partway through multi-file patches to prove rollback.
- **Testing and validation:** the Phase 8 tests run real unittest suites in throwaway projects, covering passing, failing, skipped, timed-out, truncated, side-effecting and no-tests runs. They also try shell and command injection payloads (`; whoami`, `&& powershell …`, `cmd /c …`, `../../…`, absolute and UNC paths, tampered argument vectors) and check that no process starts, and that the environment and output leak no secrets.
