"""investigate_repository: gather bounded, structured evidence for a developer question.

DevPilot does not answer the question. It turns the query into search terms
with a small deterministic heuristic (see `extract_search_terms`) and collects
evidence with the existing read-only building blocks:

- repository facts        -> tools.repository.analyze_repository
- ranked code/doc matches -> text_search.iter_files / scan_files (the search_code infrastructure)
- source excerpts         -> tools.filesystem.read_file (workspace, binary and size checks)
- Git context             -> tools.git (the Phase 4 execution boundary)
- GitHub context          -> tools.github (the Phase 5 read-only client), best effort

The consuming AI reasons over the evidence. Every list is capped; any cap that
takes effect is named in `truncated_fields`, and incomplete evidence is
explained in `warnings`. Nothing is executed or written.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from devpilot_mcp.github.client import TOKEN_ENV_VAR, GitHubClient
from devpilot_mcp.github.remote import GitHubRemoteError, discover_github_repository
from devpilot_mcp.sensitive import REDACTED, SECRET_VALUE_PATTERNS, is_secret_file_name
from devpilot_mcp.text_search import (
    GENERATED_DIRS,
    SKIPPED_DIRS,
    NotATextFileError,
    iter_files,
    read_text,
    scan_files,
)
from devpilot_mcp.tools import filesystem, git, repository
from devpilot_mcp.tools import github as github_tools
from devpilot_mcp.tools.code_search import MAX_CODE_FILE_BYTES, is_source_file
from devpilot_mcp.tools.common import READ_ONLY, as_tool_error
from devpilot_mcp.workspace import PathNotFoundError, Workspace, WorkspaceError

# --- Limits ------------------------------------------------------------------

MAX_QUERY_CHARS = 500
MIN_TERM_CHARS = 3
MAX_SEARCH_TERMS = 8
MAX_FILES_SCANNED = 20_000
MAX_SCAN_MATCHES = 2_000
MAX_RELEVANT_FILES = 10
MAX_RELEVANT_DIRECTORIES = 5
MAX_CODE_MATCHES = 30
MAX_MATCHES_PER_FILE = 5
MAX_CONTEXT_FILES = 3
MAX_SNIPPETS_PER_FILE = 2
CONTEXT_RADIUS = 4  # lines before and after a matching line
MAX_CONTEXT_LINE_CHARS = 300
MAX_CONTEXT_BYTES = 12_000
MAX_FACT_ITEMS = 10
GIT_COMMITS_SCANNED = 50  # the git_log maximum
MAX_RECENT_COMMITS = 5
MAX_RELEVANT_COMMITS = 5
MAX_RELEVANT_CHANGES = 20
MAX_RELEVANT_DIFFS = 2
MAX_DIFF_CHARS = 4_000
GITHUB_ITEMS_SCANNED = 30
MAX_GITHUB_ITEMS = 5

LIMITS = {
    "max_search_terms": MAX_SEARCH_TERMS,
    "max_files_scanned": MAX_FILES_SCANNED,
    "max_scan_matches": MAX_SCAN_MATCHES,
    "max_relevant_files": MAX_RELEVANT_FILES,
    "max_code_matches": MAX_CODE_MATCHES,
    "max_matches_per_file": MAX_MATCHES_PER_FILE,
    "max_context_files": MAX_CONTEXT_FILES,
    "context_radius_lines": CONTEXT_RADIUS,
    "max_context_bytes": MAX_CONTEXT_BYTES,
    "git_commits_scanned": GIT_COMMITS_SCANNED,
    "max_diff_chars": MAX_DIFF_CHARS,
    "github_items_scanned": GITHUB_ITEMS_SCANNED,
}

EVIDENCE_NOTE = (
    "Evidence only: DevPilot extracted search terms with a keyword heuristic and collected matching "
    "repository facts. It did not interpret the question or answer it, and the evidence may be incomplete."
)

# --- Query heuristic ---------------------------------------------------------

STOPWORDS = frozenset(
    """
    a an the and or but if of in on at to for from by with about into over under via per within across between
    is are was were be been being am do does did done doing has have had can could should would will may might
    must shall what which who whom whose where when why how this that these those there here it its they them
    their we our you your me my all any some each every not no yes also just only very more most other such than
    then so as get gets got make makes made use used uses using work works working worked implemented implement
    implements implementation happen happens handled handles handle handling involved involve responsible located
    defined define find show tell explain describe part parts piece logic code codebase file files function
    functions method methods module modules class classes repository repo project app application system please
    need want like look see thing things stuff currently exist exists whole overall does done kind way ways
    """.split()
)

# A deliberately small table of common developer vocabulary (lookup by word or stem).
SYNONYMS: dict[str, tuple[str, ...]] = {
    "authentication": ("auth", "login"),
    "authenticate": ("auth", "login"),
    "authorization": ("auth", "permission"),
    "login": ("auth",),
    "configuration": ("config", "settings"),
    "config": ("settings",),
    "settings": ("config",),
    "database": ("db", "sql"),
    "error": ("exception",),
    "exception": ("error",),
    "security": ("secure", "sandbox"),
    "registration": ("register",),
    "structure": ("architecture", "layout"),
    "structured": ("architecture", "layout"),
    "architecture": ("structure", "layout"),
    "payment": ("billing", "checkout", "invoice"),
    "logging": ("logger",),
    "api": ("endpoint", "route"),
    "endpoint": ("route", "api"),
}

QUERY_WEIGHT = 2
SYNONYM_WEIGHT = 1
KIND_FACTOR = {"source": 1.0, "test": 0.7, "documentation": 0.6}


def _stem(word: str) -> str:
    """Strip a common English suffix. Matching is by substring, so a short stem still matches its forms."""
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es") and word[:-2].endswith(("ss", "ch", "sh", "x", "z")):
        return word[:-2]
    if len(word) > 6 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 6 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


@dataclass(frozen=True)
class SearchTerm:
    term: str
    source: Literal["query", "synonym"]

    @property
    def weight(self) -> int:
        return QUERY_WEIGHT if self.source == "query" else SYNONYM_WEIGHT


def extract_search_terms(query: str) -> tuple[list[SearchTerm], bool]:
    """Deterministically derive search terms from a question.

    1. Lower-case the query and split it into words of letters, digits and '_'.
    2. Drop stop words, words shorter than 3 characters and pure numbers.
    3. Stem each word lightly (processing -> process, uploads -> upload).
    4. Add synonyms from a small fixed table (authentication -> auth, login).

    Query terms come first in the order they appear, then synonyms; duplicates
    are dropped and at most MAX_SEARCH_TERMS are kept. Returns the terms and
    whether any were dropped because of that cap.
    """
    words = []
    for word in re.findall(r"[a-z0-9_]+", query.lower()):
        word = word.strip("_")
        if len(word) >= MIN_TERM_CHARS and word not in STOPWORDS and any(c.isalpha() for c in word):
            words.append(word)

    candidates = [SearchTerm(_stem(w), "query") for w in words]
    for word in words:
        for synonym in SYNONYMS.get(word) or SYNONYMS.get(_stem(word)) or ():
            candidates.append(SearchTerm(synonym, "synonym"))

    terms: list[SearchTerm] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.term not in seen:
            seen.add(candidate.term)
            terms.append(candidate)
    return terms[:MAX_SEARCH_TERMS], len(terms) > MAX_SEARCH_TERMS


def validate_query(query: object) -> str:
    if not isinstance(query, str):
        raise WorkspaceError("query must be a string.")
    if "\x00" in query:
        raise WorkspaceError("query must not contain null bytes.")
    query = query.strip()
    if not query:
        raise WorkspaceError("query must not be empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise WorkspaceError(f"query must be at most {MAX_QUERY_CHARS} characters.")
    return query


# --- Result models (these also become the tool's output schema) --------------


class SearchTermInfo(BaseModel):
    term: str
    source: Literal["query", "synonym"]


class EntryPointFact(BaseModel):
    file: str
    detail: str


class RepositoryContext(BaseModel):
    name: str
    total_files: int
    scan_complete: bool
    languages: dict[str, int]
    top_directories: list[str]
    documentation_files: list[str]
    configuration_files: list[str]
    dependency_manifests: list[str]
    test_directories: list[str]
    test_file_count: int
    possible_entry_points: list[EntryPointFact]
    framework_indicators: list[str]


FileKind = Literal["source", "test", "documentation"]


class RelevantFile(BaseModel):
    path: str
    kind: FileKind
    score: int
    matched_terms: list[str]
    reasons: list[str]


class RelevantDirectory(BaseModel):
    path: str
    relevant_files: int
    score: int


class CodeMatchEvidence(BaseModel):
    file: str
    line: int
    text: str
    terms: list[str]
    kind: FileKind


class FileSnippet(BaseModel):
    path: str
    start_line: int
    end_line: int
    content: str
    truncated: bool


class ChangedFile(BaseModel):
    path: str
    area: Literal["staged", "unstaged", "untracked"]
    change: str


class CommitSummary(BaseModel):
    short_hash: str
    date: str
    subject: str
    matched_terms: list[str]


class DiffExcerpt(BaseModel):
    path: str
    staged: bool
    diff: str
    truncated: bool


class GitContext(BaseModel):
    available: bool
    reason: str | None = None
    branch: str | None = None
    detached: bool | None = None
    head_commit: str | None = None
    clean: bool | None = None
    changed_file_counts: dict[str, int] = {}
    relevant_changed_files: list[ChangedFile] = []
    recent_commits: list[CommitSummary] = []
    relevant_commits: list[CommitSummary] = []
    commits_scanned: int = 0
    relevant_diffs: list[DiffExcerpt] = []


class GitHubRepositorySummary(BaseModel):
    full_name: str
    description: str | None
    html_url: str
    default_branch: str
    visibility: str | None
    archived: bool
    open_issues_count: int
    pushed_at: str | None


class GitHubIssueEvidence(BaseModel):
    number: int
    title: str
    state: str
    labels: list[str]
    updated_at: str
    html_url: str
    matched_terms: list[str]


class GitHubPullRequestEvidence(BaseModel):
    number: int
    title: str
    state: str
    draft: bool | None
    source_branch: str
    target_branch: str
    updated_at: str
    html_url: str
    matched_terms: list[str]


class GitHubContext(BaseModel):
    available: bool
    reason: str | None = None
    repository: GitHubRepositorySummary | None = None
    relevant_issues: list[GitHubIssueEvidence] = []
    relevant_pull_requests: list[GitHubPullRequestEvidence] = []
    open_issues_scanned: int = 0
    open_pull_requests_scanned: int = 0
    more_open_items_not_scanned: bool = False
    authenticated: bool | None = None


class InvestigationResult(BaseModel):
    query: str
    evidence_note: str
    search_terms: list[SearchTermInfo]
    repository: RepositoryContext
    relevant_files: list[RelevantFile]
    relevant_directories: list[RelevantDirectory]
    code_matches: list[CodeMatchEvidence]
    file_context: list[FileSnippet]
    git_context: GitContext
    github_context: GitHubContext
    files_scanned: int
    warnings: list[str]
    truncated_fields: list[str]
    limits: dict[str, int]


# --- Helpers -----------------------------------------------------------------


class _Redactor:
    """Masks secret-looking values (and the configured GitHub token) in evidence text."""

    def __init__(self) -> None:
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        self._token = token if len(token) >= 8 else ""
        self.count = 0

    def __call__(self, text: str) -> str:
        if self._token and self._token in text:
            self.count += text.count(self._token)
            text = text.replace(self._token, REDACTED)
        for pattern in SECRET_VALUE_PATTERNS:
            text, n = pattern.subn(REDACTED, text)
            self.count += n
        return text


def _is_secret_file(name: str) -> bool:
    # Evidence never includes likely secret files, even if their extension looks like source/config.
    # Templates such as .env.example are safe to show (devpilot_mcp.sensitive).
    return is_secret_file_name(name)


def _file_kind(rel: str) -> FileKind:
    if repository.is_documentation(rel) and not is_source_file(Path(rel)):
        return "documentation"
    if repository.is_test_file(rel) or repository.enclosing_test_directory(rel):
        return "test"
    return "source"


def _terms_in(text: str, terms: list[SearchTerm]) -> list[SearchTerm]:
    lowered = text.lower()
    return [t for t in terms if t.term in lowered]


@dataclass
class _FileEvidence:
    rel: str
    kind: FileKind
    filename_terms: list[SearchTerm] = field(default_factory=list)
    directory_terms: list[SearchTerm] = field(default_factory=list)
    line_counts: Counter = field(default_factory=Counter)
    lines: list[tuple[int, str, list[SearchTerm]]] = field(default_factory=list)
    score: int = 0

    def matched(self, terms: list[SearchTerm]) -> list[SearchTerm]:
        present = {t.term for t in self.filename_terms + self.directory_terms} | set(self.line_counts)
        return [t for t in terms if t.term in present]


def _score(evidence: _FileEvidence, terms: list[SearchTerm], rarity: dict[str, float]) -> int:
    """Deterministic relevance score for one file.

    Per term: 10 points for a file-name hit or 4 for a directory hit, plus 2 + min(lines, 5)
    for content hits, all multiplied by the term's weight (query 2, synonym 1) and its
    rarity. Each extra distinct term adds 4. The total is scaled by the file kind so
    implementation files come before tests and prose.
    """
    filename = {t.term for t in evidence.filename_terms}
    directory = {t.term for t in evidence.directory_terms}
    raw = 0.0
    for t in terms:
        factor = t.weight * rarity.get(t.term, 0.0)
        if t.term in filename:
            raw += 10 * factor
        elif t.term in directory:
            raw += 4 * factor
        count = evidence.line_counts.get(t.term, 0)
        if count:
            raw += factor * (2 + min(count, 5))
    raw += 4 * max(0, len(evidence.matched(terms)) - 1)
    return round(raw * KIND_FACTOR[evidence.kind])


def _reasons(evidence: _FileEvidence, terms: list[SearchTerm]) -> list[str]:
    reasons = [f"file name contains '{t.term}'" for t in evidence.filename_terms]
    reasons += [f"path contains '{t.term}'" for t in evidence.directory_terms]
    for t in terms:
        count = evidence.line_counts.get(t.term, 0)
        if count:
            reasons.append(f"{count} matching line{'s' if count != 1 else ''} for '{t.term}'")
    return reasons


# --- Evidence collectors -----------------------------------------------------


def _repository_context(workspace: Workspace) -> RepositoryContext:
    facts = repository.analyze_repository(workspace)
    return RepositoryContext(
        name=workspace.root.name,
        total_files=facts.total_files,
        scan_complete=facts.scan_complete,
        languages=dict(list(facts.languages.items())[:MAX_FACT_ITEMS]),
        top_directories=[d.path for d in facts.directories if "/" not in d.path][:MAX_FACT_ITEMS],
        documentation_files=facts.documentation_files[:MAX_FACT_ITEMS],
        configuration_files=facts.configuration_files[:MAX_FACT_ITEMS],
        dependency_manifests=facts.dependency_manifests[:MAX_FACT_ITEMS],
        test_directories=facts.tests.directories[:MAX_FACT_ITEMS],
        test_file_count=facts.tests.file_count,
        possible_entry_points=[
            EntryPointFact(file=e.file, detail=e.detail) for e in facts.heuristics.possible_entry_points[:5]
        ],
        framework_indicators=sorted({f.name for f in facts.heuristics.framework_indicators})[:MAX_FACT_ITEMS],
    )


def _candidate_files(workspace: Workspace, warnings: list[str]) -> list[Path]:
    """Source and documentation files, with the Phase 1-3 ignore rules and no secret files."""
    files = []
    for path in iter_files(workspace.root, SKIPPED_DIRS | GENERATED_DIRS):
        if path.is_symlink() or not path.is_file() or _is_secret_file(path.name):
            continue
        rel = workspace.relative(path)
        if is_source_file(path) or repository.is_documentation(rel):
            if len(files) >= MAX_FILES_SCANNED:
                warnings.append(f"Only the first {MAX_FILES_SCANNED:,} candidate files were searched.")
                break
            files.append(path)
    return files


def _is_searchable_text(workspace: Workspace, rel: str) -> bool:
    """The same size and binary/UTF-8 checks the line scan applies."""
    try:
        path = workspace.resolve(rel)
        return path.stat().st_size <= MAX_CODE_FILE_BYTES and read_text(path) is not None
    except (NotATextFileError, WorkspaceError, OSError):
        return False


def _search(
    workspace: Workspace, terms: list[SearchTerm], warnings: list[str], truncated: list[str]
) -> tuple[list[_FileEvidence], int]:
    files = _candidate_files(workspace, warnings)
    evidence: dict[str, _FileEvidence] = {}
    for path in files:
        rel = workspace.relative(path)
        name = PurePosixPath(rel).name.lower()
        parent = str(PurePosixPath(rel).parent).lower()
        item = _FileEvidence(rel=rel, kind=_file_kind(rel))
        for term in terms:
            if term.term in name:
                item.filename_terms.append(term)
            elif term.term in parent:
                item.directory_terms.append(term)
        evidence[rel] = item

    # Files whose path mentions a term are searched first, so the match budget favours them.
    def priority(path: Path) -> tuple[int, str]:
        item = evidence[workspace.relative(path)]
        return (0 if item.filename_terms or item.directory_terms else 1, item.rel)

    needles = [t.term for t in terms]
    scan = scan_files(
        workspace,
        sorted(files, key=priority),
        lambda line: any(n in line.lower() for n in needles),
        max_matches=MAX_SCAN_MATCHES,
        max_file_bytes=MAX_CODE_FILE_BYTES,
    )
    if scan.truncated:
        truncated.append("search_scan")
        warnings.append(
            f"Line matching stopped after {MAX_SCAN_MATCHES:,} matches; files later in the scan order may be missing."
        )
    if scan.files_skipped:
        warnings.append(f"{scan.files_skipped} binary, non-UTF-8, oversized or unreadable files were not searched.")

    for match in scan.matches:
        item = evidence[match.path]
        line_terms = _terms_in(match.line, terms)
        for term in line_terms:
            item.line_counts[term.term] += 1
        item.lines.append((match.line_number, match.line, line_terms))

    # A term found in few files says more than one found everywhere (e.g. the package name).
    document_frequency: Counter = Counter()
    for item in evidence.values():
        for term in item.matched(terms):
            document_frequency[term.term] += 1
    total = max(1, len(evidence))
    rarity = {term: math.log(1 + total / df) for term, df in document_frequency.items()}

    ranked = []
    for item in evidence.values():
        item.score = _score(item, terms, rarity)
        # A file matched only by its name must still be a searchable text file (not binary or oversized).
        if item.score > 0 and (item.lines or _is_searchable_text(workspace, item.rel)):
            ranked.append(item)
    ranked.sort(key=lambda e: (-e.score, e.rel))
    return ranked, scan.files_searched


def _relevant_directories(ranked: list[_FileEvidence]) -> list[RelevantDirectory]:
    scores: dict[str, int] = defaultdict(int)
    counts: Counter = Counter()
    for item in ranked:
        parent = str(PurePosixPath(item.rel).parent)
        scores[parent] += item.score
        counts[parent] += 1
    ordered = sorted(scores, key=lambda d: (-scores[d], d))
    return [RelevantDirectory(path=d, relevant_files=counts[d], score=scores[d]) for d in ordered]


def _best_lines(item: _FileEvidence) -> list[tuple[int, str, list[SearchTerm]]]:
    """A file's matching lines, strongest first (more/heavier terms), then by line number."""
    return sorted(item.lines, key=lambda line: (-sum(t.weight for t in line[2]), line[0]))


def _code_matches(ranked: list[_FileEvidence], redact: _Redactor) -> list[CodeMatchEvidence]:
    matches = []
    for item in ranked:  # grouped by file relevance
        for number, text, line_terms in sorted(_best_lines(item)[:MAX_MATCHES_PER_FILE], key=lambda l: l[0]):
            matches.append(
                CodeMatchEvidence(
                    file=item.rel, line=number, text=redact(text), terms=[t.term for t in line_terms], kind=item.kind
                )
            )
    return matches


def _file_context(
    workspace: Workspace, ranked: list[_FileEvidence], redact: _Redactor, warnings: list[str], truncated: list[str]
) -> list[FileSnippet]:
    """Short windows of source around the strongest matches in the most relevant files."""
    snippets: list[FileSnippet] = []
    budget = MAX_CONTEXT_BYTES
    for item in [i for i in ranked if i.lines][:MAX_CONTEXT_FILES]:
        try:
            lines = filesystem.read_file(workspace, item.rel).content.splitlines()
        except (WorkspaceError, OSError) as exc:
            warnings.append(f"Could not read context from {item.rel}: {as_tool_error(exc)}")
            continue
        anchors = sorted(number for number, _, _ in _best_lines(item)[:MAX_SNIPPETS_PER_FILE])
        windows: list[list[int]] = []
        for anchor in anchors:
            start, end = max(1, anchor - CONTEXT_RADIUS), min(len(lines), anchor + CONTEXT_RADIUS)
            if windows and start <= windows[-1][1] + 1:
                windows[-1][1] = max(windows[-1][1], end)
            else:
                windows.append([start, end])
        for start, end in windows:
            cut = False
            kept: list[str] = []
            for line in lines[start - 1 : end]:
                if len(line) > MAX_CONTEXT_LINE_CHARS:
                    line, cut = line[:MAX_CONTEXT_LINE_CHARS] + " …", True
                cost = len(line.encode("utf-8")) + 1
                if cost > budget:
                    cut = True
                    break
                budget -= cost
                kept.append(line)
            if kept:
                snippets.append(
                    FileSnippet(
                        path=item.rel,
                        start_line=start,
                        end_line=start + len(kept) - 1,
                        content=redact("\n".join(kept)),
                        truncated=cut,
                    )
                )
            if cut and "file_context" not in truncated:
                truncated.append("file_context")
            if budget <= 0:
                return snippets
    return snippets


def _git_context(
    workspace: Workspace, terms: list[SearchTerm], ranked: list[_FileEvidence], redact: _Redactor, warnings: list[str]
) -> GitContext:
    try:
        status = git.git_status(workspace)
    except git.NotAGitRepositoryError as exc:
        return GitContext(available=False, reason=str(exc))
    except (WorkspaceError, OSError) as exc:
        warnings.append(f"Git context unavailable: {as_tool_error(exc)}")
        return GitContext(available=False, reason=str(as_tool_error(exc)))

    context = GitContext(
        available=True,
        branch=status.branch,
        detached=status.detached,
        head_commit=status.head_commit,
        clean=status.clean,
        changed_file_counts=status.counts,
    )
    if not status.complete or status.truncated_fields:
        warnings.append("git status output was truncated; the list of changed files may be incomplete.")

    relevant_paths = {item.rel for item in ranked}
    changes = [ChangedFile(path=c.path, area="staged", change=c.change) for c in status.staged]
    changes += [ChangedFile(path=c.path, area="unstaged", change=c.change) for c in status.unstaged]
    changes += [ChangedFile(path=p, area="untracked", change="added") for p in status.untracked]
    relevant = [c for c in changes if c.path in relevant_paths or _terms_in(c.path, terms)]
    relevant.sort(key=lambda c: (c.path, c.area))
    context.relevant_changed_files = relevant[:MAX_RELEVANT_CHANGES]

    try:
        log = git.git_log(workspace, GIT_COMMITS_SCANNED)
    except (WorkspaceError, OSError) as exc:
        warnings.append(f"Git history unavailable: {as_tool_error(exc)}")
    else:
        context.commits_scanned = len(log.commits)
        for commit in log.commits:
            matched = [t.term for t in _terms_in(f"{commit.subject}\n{commit.body}", terms)]
            summary = CommitSummary(
                short_hash=commit.short_hash, date=commit.date, subject=redact(commit.subject), matched_terms=matched
            )
            if len(context.recent_commits) < MAX_RECENT_COMMITS:
                context.recent_commits.append(summary)
            if matched and len(context.relevant_commits) < MAX_RELEVANT_COMMITS:
                context.relevant_commits.append(summary)

    rank = {item.rel: index for index, item in enumerate(ranked)}
    diffable = sorted(
        (c for c in context.relevant_changed_files if c.area != "untracked"),
        key=lambda c: (rank.get(c.path, len(rank)), c.path, c.area),
    )
    for change in diffable[:MAX_RELEVANT_DIFFS]:
        try:
            diff = git.git_diff(workspace, staged=change.area == "staged", path=change.path)
        except (WorkspaceError, OSError) as exc:
            warnings.append(f"Could not diff {change.path}: {as_tool_error(exc)}")
            continue
        text = diff.diff
        cut = diff.truncated or len(text) > MAX_DIFF_CHARS
        if len(text) > MAX_DIFF_CHARS:
            text = text[:MAX_DIFF_CHARS]
            text = text[: text.rfind("\n") + 1] or text
        context.relevant_diffs.append(
            DiffExcerpt(path=change.path, staged=change.area == "staged", diff=redact(text), truncated=cut)
        )
    return context


def _github_context(
    workspace: Workspace,
    client: GitHubClient,
    terms: list[SearchTerm],
    git_available: bool,
    redact: _Redactor,
    warnings: list[str],
) -> GitHubContext:
    """Best-effort GitHub evidence; any failure leaves the local evidence intact."""
    if not git_available:
        return GitHubContext(available=False, reason="GitHub context needs the workspace to be a Git repository root.")
    try:
        discover_github_repository(workspace)
    except GitHubRemoteError as exc:
        return GitHubContext(available=False, reason=str(exc))
    except (WorkspaceError, OSError) as exc:
        return GitHubContext(available=False, reason=str(as_tool_error(exc)))

    context = GitHubContext(available=True)
    try:
        info = github_tools.github_repository(workspace, client)
        context.repository = GitHubRepositorySummary(
            full_name=info.full_name,
            description=redact(info.description) if info.description else None,
            html_url=info.html_url,
            default_branch=info.default_branch,
            visibility=info.visibility,
            archived=info.archived,
            open_issues_count=info.open_issues_count,
            pushed_at=info.pushed_at,
        )
        context.authenticated = info.authenticated

        issues = github_tools.github_issues(workspace, client, "open", GITHUB_ITEMS_SCANNED)
        context.open_issues_scanned = len(issues.issues)
        for issue in issues.issues:
            matched = [t.term for t in _terms_in(" ".join([issue.title, *issue.labels]), terms)]
            if matched and len(context.relevant_issues) < MAX_GITHUB_ITEMS:
                context.relevant_issues.append(
                    GitHubIssueEvidence(
                        number=issue.number, title=redact(issue.title), state=issue.state, labels=issue.labels,
                        updated_at=issue.updated_at, html_url=issue.html_url, matched_terms=matched,
                    )  # fmt: skip
                )

        pulls = github_tools.github_pull_requests(workspace, client, "open", GITHUB_ITEMS_SCANNED)
        context.open_pull_requests_scanned = len(pulls.pull_requests)
        for pull in pulls.pull_requests:
            matched = [t.term for t in _terms_in(" ".join([pull.title, pull.source_branch, *pull.labels]), terms)]
            if matched and len(context.relevant_pull_requests) < MAX_GITHUB_ITEMS:
                context.relevant_pull_requests.append(
                    GitHubPullRequestEvidence(
                        number=pull.number, title=redact(pull.title), state=pull.state, draft=pull.draft,
                        source_branch=pull.source_branch, target_branch=pull.target_branch,
                        updated_at=pull.updated_at, html_url=pull.html_url, matched_terms=matched,
                    )  # fmt: skip
                )
        context.more_open_items_not_scanned = issues.has_more or pulls.has_more
    except (WorkspaceError, OSError) as exc:
        # Stop at the first failure rather than waiting on further requests.
        warnings.append(f"GitHub context is incomplete: {as_tool_error(exc)}")
        if context.repository is None:
            return GitHubContext(available=False, reason=str(as_tool_error(exc)))
    return context


# --- Tool logic --------------------------------------------------------------


def investigate_repository(workspace: Workspace, client: GitHubClient, query: str) -> InvestigationResult:
    """Collect bounded evidence relevant to a developer question. Does not answer it."""
    query = validate_query(query)
    root = workspace.resolve(".")
    if not root.is_dir():
        raise PathNotFoundError("The workspace root no longer exists or is not a directory.")

    warnings: list[str] = []
    truncated: list[str] = []
    redact = _Redactor()

    terms, terms_capped = extract_search_terms(query)
    if terms_capped:
        truncated.append("search_terms")
        warnings.append(f"Only the first {MAX_SEARCH_TERMS} search terms were used.")
    if not terms:
        warnings.append(
            "No search terms remained after removing common words; only repository, Git and GitHub "
            "context were collected. Rephrase the query with specific names or keywords."
        )

    def cap(name: str, items: list, limit: int) -> list:
        if len(items) > limit:
            truncated.append(name)
            return items[:limit]
        return items

    repo_context = _repository_context(workspace)
    ranked, files_scanned = _search(workspace, terms, warnings, truncated) if terms else ([], 0)
    if terms and not ranked:
        warnings.append("No file paths or lines matched the search terms.")

    code_matches = _code_matches(ranked, redact)
    git_context = _git_context(workspace, terms, ranked, redact, warnings)
    github_context = _github_context(workspace, client, terms, git_context.available, redact, warnings)

    relevant_files = [
        RelevantFile(
            path=item.rel,
            kind=item.kind,
            score=item.score,
            matched_terms=[t.term for t in item.matched(terms)],
            reasons=_reasons(item, terms),
        )
        for item in ranked
    ]
    result = InvestigationResult(
        query=query,
        evidence_note=EVIDENCE_NOTE,
        search_terms=[SearchTermInfo(term=t.term, source=t.source) for t in terms],
        repository=repo_context,
        relevant_files=cap("relevant_files", relevant_files, MAX_RELEVANT_FILES),
        relevant_directories=cap("relevant_directories", _relevant_directories(ranked), MAX_RELEVANT_DIRECTORIES),
        code_matches=cap("code_matches", code_matches, MAX_CODE_MATCHES),
        file_context=_file_context(workspace, ranked, redact, warnings, truncated),
        git_context=git_context,
        github_context=github_context,
        files_scanned=files_scanned,
        warnings=warnings,
        truncated_fields=truncated,
        limits=LIMITS,
    )
    if redact.count:
        result.warnings.append(f"{redact.count} secret-looking value(s) were redacted from the evidence.")
    return result


# --- MCP registration --------------------------------------------------------


def register(server: MCPServer, workspace: Workspace, client: GitHubClient | None = None) -> None:
    """Expose investigate_repository on `server`, bound to the workspace."""
    client = client or GitHubClient()

    # READ_ONLY (openWorldHint=False): its only external calls are the same three fixed
    # api.github.com endpoints the github_* tools use. See tools/common.py.
    @server.tool(name="investigate_repository", annotations=READ_ONLY)
    def investigate_repository_tool(
        query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)],
    ) -> InvestigationResult:
        """Gather bounded, structured evidence for a developer question about the workspace repository.

        Returns evidence, not an answer: search terms derived from the query by a keyword heuristic
        (stop words removed, light stemming, a few synonyms), repository facts, ranked relevant
        files and directories with the reasons they matched, the strongest matching lines, short
        source excerpts around them, Git context (branch, changes, recent and matching commits,
        short diffs of relevant changed files) and, when the origin is on github.com, matching
        open issues and pull requests. Everything is capped; `truncated_fields` and `warnings`
        say when evidence is incomplete. Reason over the evidence yourself and use read_file,
        search_code or git_diff to dig deeper.

        Args:
            query: A developer question, e.g. "How is authentication implemented?" (1-500 characters).
        """
        try:
            return investigate_repository(workspace, client, query)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
