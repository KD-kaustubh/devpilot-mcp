"""Write tools: apply_patch and revert_patch.

DevPilot never decides what to change. The caller supplies an explicit unified
diff; DevPilot validates the whole patch, applies exactly that patch atomically
and records it so the caller can revert it by change_id. These are the only
tools that modify files. They run no commands and never use Git to write.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from devpilot_mcp.patching import changes
from devpilot_mcp.patching.changes import CHANGE_ID_PATTERN, MAX_PATCH_BYTES, ChangeRegistry, sha256
from devpilot_mcp.tools.common import WRITES_FILES, as_tool_error
from devpilot_mcp.workspace import Workspace, WorkspaceError


class PatchedFile(BaseModel):
    path: str
    change: Literal["modified", "created", "deleted"]
    additions: int
    deletions: int
    sha256_before: str | None
    sha256_after: str | None


class ApplyPatchResult(BaseModel):
    change_id: str
    patch_sha256: str
    applied_at: str
    files_changed: list[PatchedFile]
    files_changed_count: int
    additions: int
    deletions: int
    reversible: bool
    evicted_change_ids: list[str]


class RestoredFile(BaseModel):
    path: str
    action: Literal["restored", "removed", "recreated"]


class RevertPatchResult(BaseModel):
    change_id: str
    reverted: bool
    files_restored: list[RestoredFile]
    files_restored_count: int


_REVERT_ACTIONS = {"modified": "restored", "created": "removed", "deleted": "recreated"}


def apply_patch(workspace: Workspace, registry: ChangeRegistry, patch: object) -> ApplyPatchResult:
    applied = changes.apply_patch(workspace, registry, patch)
    record = applied.record
    files = [
        PatchedFile(
            path=f.rel,
            change=f.kind,
            additions=f.additions,
            deletions=f.deletions,
            sha256_before=sha256(f.original) if f.original is not None else None,
            sha256_after=f.result_sha256,
        )
        for f in record.files
    ]
    return ApplyPatchResult(
        change_id=record.change_id,
        patch_sha256=record.patch_sha256,
        applied_at=record.applied_at,
        files_changed=files,
        files_changed_count=len(files),
        additions=sum(f.additions for f in files),
        deletions=sum(f.deletions for f in files),
        reversible=applied.reversible,
        evicted_change_ids=applied.evicted,
    )


def revert_patch(workspace: Workspace, registry: ChangeRegistry, change_id: object) -> RevertPatchResult:
    record = changes.revert_patch(workspace, registry, change_id)
    restored = [RestoredFile(path=f.rel, action=_REVERT_ACTIONS[f.kind]) for f in record.files]  # type: ignore[arg-type]
    return RevertPatchResult(
        change_id=record.change_id, reverted=True, files_restored=restored, files_restored_count=len(restored)
    )


def register(server: MCPServer, workspace: Workspace, registry: ChangeRegistry | None = None) -> None:
    """Expose apply_patch and revert_patch; changes are tracked in a per-server in-memory registry."""
    registry = registry or ChangeRegistry()

    @server.tool(name="apply_patch", annotations=WRITES_FILES)
    def apply_patch_tool(patch: Annotated[str, Field(min_length=1, max_length=MAX_PATCH_BYTES)]) -> ApplyPatchResult:
        """Apply an explicit unified diff that YOU supply to text files in the workspace. Writes files.

        DevPilot does not generate or alter the patch: it validates the whole patch, then applies
        exactly that patch atomically, or changes nothing. Supported: standard unified diff
        sections (`--- a/path`, `+++ b/path`, `@@ -l,c +l,c @@` hunks) that modify, create
        (`--- /dev/null`) or delete (`+++ /dev/null`) UTF-8 text files. Hunks must match the
        current file exactly at their stated line numbers; a stale patch is rejected. Binary
        patches, renames, mode changes, paths outside the workspace, links, .git and secret files
        (.env*, keys, credentials; .env.example is allowed) are rejected. Limits: 256 KB patch,
        20 files, 100 hunks, 2,000 added and 2,000 deleted lines, 1 MB per file.

        Returns a change_id to pass to revert_patch, the patch SHA-256, and per-file line counts
        and SHA-256 hashes before/after.

        Args:
            patch: The complete unified diff text.
        """
        try:
            return apply_patch(workspace, registry, patch)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc

    @server.tool(name="revert_patch", annotations=WRITES_FILES)
    def revert_patch_tool(change_id: Annotated[str, Field(pattern=CHANGE_ID_PATTERN)]) -> RevertPatchResult:
        """Undo a change made by apply_patch in this server session, restoring the exact previous bytes. Writes files.

        Only change_ids returned by apply_patch since the server started can be reverted, each
        once. If any file of the change was modified after the patch was applied, the revert is
        refused and nothing is changed. Git is never used.

        Args:
            change_id: The change_id returned by apply_patch, e.g. "chg_0123456789abcdef".
        """
        try:
            return revert_patch(workspace, registry, change_id)
        except (WorkspaceError, OSError) as exc:
            raise as_tool_error(exc) from exc
