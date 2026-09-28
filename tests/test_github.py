"""Tests for the read-only GitHub integration. No test contacts the real GitHub API."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mcp import Client

from devpilot_mcp.github import client as gh
from devpilot_mcp.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubConnectionError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRequestError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
    HttpResponse,
)
from devpilot_mcp.github.remote import (
    GitHubRepository,
    MalformedGitHubRemoteError,
    NoGitHubRemoteError,
    NonGitHubRemoteError,
    discover_github_repository,
    parse_github_remote,
)
from devpilot_mcp.server import create_server
from devpilot_mcp.tools import git
from devpilot_mcp.tools import github as tools
from devpilot_mcp.tools.git import GitCommandNotAllowedError, NotAGitRepositoryError
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError
from tests.test_git import GitRepoTestCase

TOKEN = "ghp_TESTSECRET_0123456789abcdef"
REPO = GitHubRepository("octo-org", "hello-world")
API = "https://api.github.com/repos/octo-org/hello-world"


# --- Fixtures ----------------------------------------------------------------


def respond(data=None, status: int = 200, headers: dict | None = None, body: bytes | None = None) -> HttpResponse:
    raw = body if body is not None else json.dumps(data).encode()
    return HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, raw)


def next_link(page: int = 2) -> dict:
    # A hostile URL in the Link header must never be requested; only rel="next" matters.
    return {"Link": f'<https://evil.example/steal?page={page}>; rel="next", <https://evil.example/last>; rel="last"'}


class FakeTransport:
    """Records every request and replays queued responses (or raises queued exceptions)."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if not self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self) -> list[str]:
        return [call[0] for call in self.calls]


def repo_json(**overrides) -> dict:
    data = {
        "id": 1296269, "node_id": "MDEwOlJlcG9zaXRvcnkxMjk2MjY5", "name": "hello-world",
        "full_name": "octo-org/hello-world",
        "owner": {"login": "octo-org", "id": 1, "type": "Organization", "site_admin": False},
        "private": False, "html_url": "https://github.com/octo-org/hello-world",
        "description": "This your first repo!", "fork": False, "url": API,
        "homepage": "https://github.com", "language": "Python", "forks_count": 9, "stargazers_count": 80,
        "watchers_count": 80, "subscribers_count": 42, "size": 108, "default_branch": "main",
        "open_issues_count": 3, "is_template": False, "topics": ["mcp", "devtools"], "archived": False,
        "disabled": False, "visibility": "public", "pushed_at": "2026-09-20T10:00:00Z",
        "created_at": "2020-01-26T19:01:12Z", "updated_at": "2026-09-21T11:00:00Z",
        "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT"},
        "permissions": {"admin": True, "push": True, "pull": True},
        "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
    }  # fmt: skip
    data.update(overrides)
    return data


def issue_json(number: int, *, pr: bool = False, **overrides) -> dict:
    data = {
        "id": 1000 + number, "number": number, "title": f"Issue {number}", "state": "open", "state_reason": None,
        "user": {"login": "octocat", "id": 1}, "labels": [{"id": 1, "name": "bug", "color": "f29513"}],
        "assignees": [{"login": "hubot"}], "comments": 2, "locked": False,
        "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-02T00:00:00Z", "closed_at": None,
        "html_url": f"https://github.com/octo-org/hello-world/issues/{number}",
        "body": "SECRET BODY TEXT", "reactions": {"+1": 5},
    }  # fmt: skip
    if pr:
        data["pull_request"] = {"url": f"{API}/pulls/{number}", "merged_at": None}
    data.update(overrides)
    return data


def pull_json(number: int, **overrides) -> dict:
    data = {
        "id": 5000 + number, "number": number, "title": f"PR {number}", "state": "open", "locked": False,
        "user": {"login": "octocat"}, "body": "SECRET PR BODY", "draft": False,
        "labels": [{"name": "enhancement"}], "assignees": [], "created_at": "2026-09-03T00:00:00Z",
        "updated_at": "2026-09-04T00:00:00Z", "closed_at": None, "merged_at": None,
        "requested_reviewers": [{"login": "reviewer1"}], "requested_teams": [{"slug": "core", "name": "Core"}],
        "head": {"ref": "feature/x", "sha": "abc", "repo": {"full_name": "contributor/hello-world"}},
        "base": {"ref": "main", "sha": "def", "repo": {"full_name": "octo-org/hello-world"}},
        "html_url": f"https://github.com/octo-org/hello-world/pull/{number}", "author_association": "CONTRIBUTOR",
    }  # fmt: skip
    data.update(overrides)
    return data


class NoTokenMixin:
    """Run each test with GITHUB_TOKEN unset unless the test sets it."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        os.environ.pop("GITHUB_TOKEN", None)


class GitHubRepoTestCase(NoTokenMixin, GitRepoTestCase):
    """A real Git repository whose origin is github.com/octo-org/hello-world, plus a fake transport."""

    def setUp(self) -> None:
        super().setUp()
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")
        self.transport = FakeTransport()
        self.client = GitHubClient(transport=self.transport)

    def queue(self, *responses) -> None:
        self.transport.responses.extend(responses)


# --- Remote parsing and discovery --------------------------------------------


class RemoteParsingTests(unittest.TestCase):
    def test_github_remote_forms(self) -> None:
        cases = [
            "https://github.com/OWNER/REPO.git",
            "https://github.com/OWNER/REPO",
            "https://github.com/OWNER/REPO/",
            "http://github.com/OWNER/REPO.git",
            "https://www.github.com/OWNER/REPO.git",
            "https://GitHub.COM/OWNER/REPO.git",
            f"https://x-access-token:{TOKEN}@github.com/OWNER/REPO.git",
            "git@github.com:OWNER/REPO.git",
            "git@github.com:OWNER/REPO",
            "ssh://git@github.com/OWNER/REPO.git",
            "ssh://git@ssh.github.com:443/OWNER/REPO.git",
            "git://github.com/OWNER/REPO.git",
            "  https://github.com/OWNER/REPO.git\n",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_github_remote(url), GitHubRepository("OWNER", "REPO"))

    def test_valid_names(self) -> None:
        repo = parse_github_remote("git@github.com:my-org2/my_repo.v2-final.git")
        self.assertEqual((repo.owner, repo.name, repo.full_name), ("my-org2", "my_repo.v2-final", "my-org2/my_repo.v2-final"))

    def test_non_github_hosts(self) -> None:
        for url, host in [
            ("https://gitlab.com/o/r.git", "gitlab.com"),
            ("git@bitbucket.org:o/r.git", "bitbucket.org"),
            ("https://github.example-corp.com/o/r.git", "github.example-corp.com"),
            (f"https://user:{TOKEN}@gitlab.com/o/r.git", "gitlab.com"),
            ("https://github.com.evil.example/o/r.git", "github.com.evil.example"),
        ]:
            with self.subTest(url=url):
                with self.assertRaises(NonGitHubRemoteError) as ctx:
                    parse_github_remote(url)
                self.assertIn(f"'{host}'", str(ctx.exception))
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertNotIn("/o/r", str(ctx.exception))

    def test_local_paths_are_not_github(self) -> None:
        for url in ("/srv/git/repo.git", "C:\\repos\\project", "C:/repos/project", "../other", "file:///srv/repo.git"):
            with self.subTest(url=url):
                with self.assertRaises(NonGitHubRemoteError):
                    parse_github_remote(url)

    def test_malformed_github_remotes(self) -> None:
        cases = [
            "https://github.com/onlyowner",
            "https://github.com/",
            "https://github.com/o/r/tree/main",
            "git@github.com:o/r/extra.git",
            "https://github.com/-bad/r",
            "https://github.com/o/..",
            "https://github.com/o/.git",
            "https://github.com/o/r%2F..",
            "https://github.com/o/re po",
            "https://github.com/" + "a" * 40 + "/r",
            "https://github.com:notaport/o/r",
            f"https://{TOKEN}@github.com/o/r/x",
        ]
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(MalformedGitHubRemoteError) as ctx:
                    parse_github_remote(url)
                self.assertNotIn(TOKEN, str(ctx.exception))


class DiscoveryTests(GitRepoTestCase):
    def test_https_and_ssh_origins(self) -> None:
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")
        self.assertEqual(discover_github_repository(self.workspace), REPO)
        self.run_git("remote", "set-url", "origin", "git@github.com:octo-org/other.git")
        self.assertEqual(discover_github_repository(self.workspace), GitHubRepository("octo-org", "other"))

    def test_insteadof_rewrites_are_applied(self) -> None:
        self.run_git("config", "url.https://github.com/.insteadOf", "gh:")
        self.run_git("remote", "add", "origin", "gh:octo-org/hello-world")
        self.assertEqual(discover_github_repository(self.workspace), REPO)

    def test_no_origin(self) -> None:
        with self.assertRaisesRegex(NoGitHubRemoteError, "no 'origin' remote"):
            discover_github_repository(self.workspace)

    def test_only_origin_is_used(self) -> None:
        self.run_git("remote", "add", "upstream", "https://github.com/octo-org/hello-world.git")
        with self.assertRaises(NoGitHubRemoteError):
            discover_github_repository(self.workspace)

    def test_non_github_origin(self) -> None:
        self.run_git("remote", "add", "origin", "https://gitlab.com/octo-org/hello-world.git")
        with self.assertRaisesRegex(NonGitHubRemoteError, "gitlab.com"):
            discover_github_repository(self.workspace)

    def test_malformed_origin(self) -> None:
        self.run_git("remote", "add", "origin", "https://github.com/octo-org")
        with self.assertRaises(MalformedGitHubRemoteError):
            discover_github_repository(self.workspace)

    def test_non_git_workspace_and_parent_repositories(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(NotAGitRepositoryError):
            discover_github_repository(Workspace(plain))
        nested = self.root / "sub"
        nested.mkdir()
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")
        with self.assertRaises(NotAGitRepositoryError):  # the parent repository's origin is not used
            discover_github_repository(Workspace(nested))

    def test_missing_workspace(self) -> None:
        with self.assertRaises(PathNotFoundError):
            discover_github_repository(Workspace(self.base / "gone"))

    def test_discovery_does_not_modify_git_config(self) -> None:
        self.run_git("remote", "add", "origin", "https://github.com/octo-org/hello-world.git")
        config = (self.root / ".git" / "config").read_bytes()
        discover_github_repository(self.workspace)
        self.assertEqual((self.root / ".git" / "config").read_bytes(), config)

    def test_git_remote_is_limited_to_get_url(self) -> None:
        with mock.patch.object(git, "_execute") as execute:
            for args in (("add", "evil", "https://evil.example"), ("remove", "origin"), ("set-url", "origin", "x"),
                         ("rename", "origin", "x"), ()):  # fmt: skip
                with self.subTest(args=args):
                    with self.assertRaises(GitCommandNotAllowedError):
                        git._run_git(self.workspace, "remote", *args)
            for name in ("origin; rm -rf /", "--upload-pack=evil", "../x", ""):
                with self.subTest(name=name):
                    with self.assertRaises(GitCommandNotAllowedError):
                        git.read_remote_url(self.workspace, name)
        execute.assert_not_called()


# --- Client ------------------------------------------------------------------


class ClientTests(NoTokenMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transport = FakeTransport()
        self.client = GitHubClient(transport=self.transport)

    def test_repository_request(self) -> None:
        self.transport.responses.append(respond(repo_json()))
        response = self.client.get_repository(REPO)
        url, headers, timeout = self.transport.calls[0]
        self.assertEqual(url, API)
        self.assertEqual(
            headers,
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
             "User-Agent": headers["User-Agent"]},
        )  # fmt: skip
        self.assertTrue(headers["User-Agent"].startswith("devpilot-mcp/"))
        self.assertEqual(timeout, gh.TIMEOUT_SECONDS)
        self.assertFalse(response.authenticated)

    def test_token_is_sent_only_as_bearer_authorization(self) -> None:
        os.environ["GITHUB_TOKEN"] = f"  {TOKEN}\n"
        self.transport.responses.append(respond(repo_json()))
        response = self.client.get_repository(REPO)
        url, headers, _ = self.transport.calls[0]
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, url)
        self.assertTrue(response.authenticated)
        self.assertNotIn(TOKEN, repr(self.client.__dict__))  # never stored on the client

    def test_list_request_parameters(self) -> None:
        self.transport.responses += [respond([]), respond([])]
        self.client.list_issues(REPO, state="closed", per_page=30, page=2)
        self.client.list_pull_requests(REPO, state="all", per_page=5)
        self.assertEqual(
            self.transport.urls,
            [f"{API}/issues?state=closed&per_page=30&page=2", f"{API}/pulls?state=all&per_page=5&page=1"],
        )

    def test_pagination_and_rate_limit_headers(self) -> None:
        headers = {**next_link(), "X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "57",
                   "X-RateLimit-Used": "3", "X-RateLimit-Reset": "1790000000", "X-RateLimit-Resource": "core"}  # fmt: skip
        self.transport.responses.append(respond([], headers=headers))
        response = self.client.list_issues(REPO, state="open", per_page=10)
        self.assertTrue(response.has_next_page)
        self.assertEqual(
            response.rate_limit,
            gh.RateLimit(limit=60, remaining=57, used=3, reset_at="2026-09-21T14:13:20Z", resource="core"),
        )
        self.assertEqual(self.transport.urls, [f"{API}/issues?state=open&per_page=10&page=1"])  # Link URL not followed

    def test_invalid_list_parameters_never_reach_the_network(self) -> None:
        for kwargs in (dict(state="merged", per_page=10), dict(state="open", per_page=0),
                       dict(state="open", per_page=101), dict(state="open", per_page=10, page=0),
                       dict(state="open", per_page=10, page=gh.MAX_PAGE + 1), dict(state="open", per_page=True)):  # fmt: skip
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GitHubRequestError):
                    self.client.list_issues(REPO, **kwargs)
        self.assertEqual(self.transport.calls, [])

    def test_only_three_fixed_operations_exist(self) -> None:
        public = {name for name in dir(GitHubClient) if not name.startswith("_")}
        self.assertEqual(public, {"get_repository", "list_issues", "list_pull_requests"})
        self.assertEqual(set(gh.ENDPOINTS), {"repository", "issues", "pulls"})
        with self.assertRaises(GitHubRequestError):
            self.client._get("search", REPO, {})
        with self.assertRaises(GitHubRequestError):
            self.client._get("repository", REPO, {"per_page": 1})
        with self.assertRaises(GitHubRequestError):
            self.client._get("issues", REPO, {"state": "open", "q": "x", "url": "https://evil.example"})
        with self.assertRaises(GitHubRequestError):
            self.client._get("repository", "octo-org/hello-world", {})  # type: ignore[arg-type]
        self.assertEqual(self.transport.calls, [])

    def test_path_segments_are_encoded(self) -> None:
        self.transport.responses.append(respond(repo_json()))
        self.client.get_repository(GitHubRepository("o", "r?x=1#/../../evil"))
        self.assertEqual(self.transport.urls, [f"{gh.API_BASE_URL}/repos/o/r%3Fx%3D1%23%2F..%2F..%2Fevil"])

    def assert_error(self, response, error_type, *fragments, token: bool = False) -> str:
        if token:
            os.environ["GITHUB_TOKEN"] = TOKEN
        else:
            os.environ.pop("GITHUB_TOKEN", None)
        self.transport.responses.append(response)
        with self.assertRaises(error_type) as ctx:
            self.client.get_repository(REPO)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        self.assertNotIn(TOKEN, message)
        return message

    def test_status_mapping(self) -> None:
        self.assert_error(respond({"message": "Bad credentials"}, 401), GitHubAuthError, "HTTP 401", "Set GITHUB_TOKEN")
        self.assert_error(respond({"message": "Bad credentials"}, 401), GitHubAuthError, "not expired", token=True)
        self.assert_error(
            respond({"message": "API rate limit exceeded"}, 403,
                    {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"}),
            GitHubRateLimitError, "rate limit", "2026-09-21T14:13:20Z", "60 per hour",
        )  # fmt: skip
        self.assert_error(respond({"message": "secondary"}, 429, {"Retry-After": "30"}), GitHubRateLimitError, "30 seconds")
        self.assert_error(
            respond({"message": "Resource not accessible by personal access token"}, 403),
            GitHubForbiddenError, "HTTP 403", "Resource not accessible",
        )  # fmt: skip
        self.assert_error(respond({"message": "Not Found"}, 404), GitHubNotFoundError, "octo-org/hello-world", "not set")
        self.assert_error(respond({"message": "Not Found"}, 404), GitHubNotFoundError, "lacking access", token=True)
        self.assert_error(respond({"message": "Issues are disabled for this repo"}, 410), GitHubRequestError, "410", "disabled")
        self.assert_error(respond({"message": "Validation Failed"}, 422), GitHubRequestError, "422", "Validation Failed")
        self.assert_error(respond(body=b"<html>oops</html>", status=502), GitHubServerError, "HTTP 502")
        self.assert_error(respond(body=b"", status=503), GitHubServerError, "HTTP 503")
        self.assert_error(
            respond(body=b"", status=301, headers={"Location": "https://api.github.com/repositories/1"}),
            GitHubRequestError, "redirected", "not followed",
        )  # fmt: skip

    def test_malformed_json(self) -> None:
        self.assert_error(respond(body=b"{not json"), GitHubResponseError, "not valid JSON")
        self.assert_error(respond(body=b"\xff\xfe"), GitHubResponseError, "not valid JSON")

    def test_token_echoed_by_github_is_scrubbed(self) -> None:
        message = self.assert_error(
            respond({"message": f"token {TOKEN} is not allowed"}, 403), GitHubForbiddenError, "***", token=True
        )
        self.assertNotIn(TOKEN, message)

    def test_transport_failures_propagate_cleanly(self) -> None:
        for error in (GitHubTimeoutError("GitHub did not respond within 15 seconds."),
                      GitHubConnectionError("Could not connect to GitHub (gaierror).")):  # fmt: skip
            with self.subTest(error=type(error).__name__):
                self.transport.responses.append(error)
                with self.assertRaises(type(error)):
                    self.client.get_repository(REPO)


# --- Stdlib transport (against local servers; never the real network) -------


class _Recorder(BaseHTTPRequestHandler):
    """Configurable handler; each server gets its own subclass with class-level settings."""

    status = 200
    body = b"[]"
    extra_headers: dict = {}
    delay = 0.0
    requests: list

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.requests.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)}
        )
        time.sleep(self.delay)
        self.send_response(self.status)
        for key, value in self.extra_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, *args) -> None:  # keep test output quiet
        pass


class TransportTests(unittest.TestCase):
    def serve(self, **settings) -> tuple[str, list]:
        requests: list = []
        handler = type("Handler", (_Recorder,), {**settings, "requests": requests})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", requests

    def test_only_https_api_github_com_is_contacted(self) -> None:
        with mock.patch.object(gh, "_open") as open_:
            for url in ("http://api.github.com/repos/o/r", "https://evil.example/repos/o/r",
                        "https://api.github.com.evil.example/repos/o/r", "https://api.github.com@evil.example/",
                        "file:///etc/passwd", "ftp://api.github.com/"):  # fmt: skip
                with self.subTest(url=url):
                    with self.assertRaises(GitHubRequestError):
                        gh.urllib_transport(url, {}, 5)
        open_.assert_not_called()

    def test_sends_a_bodyless_get(self) -> None:
        base, requests = self.serve(body=b'{"ok": true}', extra_headers={"X-RateLimit-Limit": "60"})
        response = gh._open(base + "/repos/o/r", {"Accept": "application/vnd.github+json"}, 5)
        self.assertEqual((response.status, response.body, response.headers["x-ratelimit-limit"]), (200, b'{"ok": true}', "60"))
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]["method"], requests[0]["body"]), ("GET", b""))

    def test_redirects_are_not_followed(self) -> None:
        other, stolen = self.serve()
        base, _ = self.serve(status=302, body=b"", extra_headers={"Location": other + "/steal"})
        response = gh._open(base + "/repos/o/r", {"Authorization": f"Bearer {TOKEN}"}, 5)
        self.assertEqual(response.status, 302)
        self.assertEqual(stolen, [])  # the redirect target never received anything, let alone the token

    def test_error_statuses_are_returned_not_raised(self) -> None:
        base, _ = self.serve(status=404, body=b'{"message": "Not Found"}')
        response = gh._open(base + "/x", {}, 5)
        self.assertEqual((response.status, response.body), (404, b'{"message": "Not Found"}'))

    def test_timeout(self) -> None:
        base, _ = self.serve(delay=3)
        started = time.monotonic()
        with self.assertRaises(GitHubTimeoutError):
            gh._open(base + "/slow", {}, 0.5)
        self.assertLess(time.monotonic() - started, 3)

    def test_connection_failure(self) -> None:
        base, _ = self.serve()
        port = base.rsplit(":", 1)[1]
        # Nothing listens on port 1; the error names the failure type only.
        with self.assertRaises(GitHubConnectionError) as ctx:
            gh._open("http://127.0.0.1:1/x", {"Authorization": f"Bearer {TOKEN}"}, 5)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(port, str(ctx.exception))

    def test_response_size_is_capped(self) -> None:
        base, _ = self.serve(body=b"x" * 5000)
        with mock.patch.object(gh, "MAX_RESPONSE_BYTES", 1000):
            with self.assertRaisesRegex(GitHubResponseError, "unexpectedly large"):
                gh._open(base + "/big", {}, 5)


# --- Tools -------------------------------------------------------------------


class GitHubRepositoryToolTests(GitHubRepoTestCase):
    def test_normalized_output(self) -> None:
        self.queue(respond(repo_json(), headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}))
        result = tools.github_repository(self.workspace, self.client)
        self.assertEqual(self.transport.urls, [API])
        self.assertEqual(
            result.model_dump(exclude={"rate_limit"}),
            {
                "owner": "octo-org", "name": "hello-world", "full_name": "octo-org/hello-world",
                "description": "This your first repo!", "html_url": "https://github.com/octo-org/hello-world",
                "homepage": "https://github.com", "default_branch": "main", "visibility": "public",
                "private": False, "fork": False, "archived": False, "disabled": False, "is_template": False,
                "created_at": "2020-01-26T19:01:12Z", "updated_at": "2026-09-21T11:00:00Z",
                "pushed_at": "2026-09-20T10:00:00Z", "language": "Python", "license": "MIT",
                "topics": ["mcp", "devtools"], "stargazers_count": 80, "watchers_count": 80,
                "subscribers_count": 42, "forks_count": 9, "open_issues_count": 3, "authenticated": False,
            },
        )  # fmt: skip
        self.assertEqual((result.rate_limit.limit, result.rate_limit.remaining), (60, 59))
        dumped = result.model_dump_json()
        for raw_only in ("permissions", "security_and_analysis", "node_id", "MDEwOlJlcG9zaXRvcnkxMjk2MjY5"):
            self.assertNotIn(raw_only, dumped)

    def test_missing_optional_fields(self) -> None:
        data = repo_json(description=None, homepage="", language=None, license=None, pushed_at=None)
        for key in ("topics", "subscribers_count", "visibility", "disabled", "is_template", "archived"):
            del data[key]
        self.queue(respond(data))
        result = tools.github_repository(self.workspace, self.client)
        self.assertEqual(
            (result.description, result.homepage, result.language, result.license, result.pushed_at, result.topics,
             result.subscribers_count, result.visibility, result.disabled, result.archived),
            (None, None, None, None, None, [], None, None, None, False),
        )  # fmt: skip
        self.assertIsNone(result.rate_limit)

    def test_unexpected_schema(self) -> None:
        for data in (repo_json(default_branch=None), repo_json(stargazers_count="80"), repo_json(private="no"),
                     repo_json(owner="octo-org"), [repo_json()], "text"):  # fmt: skip
            with self.subTest(data=str(data)[:40]):
                self.queue(respond(data))
                with self.assertRaisesRegex(GitHubResponseError, "Unexpected GitHub response"):
                    tools.github_repository(self.workspace, self.client)

    def test_api_errors(self) -> None:
        for response, error in ((respond({}, 401), GitHubAuthError), (respond({}, 403), GitHubForbiddenError),
                                (respond({}, 404), GitHubNotFoundError), (respond({}, 500), GitHubServerError),
                                (respond(body=b"nope"), GitHubResponseError),
                                (GitHubTimeoutError("GitHub did not respond within 15 seconds."), GitHubTimeoutError)):  # fmt: skip
            with self.subTest(error=error.__name__):
                self.queue(response)
                with self.assertRaises(error):
                    tools.github_repository(self.workspace, self.client)


class GitHubIssuesToolTests(GitHubRepoTestCase):
    def test_default_request_and_limit(self) -> None:
        self.queue(respond([issue_json(n) for n in range(40, 10, -1)], headers=next_link()))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual(self.transport.urls, [f"{API}/issues?state=open&per_page=30&page=1"])
        self.assertEqual((result.state, result.limit, len(result.issues)), ("open", 10, 10))
        self.assertEqual([i.number for i in result.issues], list(range(40, 30, -1)))  # GitHub's order is kept
        self.assertTrue(result.has_more)

    def test_states(self) -> None:
        for state in ("open", "closed", "all"):
            with self.subTest(state=state):
                self.queue(respond([]))
                result = tools.github_issues(self.workspace, self.client, state=state)
                self.assertEqual(result.state, state)
                self.assertEqual(self.transport.urls[-1], f"{API}/issues?state={state}&per_page=30&page=1")

    def test_custom_limit_bounds(self) -> None:
        self.queue(respond([issue_json(n) for n in range(3, 0, -1)]))
        self.assertEqual(len(tools.github_issues(self.workspace, self.client, limit=1).issues), 1)
        self.queue(respond([issue_json(n) for n in range(60, 0, -1)]))
        result = tools.github_issues(self.workspace, self.client, limit=50)
        self.assertEqual((len(result.issues), result.has_more), (50, True))
        self.assertEqual(self.transport.urls[-1], f"{API}/issues?state=open&per_page=51&page=1")

    def test_invalid_arguments_never_reach_the_network(self) -> None:
        for kwargs in (dict(limit=0), dict(limit=51), dict(limit=-1), dict(limit=True), dict(limit="5"),
                       dict(limit=None), dict(state="merged"), dict(state="OPEN"), dict(state="")):  # fmt: skip
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkspaceError):
                    tools.github_issues(self.workspace, self.client, **kwargs)
        self.assertEqual(self.transport.calls, [])

    def test_issue_normalization(self) -> None:
        closed = issue_json(7, state="closed", state_reason="completed", closed_at="2026-09-05T00:00:00Z",
                            user=None, labels=[{"name": "bug"}, "legacy-string-label"], assignees=[], locked=True)  # fmt: skip
        self.queue(respond([closed]))
        issue = tools.github_issues(self.workspace, self.client, state="closed").issues[0]
        self.assertEqual(
            issue.model_dump(),
            {
                "number": 7, "title": "Issue 7", "state": "closed", "state_reason": "completed", "author": None,
                "labels": ["bug", "legacy-string-label"], "assignees": [], "comments": 2, "locked": True,
                "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-02T00:00:00Z",
                "closed_at": "2026-09-05T00:00:00Z", "html_url": "https://github.com/octo-org/hello-world/issues/7",
            },
        )  # fmt: skip
        self.assertNotIn("SECRET BODY TEXT", tools.github_issues.__doc__ or "")

    def test_bodies_are_never_returned(self) -> None:
        self.queue(respond([issue_json(1)]))
        self.assertNotIn("SECRET BODY TEXT", tools.github_issues(self.workspace, self.client).model_dump_json())

    def test_pull_requests_are_excluded_and_counted(self) -> None:
        self.queue(respond([issue_json(9, pr=True), issue_json(8), issue_json(7, pr=True), issue_json(6)]))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual([i.number for i in result.issues], [8, 6])
        self.assertEqual((result.pull_requests_excluded, result.has_more, result.pages_fetched), (2, False, 1))

    def test_further_pages_are_fetched_to_fill_the_limit(self) -> None:
        page1 = [issue_json(n, pr=True) for n in range(100, 75, -1)] + [issue_json(n) for n in range(75, 70, -1)]
        page2 = [issue_json(n) for n in range(70, 60, -1)]
        self.queue(respond(page1, headers=next_link(2)), respond(page2))
        result = tools.github_issues(self.workspace, self.client, limit=10)
        self.assertEqual(
            self.transport.urls,
            [f"{API}/issues?state=open&per_page=30&page=1", f"{API}/issues?state=open&per_page=30&page=2"],
        )  # our own page URLs; the hostile Link URL was never requested
        self.assertEqual([i.number for i in result.issues], [75, 74, 73, 72, 71, 70, 69, 68, 67, 66])
        self.assertEqual((result.has_more, result.pull_requests_excluded, result.pages_fetched), (True, 25, 2))

    def test_page_cap(self) -> None:
        for page in range(1, 5):
            self.queue(respond([issue_json(n, pr=True) for n in range(30)], headers=next_link(page + 1)))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual(len(self.transport.calls), tools.MAX_ISSUE_PAGES)
        self.assertEqual((result.issues, result.has_more, result.pages_fetched), ([], True, tools.MAX_ISSUE_PAGES))

    def test_has_more_is_exact_when_github_has_no_next_page(self) -> None:
        self.queue(respond([issue_json(n) for n in range(10, 0, -1)]))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual((len(result.issues), result.has_more), (10, False))

    def test_exactly_limit_with_next_page_stops_after_one_request(self) -> None:
        self.queue(respond([issue_json(n, pr=True) for n in range(20)] + [issue_json(n) for n in range(10)], headers=next_link()))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual((len(self.transport.calls), len(result.issues), result.has_more), (1, 10, True))

    def test_empty_repository(self) -> None:
        self.queue(respond([]))
        result = tools.github_issues(self.workspace, self.client)
        self.assertEqual((result.issues, result.has_more, result.pull_requests_excluded), ([], False, 0))

    def test_unexpected_schema_and_errors(self) -> None:
        for response, error in ((respond({"message": "x"}), GitHubResponseError),
                                (respond(["not an object"]), GitHubResponseError),
                                (respond([issue_json(1, title=5)]), GitHubResponseError),
                                (respond({}, 404), GitHubNotFoundError),
                                (respond({"message": "Issues are disabled"}, 410), GitHubRequestError)):  # fmt: skip
            with self.subTest(error=error.__name__):
                self.queue(response)
                with self.assertRaises(error):
                    tools.github_issues(self.workspace, self.client)


class GitHubPullRequestsToolTests(GitHubRepoTestCase):
    def test_default_request(self) -> None:
        self.queue(respond([pull_json(n) for n in range(10, 0, -1)], headers=next_link()))
        result = tools.github_pull_requests(self.workspace, self.client)
        self.assertEqual(self.transport.urls, [f"{API}/pulls?state=open&per_page=10&page=1"])
        self.assertEqual((result.state, result.limit, len(result.pull_requests), result.has_more), ("open", 10, 10, True))

    def test_states_and_custom_limit(self) -> None:
        for state in ("open", "closed", "all"):
            with self.subTest(state=state):
                self.queue(respond([pull_json(2), pull_json(1)]))
                result = tools.github_pull_requests(self.workspace, self.client, state=state, limit=3)
                self.assertEqual(self.transport.urls[-1], f"{API}/pulls?state={state}&per_page=3&page=1")
                self.assertEqual((result.state, len(result.pull_requests), result.has_more), (state, 2, False))

    def test_limit_bounds(self) -> None:
        self.queue(respond([pull_json(1)]), respond([pull_json(1)]))
        tools.github_pull_requests(self.workspace, self.client, limit=1)
        tools.github_pull_requests(self.workspace, self.client, limit=50)
        self.assertEqual(
            self.transport.urls,
            [f"{API}/pulls?state=open&per_page=1&page=1", f"{API}/pulls?state=open&per_page=50&page=1"],
        )

    def test_invalid_arguments_never_reach_the_network(self) -> None:
        for kwargs in (dict(limit=0), dict(limit=51), dict(limit=2.5), dict(limit=False), dict(state="merged"),
                       dict(state="draft"), dict(state=None)):  # fmt: skip
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkspaceError):
                    tools.github_pull_requests(self.workspace, self.client, **kwargs)
        self.assertEqual(self.transport.calls, [])

    def test_pull_request_normalization(self) -> None:
        merged = pull_json(
            12, state="closed", draft=True, closed_at="2026-09-06T00:00:00Z", merged_at="2026-09-06T00:00:00Z",
            user=None, assignees=[{"login": "hubot"}],
            head={"ref": "fix/bug", "repo": None},  # the fork was deleted
        )  # fmt: skip
        self.queue(respond([merged]))
        pr = tools.github_pull_requests(self.workspace, self.client, state="closed").pull_requests[0]
        self.assertEqual(
            pr.model_dump(),
            {
                "number": 12, "title": "PR 12", "state": "closed", "draft": True, "author": None,
                "created_at": "2026-09-03T00:00:00Z", "updated_at": "2026-09-04T00:00:00Z",
                "closed_at": "2026-09-06T00:00:00Z", "merged_at": "2026-09-06T00:00:00Z",
                "source_branch": "fix/bug", "source_repository": None,
                "target_branch": "main", "target_repository": "octo-org/hello-world",
                "labels": ["enhancement"], "assignees": ["hubot"], "requested_reviewers": ["reviewer1"],
                "requested_teams": ["core"], "html_url": "https://github.com/octo-org/hello-world/pull/12",
            },
        )  # fmt: skip

    def test_bodies_are_never_returned(self) -> None:
        self.queue(respond([pull_json(1)]))
        dumped = tools.github_pull_requests(self.workspace, self.client).model_dump_json()
        self.assertNotIn("SECRET PR BODY", dumped)

    def test_empty_repository(self) -> None:
        self.queue(respond([]))
        result = tools.github_pull_requests(self.workspace, self.client)
        self.assertEqual((result.pull_requests, result.has_more), ([], False))

    def test_unexpected_schema_and_errors(self) -> None:
        for response, error in ((respond({}), GitHubResponseError),
                                (respond([pull_json(1, head=None)]), GitHubResponseError),
                                (respond([pull_json(1, draft="yes")]), GitHubResponseError),
                                (respond({}, 401), GitHubAuthError), (respond({}, 503), GitHubServerError)):  # fmt: skip
            with self.subTest(error=error.__name__):
                self.queue(response)
                with self.assertRaises(error):
                    tools.github_pull_requests(self.workspace, self.client)


class ToolErrorPathTests(NoTokenMixin, unittest.TestCase):
    def test_non_git_and_missing_workspaces(self) -> None:
        transport = FakeTransport()
        client = GitHubClient(transport=transport)
        with tempfile_dir() as base:
            for workspace, error in ((Workspace(base), NotAGitRepositoryError), (Workspace(base / "gone"), PathNotFoundError)):
                for call in (lambda: tools.github_repository(workspace, client),
                             lambda: tools.github_issues(workspace, client),
                             lambda: tools.github_pull_requests(workspace, client)):  # fmt: skip
                    with self.assertRaises(error):
                        call()
        self.assertEqual(transport.calls, [])


@contextlib.contextmanager
def tempfile_dir():
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        yield Path(name)


# --- Security across the tools -----------------------------------------------


class TokenSecurityTests(GitHubRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["GITHUB_TOKEN"] = TOKEN

    def test_token_is_used_but_never_returned(self) -> None:
        self.queue(respond(repo_json()), respond([issue_json(1)]), respond([pull_json(1)]))
        outputs = [
            tools.github_repository(self.workspace, self.client).model_dump_json(),
            tools.github_issues(self.workspace, self.client).model_dump_json(),
            tools.github_pull_requests(self.workspace, self.client).model_dump_json(),
        ]
        self.assertTrue(all(headers["Authorization"] == f"Bearer {TOKEN}" for _, headers, _ in self.transport.calls))
        for output in outputs:
            self.assertNotIn(TOKEN, output)
            self.assertIn('"authenticated":true', output)

    def test_token_never_in_errors(self) -> None:
        failures = [
            respond({"message": f"Bad credentials for {TOKEN}"}, 401),
            respond({"message": f"{TOKEN} forbidden"}, 403),
            respond({"message": "x"}, 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000"}),
            respond({"message": TOKEN}, 404),
            respond({"message": TOKEN}, 422),
            respond(body=TOKEN.encode(), status=500),
            respond(body=TOKEN.encode()),
            GitHubTimeoutError("GitHub did not respond within 15 seconds."),
        ]
        for failure in failures:
            with self.subTest(failure=getattr(failure, "status", failure)):
                self.queue(failure)
                with self.assertRaises(WorkspaceError) as ctx:
                    tools.github_repository(self.workspace, self.client)
                self.assertNotIn(TOKEN, str(ctx.exception))

    def test_every_request_targets_the_discovered_repository_over_https(self) -> None:
        self.queue(respond(repo_json()), respond([]), respond([]))
        tools.github_repository(self.workspace, self.client)
        tools.github_issues(self.workspace, self.client, state="all", limit=5)
        tools.github_pull_requests(self.workspace, self.client, state="closed", limit=5)
        for url in self.transport.urls:
            self.assertTrue(url == API or url.startswith(API + "/issues?") or url.startswith(API + "/pulls?"), url)


class GitHubMcpTests(GitHubRepoTestCase, unittest.IsolatedAsyncioTestCase):
    async def call(self, tool: str, args: dict | None = None, workspace: Workspace | None = None):
        server = create_server(workspace or self.workspace, github_client=self.client)
        async with Client(server) as client:
            return await client.call_tool(tool, args or {})

    async def test_tools_registered_with_bounded_schemas(self) -> None:
        async with Client(create_server(self.workspace, github_client=self.client)) as client:
            tools_by_name = {t.name: t for t in (await client.list_tools()).tools}
        self.assertEqual(tools_by_name["github_repository"].input_schema.get("properties", {}), {})
        for name in ("github_issues", "github_pull_requests"):
            with self.subTest(tool=name):
                tool = tools_by_name[name]
                props = tool.input_schema["properties"]
                self.assertEqual(set(props), {"state", "limit"})  # no url, owner, repo, path, method or headers
                self.assertEqual((props["state"]["enum"], props["state"]["default"]), (["open", "closed", "all"], "open"))
                self.assertEqual((props["limit"]["minimum"], props["limit"]["maximum"], props["limit"]["default"]), (1, 50, 10))
        for name in ("github_repository", "github_issues", "github_pull_requests"):
            self.assertTrue(tools_by_name[name].annotations.read_only_hint)
            self.assertIsNotNone(tools_by_name[name].output_schema)

    async def test_round_trips(self) -> None:
        self.queue(respond(repo_json()), respond([issue_json(2), issue_json(1, pr=True)]), respond([pull_json(1)]))
        repo = await self.call("github_repository")
        issues = await self.call("github_issues", {"state": "all", "limit": 5})
        pulls = await self.call("github_pull_requests", {"limit": 5})
        self.assertFalse(repo.is_error or issues.is_error or pulls.is_error)
        self.assertEqual(repo.structured_content["full_name"], "octo-org/hello-world")
        self.assertEqual([i["number"] for i in issues.structured_content["issues"]], [2])
        self.assertEqual(issues.structured_content["pull_requests_excluded"], 1)
        self.assertEqual(pulls.structured_content["pull_requests"][0]["source_branch"], "feature/x")

    async def test_invalid_arguments(self) -> None:
        for tool in ("github_issues", "github_pull_requests"):
            for args in ({"limit": 0}, {"limit": 51}, {"limit": "ten"}, {"state": "merged"}, {"state": 1}):
                with self.subTest(tool=tool, args=args):
                    result = await self.call(tool, args)
                    self.assertTrue(result.is_error)
        self.assertEqual(self.transport.calls, [])

    async def test_smuggled_arguments_cannot_choose_the_target(self) -> None:
        self.queue(respond([]))
        result = await self.call(
            "github_issues",
            {"url": "https://evil.example", "owner": "evil", "repo": "x", "method": "DELETE",
             "headers": {"Authorization": "x"}, "endpoint": "/user"},
        )  # fmt: skip
        self.assertFalse(result.is_error)
        self.assertEqual(self.transport.urls, [f"{API}/issues?state=open&per_page=30&page=1"])

    async def test_errors_become_tool_errors(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        result = await self.call("github_repository", workspace=Workspace(plain))
        self.assertTrue(result.is_error)
        self.assertIn("not a Git repository", result.content[0].text)
        self.queue(respond({"message": "Not Found"}, 404))
        result = await self.call("github_repository")
        self.assertTrue(result.is_error)
        self.assertIn("was not found (HTTP 404)", result.content[0].text)

    async def test_no_origin_is_a_tool_error(self) -> None:
        self.run_git("remote", "remove", "origin")
        result = await self.call("github_pull_requests")
        self.assertTrue(result.is_error)
        self.assertIn("no 'origin' remote", result.content[0].text)

    async def test_token_is_not_logged_printed_or_written(self) -> None:
        os.environ["GITHUB_TOKEN"] = TOKEN
        self.queue(respond(repo_json()), respond({"message": f"Bad credentials {TOKEN}"}, 401), respond([]))

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        root = logging.getLogger()
        handler, old_level = Capture(level=logging.DEBUG), root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        streams = io.StringIO()
        try:
            with contextlib.redirect_stdout(streams), contextlib.redirect_stderr(streams):
                results = [await self.call("github_repository"), await self.call("github_issues"),
                           await self.call("github_pull_requests")]  # fmt: skip
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)

        self.assertTrue(results[1].is_error)  # the 401 path was exercised
        self.assertTrue(records)  # logging really was captured
        for text in records + [streams.getvalue()] + [r.model_dump_json() for r in results]:
            self.assertNotIn(TOKEN, text)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN.encode(), path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
