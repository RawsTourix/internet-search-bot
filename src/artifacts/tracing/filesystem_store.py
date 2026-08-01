"""Filesystem JSONL persistence for artifact lifecycle traces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ...storage.config import StorageConfigType
from ..errors import ArtifactIntegrityError, ArtifactStorageError
from .models import ArtifactTraceEvent


class FileSystemArtifactTraceStore:
    """Append validated events to daily, size-bounded session JSONL files."""

    def __init__(
        self,
        storage_config: StorageConfigType,
        *,
        max_file_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_file_bytes < 1024:
            raise ValueError("artifact trace max file size is too small")
        configured = Path(storage_config.root_dir).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        self.root = configured.resolve(strict=False) / "artifact_traces"
        self.atomic_writes = storage_config.atomic_writes
        self.max_file_bytes = int(max_file_bytes)
        self._lock = threading.RLock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to initialize artifact trace storage"
            ) from error

    async def append(self, event: ArtifactTraceEvent) -> None:
        await asyncio.to_thread(self._append_sync, event)

    async def list_session(self, session_id: str) -> list[ArtifactTraceEvent]:
        return await asyncio.to_thread(self._list_session_sync, session_id)

    def _append_sync(self, event: ArtifactTraceEvent) -> None:
        payload = event.model_dump(mode="json")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        session_dir = self._session_dir(event.session_id)
        with self._lock:
            try:
                session_dir.mkdir(parents=True, exist_ok=True)
                self._write_session_metadata_sync(
                    session_dir,
                    session_id=event.session_id,
                    occurred_at=event.occurred_at,
                )
                path = self._select_trace_path_sync(
                    session_dir,
                    occurred_at=event.occurred_at,
                    additional_bytes=len(encoded),
                )
                with path.open("ab") as output:
                    output.write(encoded)
                    output.flush()
                    if self.atomic_writes:
                        try:
                            os.fsync(output.fileno())
                        except OSError:
                            pass
            except OSError as error:
                raise ArtifactStorageError(
                    "Failed to append artifact trace event"
                ) from error

    def _list_session_sync(self, session_id: str) -> list[ArtifactTraceEvent]:
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return []
        if session_dir.is_symlink() or not session_dir.is_dir():
            raise ArtifactIntegrityError("Invalid artifact trace session directory")
        result: list[ArtifactTraceEvent] = []
        for path in sorted(session_dir.glob("*.jsonl")):
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise ArtifactIntegrityError("Invalid artifact trace file")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as error:
                raise ArtifactStorageError(
                    "Failed to read artifact trace file"
                ) from error
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = ArtifactTraceEvent.model_validate_json(line)
                except (ValidationError, ValueError) as error:
                    raise ArtifactIntegrityError(
                        "Invalid artifact trace event"
                    ) from error
                if event.session_id != session_id:
                    raise ArtifactIntegrityError(
                        "Artifact trace session authority mismatch"
                    )
                result.append(event)
        result.sort(key=lambda item: (item.occurred_at, item.event_id))
        return result

    def _session_dir(self, session_id: str) -> Path:
        normalized = session_id.strip()
        if not normalized:
            raise ValueError("artifact trace session ID must not be empty")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
        return self.root / f"session_{digest}"

    def _select_trace_path_sync(
        self,
        session_dir: Path,
        *,
        occurred_at: datetime,
        additional_bytes: int,
    ) -> Path:
        day = occurred_at.astimezone(timezone.utc).date().isoformat()
        index = 0
        while True:
            suffix = "" if index == 0 else f".{index:03d}"
            path = session_dir / f"{day}{suffix}.jsonl"
            current_size = path.stat().st_size if path.exists() else 0
            if current_size + additional_bytes <= self.max_file_bytes:
                return path
            index += 1

    def _write_session_metadata_sync(
        self,
        session_dir: Path,
        *,
        session_id: str,
        occurred_at: datetime,
    ) -> None:
        path = session_dir / "session.json"
        created_at = occurred_at.astimezone(timezone.utc).isoformat()
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ArtifactIntegrityError(
                    "Invalid artifact trace session metadata"
                ) from error
            if existing.get("session_id") != session_id:
                raise ArtifactIntegrityError(
                    "Artifact trace session metadata mismatch"
                )
            created_at = str(existing.get("created_at") or created_at)
        payload = {
            "schema_version": 1,
            "trace_type": "artifact_session",
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": occurred_at.astimezone(timezone.utc).isoformat(),
        }
        temporary = session_dir / ".session.json.tmp"
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
