# Development

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
├── devpilot_ui/           # DevPilot Studio backend (optional [ui] extra; see docs/UI.md)
│   ├── app.py             # local web server: status API, chat/tool WebSocket, built page
│   ├── agent.py           # AI tool-calling loop (AI Pipe / any OpenAI-compatible model)
│   ├── mcp_session.py     # MCP client session and the approval gate for every tool call
│   └── config.py          # UI settings (token, model, port)
├── ui/                    # DevPilot Studio frontend (React, TypeScript, Tailwind, Framer Motion)
├── docs/                  # usage guide, tool reference, security, development
├── tests/                 # unittest suite (sandboxing, tool logic, in-process MCP client)
├── workspace/
│   └── sample_project/    # small demo repo to explore with the tools
├── .github/workflows/     # CI: tests on Windows and Linux + web UI build; PyPI publishing on release
├── .env.example
├── LICENSE
└── pyproject.toml
```

To add a tool group, create a new module in `devpilot_mcp/tools/` with a `register(server, workspace)` function, pick one of the annotation profiles in `tools/common.py`, then call it from `create_server()` in `server.py`.

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

Current result on Windows (Python 3.12): **389 tests, 383 passed, 6 skipped, 0 failed.** The skips are the five Windows symlink tests that need Developer Mode, and the real-pytest test, because pytest is not installed in DevPilot's environment. On Linux (Ubuntu, Python 3.12) the same suite passes with 3 skipped: the two Windows-only junction tests and the real-pytest test. pytest behaviour is still covered by parsing recorded output and by a real "pytest not installed" run.

**Continuous integration:** `.github/workflows/test.yml` installs the package with the `[ui]` extra and runs the same `unittest` command on every push and pull request, on Ubuntu and Windows with Python 3.11 and 3.12. A second job type-checks and builds the web UI. The web UI's own development steps are in [Web UI](UI.md#developing-the-ui).

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
10. **v0.1.1 security fix:** after external testing, the read and search tools never return secret files or `.git` internals, and directory walks never enter Windows junctions.
11. **DevPilot Studio (v0.2.0):** a local web UI. It chats through AI Pipe, shows every tool call in an animated timeline, and asks for approval before any change or test run.

Throughout, DevPilot never decides what to change and never runs arbitrary commands: the caller supplies every patch, and the only code it runs is the repository's tests, through two fixed commands and only on request.
