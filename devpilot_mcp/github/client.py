"""Minimal read-only GitHub REST API client.

This is the only code that talks to GitHub, and it is deliberately narrow:

- Three operations only (repository, issues, pulls), built from fixed
  endpoint templates and a GitHubRepository validated by `remote`. There is
  no way to request an arbitrary URL, path, header or query parameter.
- GET only: the transport has no method parameter, and the stdlib transport
  hard-codes GET with no request body.
- HTTPS to api.github.com only, with a timeout, no retries and a cap on how
  much of a response is read.
- Redirects are never followed, so the Authorization header can't be
  forwarded to another host; a redirect is reported as an error instead.
- GITHUB_TOKEN is read from the environment at request time, placed only in
  the Authorization header here, and never stored, logged or echoed in errors.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Any
from urllib.parse import quote, urlencode

from devpilot_mcp import __version__
from devpilot_mcp.github.remote import REMOTE_NAME, GitHubRepository
from devpilot_mcp.workspace import WorkspaceError

API_BASE_URL = "https://api.github.com"
API_VERSION = "2026-03-10"  # latest version listed by GET https://api.github.com/versions
TOKEN_ENV_VAR = "GITHUB_TOKEN"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 10_000_000
MAX_PER_PAGE = 100  # documented maximum for list endpoints
MAX_PAGE = 10
MAX_ERROR_CHARS = 200

STATES = ("open", "closed", "all")

# The complete set of operations: name -> (path template, allowed query parameters).
ENDPOINTS: dict[str, tuple[str, frozenset[str]]] = {
    "repository": ("/repos/{owner}/{repo}", frozenset()),
    "issues": ("/repos/{owner}/{repo}/issues", frozenset({"state", "per_page", "page"})),
    "pulls": ("/repos/{owner}/{repo}/pulls", frozenset({"state", "per_page", "page"})),
}


class GitHubError(WorkspaceError):
    """Base class for GitHub failures; the message is safe to show the client."""


class GitHubAuthError(GitHubError):
    pass


class GitHubForbiddenError(GitHubError):
    pass


class GitHubRateLimitError(GitHubError):
    pass


class GitHubNotFoundError(GitHubError):
    pass


class GitHubRequestError(GitHubError):
    pass


class GitHubServerError(GitHubError):
    pass


class GitHubTimeoutError(GitHubError):
    pass


class GitHubConnectionError(GitHubError):
    pass


class GitHubResponseError(GitHubError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-cased names
    body: bytes


# (url, headers, timeout) -> response. Deliberately has no method or body parameter.
Transport = Callable[[str, Mapping[str, str], float], HttpResponse]


# --- stdlib transport --------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so credentials are never re-sent anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # urllib then raises HTTPError with the 3xx status


def _open(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Perform one GET without following redirects, reading at most MAX_RESPONSE_BYTES."""
    opener = urllib.request.build_opener(_NoRedirects)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            status, response_headers = response.status, response.headers
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:  # non-2xx, including refused redirects
        status, response_headers = exc.code, exc.headers
        body = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp else b""
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise GitHubTimeoutError(f"GitHub did not respond within {timeout:g} seconds.") from None
        raise GitHubConnectionError(f"Could not connect to GitHub ({type(exc.reason).__name__}).") from None
    except (TimeoutError, socket.timeout):
        raise GitHubTimeoutError(f"GitHub did not respond within {timeout:g} seconds.") from None
    except (HTTPException, OSError) as exc:
        raise GitHubConnectionError(f"Connection to GitHub failed ({type(exc).__name__}).") from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise GitHubResponseError("GitHub's response was unexpectedly large and was not processed.")
    return HttpResponse(status, {k.lower(): v for k, v in (response_headers or {}).items()}, body)


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Default transport: HTTPS GET to the GitHub API only."""
    if not url.startswith(API_BASE_URL + "/"):
        raise GitHubRequestError("Refusing to contact a host other than the GitHub API over HTTPS.")
    return _open(url, headers, timeout)


# --- Client ------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimit:
    limit: int | None
    remaining: int | None
    used: int | None
    reset_at: str | None
    resource: str | None


@dataclass(frozen=True)
class GitHubResponse:
    data: Any
    has_next_page: bool
    rate_limit: RateLimit | None
    authenticated: bool


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _rate_limit(headers: Mapping[str, str]) -> RateLimit | None:
    if "x-ratelimit-limit" not in headers:
        return None
    reset = _int_header(headers, "x-ratelimit-reset")
    reset_at = (
        datetime.fromtimestamp(reset, tz=timezone.utc).isoformat().replace("+00:00", "Z") if reset is not None else None
    )
    return RateLimit(
        limit=_int_header(headers, "x-ratelimit-limit"),
        remaining=_int_header(headers, "x-ratelimit-remaining"),
        used=_int_header(headers, "x-ratelimit-used"),
        reset_at=reset_at,
        resource=headers.get("x-ratelimit-resource"),
    )


def _has_next_page(headers: Mapping[str, str]) -> bool:
    # Only the presence of rel="next" is used; URLs from the header are never requested.
    return any('rel="next"' in part for part in headers.get("link", "").split(","))


class GitHubClient:
    """Read-only client for three GitHub REST operations on one repository at a time."""

    def __init__(self, transport: Transport | None = None, timeout: float = TIMEOUT_SECONDS) -> None:
        self._transport = transport or urllib_transport
        self._timeout = timeout

    def get_repository(self, repo: GitHubRepository) -> GitHubResponse:
        return self._get("repository", repo, {})

    def list_issues(self, repo: GitHubRepository, *, state: str, per_page: int, page: int = 1) -> GitHubResponse:
        return self._get("issues", repo, self._list_params(state, per_page, page))

    def list_pull_requests(
        self, repo: GitHubRepository, *, state: str, per_page: int, page: int = 1
    ) -> GitHubResponse:
        return self._get("pulls", repo, self._list_params(state, per_page, page))

    @staticmethod
    def _list_params(state: str, per_page: int, page: int) -> dict[str, str | int]:
        if state not in STATES:
            raise GitHubRequestError(f"state must be one of: {', '.join(STATES)}.")
        for name, value, maximum in (("per_page", per_page, MAX_PER_PAGE), ("page", page, MAX_PAGE)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise GitHubRequestError(f"{name} must be an integer between 1 and {maximum}.")
        return {"state": state, "per_page": per_page, "page": page}

    def _get(self, operation: str, repo: GitHubRepository, params: Mapping[str, str | int]) -> GitHubResponse:
        if operation not in ENDPOINTS:
            raise GitHubRequestError(f"GitHub operation '{operation}' is not permitted.")
        template, allowed_params = ENDPOINTS[operation]
        if not set(params) <= allowed_params:
            raise GitHubRequestError(f"Query parameters not permitted for '{operation}'.")
        if not isinstance(repo, GitHubRepository):
            raise GitHubRequestError("A repository discovered from the workspace is required.")

        path = template.format(owner=quote(repo.owner, safe=""), repo=quote(repo.name, safe=""))
        url = API_BASE_URL + path + (f"?{urlencode(params)}" if params else "")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"devpilot-mcp/{__version__}",
        }
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = self._transport(url, headers, self._timeout)
        finally:
            headers.pop("Authorization", None)
        return self._handle(response, repo, authenticated=bool(token), token=token)

    def _handle(self, response: HttpResponse, repo: GitHubRepository, *, authenticated: bool, token: str) -> GitHubResponse:
        status, headers = response.status, response.headers
        rate = _rate_limit(headers)
        if 200 <= status < 300:
            try:
                data = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise GitHubResponseError("GitHub returned a response that is not valid JSON.") from None
            return GitHubResponse(data, _has_next_page(headers), rate, authenticated)

        detail = self._error_detail(response.body, token)
        suffix = f": {detail}" if detail else "."
        if 300 <= status < 400:
            raise GitHubRequestError(
                f"GitHub redirected the request (HTTP {status}); the repository may have been renamed or moved. "
                f"Update the '{REMOTE_NAME}' remote. Redirects are not followed."
            )
        if status == 401:
            hint = "Check that GITHUB_TOKEN is valid and not expired." if authenticated else "Set GITHUB_TOKEN."
            raise GitHubAuthError(f"GitHub rejected the request as unauthenticated (HTTP 401). {hint}")
        if status in (403, 429) and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers):
            when = f" Retry after {headers['retry-after']} seconds." if "retry-after" in headers else ""
            if rate and rate.reset_at and headers.get("x-ratelimit-remaining") == "0":
                when = f" The limit resets at {rate.reset_at}."
            hint = "" if authenticated else " Unauthenticated requests are limited to 60 per hour; set GITHUB_TOKEN for more."
            raise GitHubRateLimitError(f"GitHub API rate limit exceeded (HTTP {status}).{when}{hint}")
        if status == 403:
            raise GitHubForbiddenError(
                f"GitHub denied access (HTTP 403){suffix} The token may lack read permission for this repository."
            )
        if status == 404:
            raise GitHubNotFoundError(
                f"GitHub repository '{repo.full_name}' was not found (HTTP 404). It may not exist, or it may be "
                f"private and GITHUB_TOKEN is {'lacking access' if authenticated else 'not set'}."
            )
        if 400 <= status < 500:
            raise GitHubRequestError(f"GitHub rejected the request (HTTP {status}){suffix}")
        if status >= 500:
            raise GitHubServerError(f"GitHub server error (HTTP {status}). Try again later.")
        raise GitHubResponseError(f"Unexpected HTTP status from GitHub: {status}.")

    @staticmethod
    def _error_detail(body: bytes, token: str) -> str:
        """GitHub's own error message, if any, truncated and scrubbed of the token."""
        try:
            message = json.loads(body.decode("utf-8")).get("message", "")
        except (UnicodeDecodeError, ValueError, AttributeError):
            return ""
        if not isinstance(message, str):
            return ""
        if token:
            message = message.replace(token, "***")
        return " ".join(message.split())[:MAX_ERROR_CHARS]
