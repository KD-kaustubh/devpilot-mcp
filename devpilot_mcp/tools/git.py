"""Read-only Git tools: git_status, git_log, git_diff, git_branch.

Git runs as a subprocess, so every invocation goes through `_run_git`, which
is the execution boundary:

- Only subcommands in ALLOWED_SUBCOMMANDS can run, and they are only ever used
  in read-only forms built here. Tool input is limited to a validated integer
  (`limit`), a boolean (`staged`) and a workspace-validated path passed after
  `--` with literal pathspecs, so input can never become a Git option or command.
- An argument list is used with shell=False, and cwd is always the workspace root.
- The environment is sanitized: inherited GIT_* variables (GIT_DIR,
  GIT_WORK_TREE, GIT_CONFIG_PARAMETERS, ...) are dropped, and
  GIT_CEILING_DIRECTORIES stops Git from using a repository in a parent directory.
- Programs that read-only commands could still launch from repository config
  are disabled: fsmonitor hook, external diff, textconv, pager, GPG signature
  checks and lazy fetches. GIT_OPTIONAL_LOCKS=0 keeps `git status` from
  rewriting the index, so no file under the workspace is modified.
- Each run has a timeout, and at most `max_bytes` of stdout is ever read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from devpilot_mcp.tools.common import READ_ONLY, as_tool_error
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

GIT_TIMEOUT_SECONDS = 15
MAX_GIT_OUTPUT_BYTES = 2_000_000  # hard cap on stdout read from any Git process
MAX_DIFF_BYTES = 60_000  # diff text returned to the client
DEFAULT_LOG_LIMIT = 10
MAX_LOG_LIMIT = 50
MAX_FILE_ENTRIES = 200  # per file list in git_status / git_diff
MAX_BRANCHES = 100
MAX_COMMIT_BODY_CHARS = 2_000
MAX_ERROR_CHARS = 300

ALLOWED_SUBCOMMANDS = frozenset({"status", "log", "diff", "diff-files", "branch", "rev-parse"})

# Applied to every invocation as `git -c key=value`.
SAFE_CONFIG = (
    "core.fsmonitor=false",  # a configured fsmonitor hook is an arbitrary command
    "core.quotePath=false",
    "color.ui=false",
    "log.showSignature=false",  # would launch gpg
    "protocol.allow=never",  # no network access, ever
)

ChangeType = Literal["added", "copied", "deleted", "modified", "renamed", "type_changed", "unmerged", "unknown"]
_CHANGE_NAMES: dict[str, ChangeType] = {
    "A": "added", "C": "copied", "D": "deleted", "M": "modified",
    "R": "renamed", "T": "type_changed", "U": "unmerged",
}  # fmt: skip


class GitError(WorkspaceError):
    """Base class for Git tool failures; the message is safe to show the client."""


class GitUnavailableError(GitError):
    pass


class GitCommandNotAllowedError(GitError):
    pass


class GitTimeoutError(GitError):
    pass


class GitCommandError(GitError):
    pass


class GitOutputError(GitError):
    pass


class NotAGitRepositoryError(GitError):
    pass


# --- Result models (these also become the tools' output schemas) -------------


class GitFileChange(BaseModel):
    path: str
    change: ChangeType
    original_path: str | None = None


class GitStatus(BaseModel):
    branch: str | None
    detached: bool
    head_commit: str | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    clean: bool
    staged: list[GitFileChange]
    unstaged: list[GitFileChange]
    untracked: list[str]
    deleted: list[str]
    conflicted: list[str]
    counts: dict[str, int]
    complete: bool
    truncated_fields: list[str]


class GitCommit(BaseModel):
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    parents: list[str]
    subject: str
    body: str
    body_truncated: bool


class GitLog(BaseModel):
    limit: int
    commits: list[GitCommit]
    has_more: bool


class GitDiffFile(BaseModel):
    path: str
    change: ChangeType
    original_path: str | None = None
    additions: int | None
    deletions: int | None
    binary: bool


class GitDiff(BaseModel):
    staged: bool
    path: str
    files: list[GitDiffFile]
    files_changed: int
    additions: int
    deletions: int
    diff: str
    diff_bytes: int
    truncated: bool
    warnings: list[str]


class GitBranch(BaseModel):
    name: str
    commit: str
    upstream: str | None
    current: bool


class GitBranches(BaseModel):
    current_branch: str | None
    detached: bool
    branches: list[GitBranch]
    total_branches: int
    truncated: bool


# --- Execution boundary ------------------------------------------------------

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows


@dataclass
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str
    truncated: bool  # stdout exceeded max_bytes and the process was stopped early


def _execute(argv: list[str], cwd: Path, env: dict[str, str], *, timeout: float, max_bytes: int) -> GitResult:
    """Run `argv` without a shell, reading at most `max_bytes` of stdout."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError("Git executable not found.") from exc

    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    stderr_head = bytearray()

    def drain_stderr() -> None:
        # Drain continuously so a chatty stderr can't fill the pipe and block Git.
        while chunk := proc.stderr.read(65536):
            if len(stderr_head) < 65536:
                stderr_head.extend(chunk)

    timer = threading.Timer(timeout, kill_on_timeout)
    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stdout = bytearray()
    truncated = False
    with proc:
        timer.start()
        stderr_thread.start()
        try:
            while chunk := proc.stdout.read(65536):
                stdout.extend(chunk)
                if len(stdout) > max_bytes:
                    truncated = True
                    proc.kill()
                    break
            proc.wait()
            stderr_thread.join(timeout=5)
        finally:
            timer.cancel()

    if timed_out.is_set():
        raise GitTimeoutError(f"Git command timed out after {timeout:g} seconds.")
    return GitResult(
        returncode=proc.returncode,
        stdout=bytes(stdout[:max_bytes]),
        stderr=stderr_head.decode("utf-8", "replace"),
        truncated=truncated,
    )


def _git_environment(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        GIT_CEILING_DIRECTORIES=str(root.parent),
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        LC_ALL="C",
    )
    return env


def _workspace_root(workspace: Workspace) -> Path:
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")
    return root


def _clean_error(stderr: str, root: Path) -> str:
    """First meaningful stderr line, with the absolute workspace path masked."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    line = next((l for l in lines if l.startswith(("fatal:", "error:"))), lines[0] if lines else "no error output")
    for form in {str(root), root.as_posix()}:
        line = line.replace(form, "<workspace>")
    return line[:MAX_ERROR_CHARS]


def _run_git(
    workspace: Workspace, subcommand: str, *args: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES, check: bool = True
) -> GitResult:
    """Run one allow-listed, read-only Git subcommand in the workspace root."""
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise GitCommandNotAllowedError(f"Git subcommand '{subcommand}' is not permitted.")
    root = _workspace_root(workspace)
    git = shutil.which("git")
    if git is None:
        raise GitUnavailableError("Git executable not found on PATH.")

    config = [item for pair in SAFE_CONFIG for item in ("-c", pair)]
    argv = [git, "--no-pager", "--literal-pathspecs", *config, subcommand, *args]
    result = _execute(argv, root, _git_environment(root), timeout=GIT_TIMEOUT_SECONDS, max_bytes=max_bytes)
    if check and result.returncode != 0 and not result.truncated:
        raise GitCommandError(f"git {subcommand} failed: {_clean_error(result.stderr, root)}")
    return result


def _require_repository(workspace: Workspace) -> None:
    """Fail unless the workspace root is itself the top level of a Git work tree."""
    root = _workspace_root(workspace)
    result = _run_git(workspace, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        if "not a git repository" in result.stderr.lower():
            raise NotAGitRepositoryError(
                "The workspace is not a Git repository. The configured workspace itself must be the "
                "repository root; parent directories are not searched."
            )
        raise GitCommandError(f"Could not inspect the Git repository: {_clean_error(result.stderr, root)}")
    toplevel = Path(result.stdout.decode("utf-8", "replace").strip())
    if toplevel.resolve() != root:
        raise NotAGitRepositoryError("The workspace is inside a Git work tree but is not its root.")


def _has_commits(workspace: Workspace) -> bool:
    return _run_git(workspace, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _change(code: str) -> ChangeType:
    return _CHANGE_NAMES.get(code[:1], "unknown")


# --- git_status --------------------------------------------------------------


def git_status(workspace: Workspace) -> GitStatus:
    """Working-tree state from `git status --porcelain=v2 -z`."""
    _require_repository(workspace)
    result = _run_git(
        workspace, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all",
        "--find-renames", "--ignore-submodules=dirty",
    )  # fmt: skip

    headers: dict[str, str] = {}
    staged: list[GitFileChange] = []
    unstaged: list[GitFileChange] = []
    untracked: list[str] = []
    conflicted: list[str] = []

    records = result.stdout.split(b"\0")
    if result.truncated:
        records = records[:-1]  # the last record may be cut off
    index = 0
    try:
        while index < len(records):
            record = _decode(records[index])
            index += 1
            if not record:
                continue
            kind = record[0]
            if kind == "#":
                key, _, value = record[2:].partition(" ")
                headers[key] = value
            elif kind == "1":
                fields = record.split(" ", 8)
                xy, path = fields[1], fields[8]
                if xy[0] != ".":
                    staged.append(GitFileChange(path=path, change=_change(xy[0])))
                if xy[1] != ".":
                    unstaged.append(GitFileChange(path=path, change=_change(xy[1])))
            elif kind == "2":
                fields = record.split(" ", 9)
                xy, path = fields[1], fields[9]
                original = _decode(records[index])  # -z puts the original path in its own record
                index += 1
                if xy[0] != ".":
                    staged.append(GitFileChange(path=path, change=_change(xy[0]), original_path=original))
                if xy[1] != ".":
                    unstaged.append(GitFileChange(path=path, change=_change(xy[1])))
            elif kind == "u":
                conflicted.append(record.split(" ", 10)[10])
            elif kind == "?":
                untracked.append(record[2:])
            elif kind != "!":
                raise GitOutputError("Unexpected output from git status.")
    except IndexError as exc:
        if not result.truncated:
            raise GitOutputError("Unexpected output from git status.") from exc

    head = headers.get("branch.head")
    oid = headers.get("branch.oid")
    ahead = behind = None
    if ab := headers.get("branch.ab"):
        try:
            ahead_text, behind_text = ab.split()
            ahead, behind = int(ahead_text.lstrip("+")), int(behind_text.lstrip("-"))
        except ValueError as exc:
            raise GitOutputError("Unexpected branch information from git status.") from exc

    by_path = lambda change: change.path  # noqa: E731
    staged.sort(key=by_path)
    unstaged.sort(key=by_path)
    untracked.sort()
    conflicted.sort()
    deleted = sorted({c.path for c in staged + unstaged if c.change == "deleted"})

    truncated: list[str] = []

    def cap(field: str, items: list) -> list:
        if len(items) > MAX_FILE_ENTRIES:
            truncated.append(field)
            return items[:MAX_FILE_ENTRIES]
        return items

    return GitStatus(
        branch=None if head in (None, "(detached)") else head,
        detached=head == "(detached)",
        head_commit=None if oid in (None, "(initial)") else oid,
        upstream=headers.get("branch.upstream"),
        ahead=ahead,
        behind=behind,
        clean=not (staged or unstaged or untracked or conflicted),
        staged=cap("staged", staged),
        unstaged=cap("unstaged", unstaged),
        untracked=cap("untracked", untracked),
        deleted=cap("deleted", deleted),
        conflicted=cap("conflicted", conflicted),
        counts={
            "staged": len(staged), "unstaged": len(unstaged), "untracked": len(untracked),
            "deleted": len(deleted), "conflicted": len(conflicted),
        },  # fmt: skip
        complete=not result.truncated,
        truncated_fields=truncated,
    )


# --- git_log -----------------------------------------------------------------

_LOG_FORMAT = "%x1f".join(("%H", "%h", "%an", "%ae", "%aI", "%P", "%s", "%b"))


def git_log(workspace: Workspace, limit: int = DEFAULT_LOG_LIMIT) -> GitLog:
    """The most recent commits reachable from HEAD, newest first."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LOG_LIMIT:
        raise WorkspaceError(f"limit must be an integer between 1 and {MAX_LOG_LIMIT}.")
    _require_repository(workspace)
    if not _has_commits(workspace):
        return GitLog(limit=limit, commits=[], has_more=False)

    # Ask for one extra commit to learn whether older history exists.
    result = _run_git(workspace, "log", f"--max-count={limit + 1}", f"--format={_LOG_FORMAT}", "-z", "--no-color")
    commits: list[GitCommit] = []
    for record in _decode(result.stdout).split("\0"):
        if not record.strip():
            continue
        fields = record.lstrip("\n").split("\x1f", 7)
        if len(fields) != 8:
            raise GitOutputError("Unexpected output from git log.")
        full, short, name, email, date, parents, subject, body = fields
        body = body.strip()
        commits.append(
            GitCommit(
                hash=full,
                short_hash=short,
                author_name=name,
                author_email=email,
                date=date,
                parents=parents.split(),
                subject=subject,
                body=body[:MAX_COMMIT_BODY_CHARS],
                body_truncated=len(body) > MAX_COMMIT_BODY_CHARS,
            )
        )
    return GitLog(limit=limit, commits=commits[:limit], has_more=len(commits) > limit)


# --- git_diff ----------------------------------------------------------------


def _parse_name_status(raw: bytes) -> list[tuple[ChangeType, str, str | None]]:
    tokens = [_decode(t) for t in raw.split(b"\0")]
    entries = []
    index = 0
    try:
        while index < len(tokens) and tokens[index]:
            code = tokens[index]
            if code[0] in "RC":
                entries.append((_change(code), tokens[index + 2], tokens[index + 1]))
                index += 3
            else:
                entries.append((_change(code), tokens[index + 1], None))
                index += 2
    except IndexError as exc:
        raise GitOutputError("Unexpected output from git diff.") from exc
    return entries


def _parse_numstat(raw: bytes) -> dict[str, tuple[int | None, int | None]]:
    """Map path -> (additions, deletions); binary files have (None, None)."""
    tokens = [_decode(t) for t in raw.split(b"\0")]
    stats = {}
    index = 0
    try:
        while index < len(tokens) and tokens[index]:
            added, deleted, path = tokens[index].split("\t", 2)
            index += 1
            if not path:  # rename/copy: the old and new paths follow as separate records
                path = tokens[index + 1]
                index += 2
            stats[path] = (None, None) if added == "-" else (int(added), int(deleted))
    except (IndexError, ValueError) as exc:
        raise GitOutputError("Unexpected output from git diff.") from exc
    return stats


def git_diff(workspace: Workspace, staged: bool = False, path: str = ".") -> GitDiff:
    """Unstaged (working tree vs index) or staged (index vs HEAD) changes."""
    if not isinstance(staged, bool):
        raise WorkspaceError("staged must be true or false.")
    # The path may name a deleted file, so it only has to be inside the workspace, not exist.
    pathspec = workspace.relative(workspace.resolve(path))
    _require_repository(workspace)

    options = ["--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty"]
    if staged:
        command = "diff"
        options += ["--cached", "--find-renames"]
    else:
        # Porcelain `git diff` refreshes and rewrites .git/index even with
        # GIT_OPTIONAL_LOCKS=0, so unstaged changes use the plumbing command,
        # which never writes.
        command = "diff-files"
    names = _run_git(workspace, command, *options, "--name-status", "-z", "--", pathspec)
    numstat = _run_git(workspace, command, *options, "--numstat", "-z", "--", pathspec)
    patch = _run_git(workspace, command, *options, "-p", "--", pathspec, max_bytes=MAX_DIFF_BYTES)

    stats = _parse_numstat(numstat.stdout)
    entries = _parse_name_status(names.stdout)
    if not staged:
        # Without an index refresh, diff-files also names files whose cached stat
        # data is merely stale; numstat compares contents and omits those.
        entries = [entry for entry in entries if entry[1] in stats]
    files = []
    for change, file_path, original in sorted(entries, key=lambda e: e[1]):
        additions, deletions = stats.get(file_path, (None, None))
        files.append(
            GitDiffFile(
                path=file_path,
                change=change,
                original_path=original,
                additions=additions,
                deletions=deletions,
                binary=file_path in stats and additions is None,
            )
        )

    warnings = []
    text = _decode(patch.stdout)
    if patch.truncated:
        text = text[: text.rfind("\n") + 1]  # don't end mid-line
        warnings.append(
            f"Diff text exceeds {MAX_DIFF_BYTES:,} bytes and was truncated; "
            "call git_diff with a narrower path to see the rest."
        )
    if len(files) > MAX_FILE_ENTRIES:
        warnings.append(f"Only the first {MAX_FILE_ENTRIES} of {len(files)} changed files are listed.")

    return GitDiff(
        staged=staged,
        path=pathspec,
        files=files[:MAX_FILE_ENTRIES],
        files_changed=len(files),
        additions=sum(f.additions or 0 for f in files),
        deletions=sum(f.deletions or 0 for f in files),
        diff=text,
        diff_bytes=len(text.encode("utf-8")),
        truncated=patch.truncated or len(files) > MAX_FILE_ENTRIES,
        warnings=warnings,
    )


# --- git_branch --------------------------------------------------------------


def git_branch(workspace: Workspace) -> GitBranches:
    """The current branch and all local branches (sorted by name)."""
    _require_repository(workspace)
    current = _decode(_run_git(workspace, "branch", "--show-current").stdout).strip()
    listing = _run_git(
        workspace, "branch", "--list", "--no-color", "--format=%(refname)%00%(objectname:short)%00%(upstream:short)"
    )

    branches = []
    for line in _decode(listing.stdout).splitlines():
        if not line:
            continue
        fields = line.split("\0")
        if len(fields) != 3:
            raise GitOutputError("Unexpected output from git branch.")
        refname, commit, upstream = fields
        if not refname.startswith("refs/heads/"):  # e.g. the "(HEAD detached at ...)" pseudo-entry
            continue
        name = refname.removeprefix("refs/heads/")
        branches.append(GitBranch(name=name, commit=commit, upstream=upstream or None, current=name == current))

    branches.sort(key=lambda b: b.name)
    return GitBranches(
        current_branch=current or None,
        detached=not current,
        branches=branches[:MAX_BRANCHES],
        total_branches=len(branches),
        truncated=len(branches) > MAX_BRANCHES,
    )


# --- MCP registration --------------------------------------------------------


def register(server: MCPServer, workspace: Workspace) -> None:
    """Expose the read-only Git tools on `server`, bound to the workspace repository."""

    @server.tool(name="git_status", annotations=READ_ONLY)
    def git_status_tool() -> GitStatus:
        """Current Git working-tree state of the workspace repository.

        Returns the branch (None when detached), HEAD commit, upstream and ahead/behind counts,
        whether the tree is clean, and sorted file lists: staged (index vs HEAD), unstaged
        (working tree vs index), untracked, deleted and conflicted. Each change has a `change`
        type (added, modified, deleted, renamed, ...). Lists hold at most 200 paths; `counts`
        gives full totals and `truncated_fields` names cut lists. Paths are relative to the
        workspace root. The workspace itself must be the repository root.
        """
        try:
            return git_status(workspace)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="git_log", annotations=READ_ONLY)
    def git_log_tool(
        limit: Annotated[int, Field(ge=1, le=MAX_LOG_LIMIT)] = DEFAULT_LOG_LIMIT,
    ) -> GitLog:
        """Recent commits reachable from HEAD, newest first.

        Each commit has its full and short hash, author name and email, ISO 8601 author date,
        parent hashes, subject and body (bodies over 2,000 characters are cut). `has_more` is
        true when older commits exist.

        Args:
            limit: Number of commits to return, 1-50 (default 10).
        """
        try:
            return git_log(workspace, limit)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="git_diff", annotations=READ_ONLY)
    def git_diff_tool(staged: bool = False, path: str = ".") -> GitDiff:
        """Current uncommitted changes as structured file stats plus unified diff text.

        By default shows unstaged changes (working tree vs index); with staged=true shows
        staged changes (index vs HEAD). Untracked files are not included (see git_status).
        `files` lists each changed file with its change type and added/deleted line counts.
        The diff text is capped at 60,000 bytes; `truncated` and `warnings` say when output
        was cut, in which case narrow the request with `path`.

        Args:
            staged: false for unstaged changes (default), true for staged changes.
            path: Limit the diff to this file or directory, relative to the workspace root.
        """
        try:
            return git_diff(workspace, staged, path)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="git_branch", annotations=READ_ONLY)
    def git_branch_tool() -> GitBranches:
        """The current branch and the local branches (name, short commit, upstream), sorted by name.

        `current_branch` is None and `detached` is true when HEAD is detached. At most 100
        branches are listed; `total_branches` gives the full count. Nothing is switched or created.
        """
        try:
            return git_branch(workspace)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
