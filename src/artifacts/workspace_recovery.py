"""Startup cleanup for isolated artifact workspaces left by process failure."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .errors import ArtifactStorageError, ArtifactWorkspaceError
from .workspace import ArtifactWorkspaceManager


async def cleanup_stale_artifact_workspaces(
    manager: ArtifactWorkspaceManager,
    *,
    ttl_seconds: int,
    now_timestamp: float | None = None,
) -> list[str]:
    """Remove old workspace directories without following filesystem links."""

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    now = time.time() if now_timestamp is None else float(now_timestamp)
    cutoff = now - ttl_seconds

    def candidates() -> list[tuple[Path, bool]]:
        result: list[tuple[Path, bool]] = []
        try:
            entries = list(manager.root.iterdir())
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list artifact workspaces for recovery"
            ) from error
        for entry in entries:
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ArtifactStorageError(
                    "Failed to inspect artifact workspace"
                ) from error
            if metadata.st_mtime > cutoff:
                continue
            result.append((entry, entry.is_symlink()))
        return result

    removed: list[str] = []
    for path, is_symlink in await asyncio.to_thread(candidates):
        if is_symlink:
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as error:
                raise ArtifactWorkspaceError(
                    "Failed to remove stale workspace symlink"
                ) from error
            removed.append(path.name)
            continue
        if not path.is_dir():
            # Unknown regular files under workspaces are not valid runtime state.
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as error:
                raise ArtifactWorkspaceError(
                    "Failed to remove stale workspace entry"
                ) from error
            removed.append(path.name)
            continue
        await manager.cleanup_path(path)
        removed.append(path.name)
    return removed
