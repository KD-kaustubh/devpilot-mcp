"""Read-only GitHub tools: github_repository, github_issues, github_pull_requests.

The repository is always the one behind the workspace's `origin` remote; tool
input can only choose a state filter and a bounded limit. GitHub responses are
normalized into stable schemas holding objective fields only (no raw payloads,
no issue/PR bodies, no comments).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from devpilot_mcp.github import client as gh
from devpilot_mcp.github.client import GitHubClient, GitHubResponseError
from devpilot_mcp.github.remote import GitHubRepository, discover_github_repository
from devpilot_mcp.tools.common import READ_ONLY, as_tool_error
from devpilot_mcp.workspace import Workspace, WorkspaceError

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_ISSUE_PAGES = 3  # the issues endpoint mixes in pull requests, so a few pages may be needed

State = Literal["open", "closed", "all"]

# --- Result models (these also become the tools' output schemas) -------------


class RateLimitInfo(BaseModel):
    limit: int | None
    remaining: int | None
    used: int | None
    reset_at: str | None
    resource: str | None


class GitHubRepositoryInfo(BaseModel):
    owner: str
    name: str
    full_name: str
    description: str | None
    html_url: str
    homepage: str | None
    default_branch: str
    visibility: str | None
    private: bool
    fork: bool
    archived: bool
    disabled: bool | None
    is_template: bool | None
    created_at: str | None
    updated_at: str | None
    pushed_at: str | None
    language: str | None
    license: str | None
    topics: list[str]
    stargazers_count: int
    watchers_count: int
    subscribers_count: int | None
    forks_count: int
    open_issues_count: int
    authenticated: bool
    rate_limit: RateLimitInfo | None


class GitHubIssue(BaseModel):
    number: int
    title: str
    state: str
    state_reason: str | None
    author: str | None
    labels: list[str]
    assignees: list[str]
    comments: int
    locked: bool | None
    created_at: str
    updated_at: str
    closed_at: str | None
    html_url: str


class GitHubIssueList(BaseModel):
    repository: str
    state: State
    limit: int
    issues: list[GitHubIssue]
    has_more: bool
    pull_requests_excluded: int
    pages_fetched: int
    authenticated: bool
    rate_limit: RateLimitInfo | None


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    state: str
    draft: bool | None
    author: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    merged_at: str | None
    source_branch: str
    source_repository: str | None
    target_branch: str
    target_repository: str | None
    labels: list[str]
    assignees: list[str]
    requested_reviewers: list[str]
    requested_teams: list[str]
    html_url: str


class GitHubPullRequestList(BaseModel):
    repository: str
    state: State
    limit: int
    pull_requests: list[GitHubPullRequest]
    has_more: bool
    authenticated: bool
    rate_limit: RateLimitInfo | None


# --- Normalization -----------------------------------------------------------


def _field(obj: Any, key: str, kind: type | tuple[type, ...], *, required: bool = True) -> Any:
    """Read one field, raising GitHubResponseError if it is missing (when required) or mistyped."""
    if not isinstance(obj, dict):
        raise GitHubResponseError("Unexpected GitHub response: expected a JSON object.")
    value = obj.get(key)
    if value is None:
        if required:
            raise GitHubResponseError(f"Unexpected GitHub response: '{key}' is missing.")
        return None
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if not isinstance(value, kinds) or (bool not in kinds and isinstance(value, bool)):
        raise GitHubResponseError(f"Unexpected GitHub response: '{key}' has an unexpected type.")
    return value


def _login(user: Any) -> str | None:
    """Login of a user object; GitHub uses null for deleted ("ghost") accounts."""
    return _field(user, "login", str) if user is not None else None


def _logins(obj: dict, key: str) -> list[str]:
    return [login for user in (_field(obj, key, list, required=False) or []) if (login := _login(user))]


def _label_names(obj: dict) -> list[str]:
    names = []
    for label in _field(obj, "labels", list, required=False) or []:
        names.append(label if isinstance(label, str) else _field(label, "name", str))
    return names


def _rate(response: gh.GitHubResponse) -> RateLimitInfo | None:
    rate = response.rate_limit
    return RateLimitInfo(**rate.__dict__) if rate else None


def normalize_repository(data: Any, response: gh.GitHubResponse) -> GitHubRepositoryInfo:
    license_obj = _field(data, "license", dict, required=False)
    return GitHubRepositoryInfo(
        owner=_field(_field(data, "owner", dict), "login", str),
        name=_field(data, "name", str),
        full_name=_field(data, "full_name", str),
        description=_field(data, "description", str, required=False),
        html_url=_field(data, "html_url", str),
        homepage=_field(data, "homepage", str, required=False) or None,
        default_branch=_field(data, "default_branch", str),
        visibility=_field(data, "visibility", str, required=False),
        private=_field(data, "private", bool),
        fork=_field(data, "fork", bool),
        archived=_field(data, "archived", bool, required=False) or False,
        disabled=_field(data, "disabled", bool, required=False),
        is_template=_field(data, "is_template", bool, required=False),
        created_at=_field(data, "created_at", str, required=False),
        updated_at=_field(data, "updated_at", str, required=False),
        pushed_at=_field(data, "pushed_at", str, required=False),
        language=_field(data, "language", str, required=False),
        license=_field(license_obj, "spdx_id", str, required=False) if license_obj else None,
        topics=[t for t in (_field(data, "topics", list, required=False) or []) if isinstance(t, str)],
        stargazers_count=_field(data, "stargazers_count", int),
        watchers_count=_field(data, "watchers_count", int),
        subscribers_count=_field(data, "subscribers_count", int, required=False),
        forks_count=_field(data, "forks_count", int),
        open_issues_count=_field(data, "open_issues_count", int),
        authenticated=response.authenticated,
        rate_limit=_rate(response),
    )


def is_pull_request(item: dict) -> bool:
    """The issues endpoint marks pull requests with a `pull_request` key."""
    return item.get("pull_request") is not None


def normalize_issue(item: Any) -> GitHubIssue:
    return GitHubIssue(
        number=_field(item, "number", int),
        title=_field(item, "title", str),
        state=_field(item, "state", str),
        state_reason=_field(item, "state_reason", str, required=False),
        author=_login(item.get("user")),
        labels=_label_names(item),
        assignees=_logins(item, "assignees"),
        comments=_field(item, "comments", int, required=False) or 0,
        locked=_field(item, "locked", bool, required=False),
        created_at=_field(item, "created_at", str),
        updated_at=_field(item, "updated_at", str),
        closed_at=_field(item, "closed_at", str, required=False),
        html_url=_field(item, "html_url", str),
    )


def _branch(item: dict, side: str) -> tuple[str, str | None]:
    ref = _field(item, side, dict)
    repo = _field(ref, "repo", dict, required=False)  # null when a fork has been deleted
    return _field(ref, "ref", str), (_field(repo, "full_name", str) if repo else None)


def normalize_pull_request(item: Any) -> GitHubPullRequest:
    source_branch, source_repository = _branch(item, "head")
    target_branch, target_repository = _branch(item, "base")
    return GitHubPullRequest(
        number=_field(item, "number", int),
        title=_field(item, "title", str),
        state=_field(item, "state", str),
        draft=_field(item, "draft", bool, required=False),
        author=_login(item.get("user")),
        created_at=_field(item, "created_at", str),
        updated_at=_field(item, "updated_at", str),
        closed_at=_field(item, "closed_at", str, required=False),
        merged_at=_field(item, "merged_at", str, required=False),
        source_branch=source_branch,
        source_repository=source_repository,
        target_branch=target_branch,
        target_repository=target_repository,
        labels=_label_names(item),
        assignees=_logins(item, "assignees"),
        requested_reviewers=_logins(item, "requested_reviewers"),
        requested_teams=[
            _field(team, "slug", str) for team in (_field(item, "requested_teams", list, required=False) or [])
        ],
        html_url=_field(item, "html_url", str),
    )


def _as_list(data: Any) -> list:
    if not isinstance(data, list):
        raise GitHubResponseError("Unexpected GitHub response: expected a JSON array.")
    return data


# --- Tool logic --------------------------------------------------------------


def _validate(state: str, limit: int) -> None:
    if state not in gh.STATES:
        raise WorkspaceError(f"state must be one of: {', '.join(gh.STATES)}.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise WorkspaceError(f"limit must be an integer between 1 and {MAX_LIMIT}.")


def github_repository(workspace: Workspace, client: GitHubClient) -> GitHubRepositoryInfo:
    """Metadata of the GitHub repository behind the workspace's origin remote."""
    repo = discover_github_repository(workspace)
    response = client.get_repository(repo)
    return normalize_repository(response.data, response)


def github_issues(
    workspace: Workspace, client: GitHubClient, state: str = "open", limit: int = DEFAULT_LIMIT
) -> GitHubIssueList:
    """Issues (pull requests excluded), newest first, fetching at most MAX_ISSUE_PAGES pages."""
    _validate(state, limit)
    repo: GitHubRepository = discover_github_repository(workspace)
    # One page of limit+1 usually settles has_more; pull requests mixed into
    # the results may require further pages, up to MAX_ISSUE_PAGES.
    per_page = min(gh.MAX_PER_PAGE, max(30, limit + 1))
    issues: list[GitHubIssue] = []
    excluded = 0
    has_more = False
    response = None
    page = 0
    for page in range(1, MAX_ISSUE_PAGES + 1):
        response = client.list_issues(repo, state=state, per_page=per_page, page=page)
        for item in _as_list(response.data):
            if not isinstance(item, dict):
                raise GitHubResponseError("Unexpected GitHub response: expected a JSON object.")
            if is_pull_request(item):
                excluded += 1
            else:
                issues.append(normalize_issue(item))
        if len(issues) > limit:
            has_more = True
            break
        if not response.has_next_page:
            break
        if len(issues) == limit or page == MAX_ISSUE_PAGES:
            has_more = True  # more pages exist; they may hold further issues
            break

    return GitHubIssueList(
        repository=repo.full_name,
        state=state,  # type: ignore[arg-type]
        limit=limit,
        issues=issues[:limit],
        has_more=has_more,
        pull_requests_excluded=excluded,
        pages_fetched=page,
        authenticated=response.authenticated,
        rate_limit=_rate(response),
    )


def github_pull_requests(
    workspace: Workspace, client: GitHubClient, state: str = "open", limit: int = DEFAULT_LIMIT
) -> GitHubPullRequestList:
    """Pull requests, newest first, from a single page of `limit` results."""
    _validate(state, limit)
    repo = discover_github_repository(workspace)
    response = client.list_pull_requests(repo, state=state, per_page=limit, page=1)
    pulls = [normalize_pull_request(item) for item in _as_list(response.data)]
    return GitHubPullRequestList(
        repository=repo.full_name,
        state=state,  # type: ignore[arg-type]
        limit=limit,
        pull_requests=pulls[:limit],
        has_more=response.has_next_page or len(pulls) > limit,
        authenticated=response.authenticated,
        rate_limit=_rate(response),
    )


# --- MCP registration --------------------------------------------------------


def register(server: MCPServer, workspace: Workspace, client: GitHubClient | None = None) -> None:
    """Expose the read-only GitHub tools; the repository comes from the workspace's origin remote."""
    client = client or GitHubClient()

    @server.tool(name="github_repository", annotations=READ_ONLY)
    def github_repository_tool() -> GitHubRepositoryInfo:
        """Metadata of the GitHub repository behind the workspace's `origin` remote (github.com only).

        Returns objective fields from GitHub: owner, name, description, URLs, default branch,
        visibility, fork/archived flags, timestamps, primary language, license (SPDX id), topics,
        and counts. Note GitHub's own semantics: `open_issues_count` includes open pull requests,
        `watchers_count` equals stars, and `subscribers_count` is the number of watchers.
        Set GITHUB_TOKEN for private repositories or higher rate limits.
        """
        try:
            return github_repository(workspace, client)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="github_issues", annotations=READ_ONLY)
    def github_issues_tool(
        state: State = "open",
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> GitHubIssueList:
        """Issues of the workspace's GitHub repository, newest first. Pull requests are excluded.

        GitHub's issues API also returns pull requests; they are left out here (counted in
        `pull_requests_excluded`) - use github_pull_requests for them. Each issue has number,
        title, state, author, labels, assignees, comment count, timestamps and URL; bodies and
        comments are not included. `has_more` is true when further issues may exist.

        Args:
            state: "open" (default), "closed" or "all".
            limit: Number of issues to return, 1-50 (default 10).
        """
        try:
            return github_issues(workspace, client, state, limit)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="github_pull_requests", annotations=READ_ONLY)
    def github_pull_requests_tool(
        state: State = "open",
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> GitHubPullRequestList:
        """Pull requests of the workspace's GitHub repository, newest first.

        Each pull request has number, title, state, draft flag, author, timestamps (including
        merged_at; null means not merged), source and target branch and repository, labels,
        assignees, requested reviewers/teams and URL. Bodies, comments, reviews, files and commits
        are not included. `has_more` is true when more pull requests exist.

        Args:
            state: "open" (default), "closed" or "all".
            limit: Number of pull requests to return, 1-50 (default 10).
        """
        try:
            return github_pull_requests(workspace, client, state, limit)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
