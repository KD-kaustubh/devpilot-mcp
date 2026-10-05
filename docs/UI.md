# Web UI: DevPilot Studio

DevPilot Studio is a local web app for DevPilot MCP. You ask questions about a repository in a chat, an AI model answers by calling DevPilot's tools, and every tool call appears live in an animated activity timeline. It runs only on your computer, at `http://127.0.0.1:8765`.

## Install and start

```powershell
pip install "devpilot-mcp[ui]"
devpilot-ui
```

`devpilot-ui` opens the browser automatically; use `--no-browser` to skip that and `--port 8800` to change the port.

The workspace comes from `DEVPILOT_WORKSPACE`, exactly as for the MCP server. When DevPilot is installed from PyPI, set it to the **absolute** path of the repository:

```powershell
$env:DEVPILOT_WORKSPACE = "D:\path\to\repo"     # macOS/Linux: export DEVPILOT_WORKSPACE=/path/to/repo
devpilot-ui
```

From a clone of this repository, settings in the project's `.env` are used too (see `.env.example`).

## Turn on AI chat (AI Pipe)

Chat uses an OpenAI-compatible model through [AI Pipe](https://aipipe.org):

1. Sign in at **https://aipipe.org/login** and copy your token.
2. Set `AIPIPE_TOKEN`, either in `.env` or in the terminal before `devpilot-ui`.
3. Restart `devpilot-ui`. The sidebar shows **AI · gpt-4.1-mini via aipipe.org**.

| Setting | Default | Meaning |
|---------|---------|---------|
| `AIPIPE_TOKEN` | *(unset)* | Your AI Pipe token. Without it, chat is off and quick actions still work. |
| `DEVPILOT_UI_MODEL` | `gpt-4.1-mini` | The model. `gpt-4.1-nano` is cheaper and uses less of your AI Pipe budget. |
| `DEVPILOT_UI_BASE_URL` | `https://aipipe.org/openai/v1` | Any OpenAI-compatible endpoint. |
| `DEVPILOT_UI_API_KEY` | *(unset)* | A key for another provider; used instead of `AIPIPE_TOKEN`. |
| `DEVPILOT_UI_PORT` | `8765` | The local port. |

AI Pipe gives each user a small weekly budget. Every question sends DevPilot's 18 tool descriptions plus the tool results to the model, so a question typically costs a few thousand tokens. Tool results sent to the model are capped at 12,000 characters each, and the chat remembers the last 4 questions.

## What you see

- **Sidebar, Overview tab:**
  - the workspace, its branch, clean or changed state, and whether AI is on;
  - **quick actions:** Analyze, Git status, Commits, Detect tests, Validate, Run tests;
  - the 18 tools grouped by access. Click a tool to run it.
- **Sidebar, Files tab:** a folder tree of the repository.
  - Click a file to open it in the **file viewer**, which has line numbers and highlighting.
  - **Ask about this file** puts the file into the chat box.
  - Secret files such as `.env` are listed with a lock and never opened.
- **Chat:**
  - suggested questions and streaming Markdown answers with highlighted code;
  - clickable file paths that open the file viewer;
  - a **Steps** panel inside each answer. It lists every tool the AI used, animated while running (with a live timer). Each step ends as done, failed, **blocked by DevPilot security** (for example a request for `.env`) or denied. It folds into one line ("Used 3 tools · 264 ms · 1 blocked") when the answer is finished; click a step for details.
- **Result cards instead of JSON.** Every tool's result appears as a visual card in the chat:
  - language bars and stat tiles;
  - Git status lists, a commit timeline and colored diffs;
  - search matches grouped by file, with the term highlighted;
  - ranked investigation evidence, GitHub issues and pull requests;
  - test results with a pass/fail bar, and a validation checklist.

  The details view adds the full result and a **Raw JSON** tab.
- **Tools (Ctrl+K):** run any of the 18 tools directly. Each tool gets a form built from its inputs, so no AI is needed.
- **Activity (header):** the full history of tool calls, opened only when you want it. The badge pulses while calls are running.
- **Changes (header):**
  - every patch applied in this session, with its files, the patch itself and an **Undo** button;
  - the current Git diff.
- **Approval dialog:** for changes and test runs, with a colored diff preview for `apply_patch`.
- Dark and light themes.

The file explorer, file viewer and Changes panel read the repository with *silent* read-only tool calls. These don't add messages to the chat. The server refuses to run any tool that writes files or runs code this way, so those always go through the approval dialog.

## Safety

- **Local only.** The server listens on `127.0.0.1`. Requests must use a localhost `Host` header (protection against DNS rebinding), and the WebSocket accepts only the Studio page's own origin, so other websites cannot drive it.
- **Approval for every risky call.** `apply_patch`, `revert_patch`, `run_tests` and `validate_repository` with `run_tests=true` wait for you to click **Approve** each time, whether the AI or a quick action asked. This is enforced by the backend, not only by the page. Closing the tab denies anything still waiting.
- **Same DevPilot boundaries.** The UI is an ordinary MCP client: it starts the DevPilot server over stdio and only calls its tools, so every rule in [Security](SECURITY.md) still applies. Secret files and `.git` stay unreadable, and paths cannot leave the workspace.
- **The token stays in the backend.** `AIPIPE_TOKEN` is never sent to the browser, never logged, and never passed to the DevPilot server process.
- **Untrusted content.** The AI is told that repository files and tool results are data, not instructions. That lowers, but cannot remove, the risk of prompt injection from repository content. The approval step is the real safeguard for anything that changes files or runs code.

## How it works

```
Browser (React)  ⇄  WebSocket + /api/status  ⇄  devpilot_ui (Starlette, 127.0.0.1)
                                                  ├─ agent loop ⇄ AI Pipe (OpenAI-compatible, streaming, tool calling)
                                                  └─ MCP client ⇄ stdio ⇄ DevPilot MCP server
```

| Module | Role |
|--------|------|
| `devpilot_ui/config.py` | Settings from the environment and `.env` |
| `devpilot_ui/mcp_session.py` | The long-lived MCP client session, and `ToolRunner`, the single path every tool call takes (events and the approval gate) |
| `devpilot_ui/agent.py` | The tool-calling loop: at most 8 tool rounds per question, streamed answers, capped results |
| `devpilot_ui/app.py` | The web server: `/api/status`, the `/ws` WebSocket and the built page |
| `ui/` | The React + TypeScript + Tailwind + Framer Motion frontend, built into `devpilot_ui/static/` |

One MCP session is kept for the whole run, so a change made with `apply_patch` can be undone with `revert_patch` later in the same session.

## Developing the UI

```powershell
cd ui
npm install
npm run build        # type check + bundle into devpilot_ui/static/
```

For live reloading, run the backend and the Vite dev server side by side:

```powershell
$env:DEVPILOT_UI_DEV_ORIGIN = "http://localhost:5173"; devpilot-ui --no-browser   # terminal 1
cd ui; npm run dev                                                                  # terminal 2, open http://localhost:5173
```

The backend tests are in `tests/test_ui.py`. They use a scripted fake model, so no network or token is needed. CI type-checks and builds the frontend on every push, and the release workflow bundles it into the wheel.

## Limitations

- **One user, one machine.** It is not designed to be hosted on a server or shared over a network.
- **Chat needs a model with tool calling**; `gpt-4.1-mini` and `gpt-4.1-nano` work through AI Pipe.
- **One request at a time per tab.** A new question waits until the current answer or action finishes.
- **Small screens.** The sidebar (with quick actions and the file explorer) hides below laptop width; chat, Ctrl+K, Activity and Changes still work.
