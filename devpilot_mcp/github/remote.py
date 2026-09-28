"""Discover the GitHub repository from the workspace's `origin` remote.

The owner and repository name are never taken from tool input: they come from
local Git configuration and are validated strictly, so they can only ever form
a plain `/repos/{owner}/{repo}` API path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from devpilot_mcp.tools.git import read_remote_url
from devpilot_mcp.workspace import Workspace, WorkspaceError

REMOTE_NAME = "origin"
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})

# GitHub account names: letters, digits and hyphens, not starting with a hyphen, at most 39 characters.
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
# Repository names: letters, digits, '.', '-' and '_', at most 100 characters.
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# scp-like syntax used by SSH remotes: [user@]host:path
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>.+)$")


class GitHubRemoteError(WorkspaceError):
    """The workspace's origin remote does not identify a github.com repository."""


class NoGitHubRemoteError(GitHubRemoteError):
    pass


class NonGitHubRemoteError(GitHubRemoteError):
    pass


class MalformedGitHubRemoteError(GitHubRemoteError):
    pass


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def _safe_host(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]", "", host)[:100] or "unknown"


def parse_github_remote(url: str) -> GitHubRepository:
    """Extract owner/repo from a github.com remote URL.

    Accepts https://github.com/OWNER/REPO(.git), git@github.com:OWNER/REPO(.git)
    and ssh://git@github.com/OWNER/REPO(.git). Error messages never include the
    URL itself, because remote URLs can embed credentials.
    """
    url = url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            parts.port  # raises ValueError for a malformed port
        except ValueError as exc:
            raise MalformedGitHubRemoteError(f"The '{REMOTE_NAME}' remote URL could not be parsed.") from exc
        if parts.scheme.lower() not in ("https", "http", "ssh", "git"):
            raise NonGitHubRemoteError(
                f"The '{REMOTE_NAME}' remote is not a network URL; only github.com repositories are supported."
            )
        path = parts.path
    elif (match := _SCP_LIKE.match(url)) and len(match["host"]) > 1:  # "C:\..." is a Windows path
        host, path = match["host"], match["path"]
    else:
        raise NonGitHubRemoteError(
            f"The '{REMOTE_NAME}' remote is a local path, not a github.com URL; only github.com repositories are supported."
        )

    host = host.lower()
    if host not in GITHUB_HOSTS:
        raise NonGitHubRemoteError(
            f"The '{REMOTE_NAME}' remote points to '{_safe_host(host)}', not github.com. "
            "Only github.com repositories are supported."
        )

    segments = path.strip("/").split("/")
    if len(segments) != 2:
        raise MalformedGitHubRemoteError(
            f"The '{REMOTE_NAME}' remote does not have the form github.com/OWNER/REPO."
        )
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _OWNER.match(owner) or not _REPO.match(name) or name in (".", ".."):
        raise MalformedGitHubRemoteError(
            f"The '{REMOTE_NAME}' remote does not contain a valid GitHub owner and repository name."
        )
    return GitHubRepository(owner=owner, name=name)


def discover_github_repository(workspace: Workspace) -> GitHubRepository:
    """The GitHub repository behind the workspace repository's `origin` remote.

    Raises:
        WorkspaceError: If the workspace is missing or not a Git repository root
            (from the Git tools), or `origin` is absent or not a github.com repository.
    """
    url = read_remote_url(workspace, REMOTE_NAME)
    if url is None:
        raise NoGitHubRemoteError(
            f"The workspace repository has no '{REMOTE_NAME}' remote, so no GitHub repository can be identified."
        )
    return parse_github_remote(url)
