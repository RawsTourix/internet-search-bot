"""Durable storage for tool-output candidates awaiting explicit promotion."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ..storage.config import StorageConfigType
from ..storage.file_backend import _fsync_directory
from ..storage.serializers import deserialize_model, serialize_model
from .errors import (
    ArtifactCandidateError,
    ArtifactNotFoundError,
    ArtifactStorageError,
)
from .models import (
    ArtifactCandidate,
    ArtifactCandidateStatus,
    is_artifact_candidate_id,
)


@runtime_checkable
class ArtifactCandidateStore(Protocol):
    async def create(self, candidate: ArtifactCandidate) -> ArtifactCandidate:
        ...

    async def get(self, candidate_id: str) -> ArtifactCandidate:
        ...

    async def list_cycle(
        self,
        *,
        session_id: str,
        cycle_id: str,
        include_terminal: bool = False,
    ) -> list[ArtifactCandidate]:
        ...

    async def mark_promoted(
        self,
        candidate_id: str,
        *,
        artifact_id: str,
    ) -> ArtifactCandidate:
        ...

    async def mark_discarded(self, candidate_id: str) -> ArtifactCandidate:
        ...


class FileSystemArtifactCandidateStore(ArtifactCandidateStore):
    """Persist candidates separately from committed artifact lineages."""

    def __init__(self, storage_config: StorageConfigType) -> None:
        configured_root = Path(storage_config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = (
            configured_root.resolve(strict=False)
            / "artifacts"
            / "candidates"
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to initialize artifact candidate storage"
            ) from error
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = threading.Lock()

    async def create(self, candidate: ArtifactCandidate) -> ArtifactCandidate:
        return await asyncio.to_thread(self._create_sync, candidate)

    async def get(self, candidate_id: str) -> ArtifactCandidate:
        self._validate_id(candidate_id)
        return await asyncio.to_thread(self._load_sync, candidate_id)

    async def list_cycle(
        self,
        *,
        session_id: str,
        cycle_id: str,
        include_terminal: bool = False,
    ) -> list[ArtifactCandidate]:
        values = await asyncio.to_thread(self._load_all_sync)
        result = [
            item
            for item in values
            if item.session_id == session_id
            and item.cycle_id == cycle_id
            and (
                include_terminal
                or item.status == ArtifactCandidateStatus.AVAILABLE
            )
        ]
        result.sort(key=lambda item: (item.created_at, item.candidate_id))
        return result

    async def mark_promoted(
        self,
        candidate_id: str,
        *,
        artifact_id: str,
    ) -> ArtifactCandidate:
        return await self._transition(
            candidate_id,
            ArtifactCandidateStatus.PROMOTED,
            promoted_artifact_id=artifact_id,
        )

    async def mark_discarded(self, candidate_id: str) -> ArtifactCandidate:
        return await self._transition(
            candidate_id,
            ArtifactCandidateStatus.DISCARDED,
            promoted_artifact_id=None,
        )

    async def _transition(
        self,
        candidate_id: str,
        status: ArtifactCandidateStatus,
        *,
        promoted_artifact_id: str | None,
    ) -> ArtifactCandidate:
        self._validate_id(candidate_id)
        lock = self._acquire_lock(candidate_id)
        try:
            async with lock:
                current = await asyncio.to_thread(
                    self._load_sync,
                    candidate_id,
                )
                if current.status == status:
                    if current.promoted_artifact_id == promoted_artifact_id:
                        return current
                    raise ArtifactCandidateError(
                        "Candidate terminal state disagrees with requested target"
                    )
                if current.status != ArtifactCandidateStatus.AVAILABLE:
                    raise ArtifactCandidateError(
                        "Candidate is already terminal: "
                        f"{current.status.value}"
                    )
                try:
                    updated = ArtifactCandidate.model_validate(
                        current.model_copy(
                            update={
                                "status": status,
                                "promoted_artifact_id": promoted_artifact_id,
                            }
                        ).model_dump()
                    )
                except ValidationError as error:
                    raise ArtifactCandidateError(
                        "Candidate transition is invalid"
                    ) from error
                await asyncio.to_thread(self._replace_sync, updated)
                return updated
        finally:
            self._release_lock(candidate_id, lock)

    def _candidate_dir(self, candidate_id: str) -> Path:
        self._validate_id(candidate_id)
        return self.root / candidate_id

    def _metadata_path(self, candidate_id: str) -> Path:
        return self._candidate_dir(candidate_id) / "metadata.json"

    def _create_sync(self, candidate: ArtifactCandidate) -> ArtifactCandidate:
        target = self._candidate_dir(candidate.candidate_id)
        if target.is_symlink():
            raise ArtifactStorageError(
                "Artifact candidate path must not be a symlink"
            )
        if target.exists():
            existing = self._load_sync(candidate.candidate_id)
            if existing == candidate:
                return existing
            raise ArtifactCandidateError("Candidate ID already exists")
        temp = Path(tempfile.mkdtemp(prefix=".cand-", dir=self.root))
        try:
            path = temp / "metadata.json"
            path.write_bytes(serialize_model(candidate))
            self._fsync_file(path)
            os.replace(temp, target)
            _fsync_directory(self.root)
            return candidate
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def _replace_sync(self, candidate: ArtifactCandidate) -> None:
        directory = self._candidate_dir(candidate.candidate_id)
        target = directory / "metadata.json"
        self._reject_symlink(directory, target)
        if not target.is_file():
            raise ArtifactNotFoundError(
                f"Artifact candidate not found: {candidate.candidate_id}"
            )
        fd, temporary_name = tempfile.mkstemp(
            prefix=".metadata-",
            suffix=".json",
            dir=directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(serialize_model(candidate))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _load_sync(self, candidate_id: str) -> ArtifactCandidate:
        directory = self._candidate_dir(candidate_id)
        path = directory / "metadata.json"
        self._reject_symlink(directory, path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactNotFoundError(
                f"Artifact candidate not found: {candidate_id}"
            ) from error
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to read artifact candidate metadata"
            ) from error
        try:
            return deserialize_model(raw, ArtifactCandidate)
        except (ValidationError, ValueError, TypeError) as error:
            raise ArtifactStorageError(
                "Artifact candidate metadata is invalid"
            ) from error

    def _load_all_sync(self) -> list[ArtifactCandidate]:
        result: list[ArtifactCandidate] = []
        try:
            directories = list(self.root.iterdir())
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list artifact candidates"
            ) from error
        for directory in directories:
            if directory.name.startswith("."):
                continue
            if directory.is_symlink():
                raise ArtifactStorageError(
                    "Artifact candidate directory must not be a symlink"
                )
            if not directory.is_dir():
                continue
            if not is_artifact_candidate_id(directory.name):
                continue
            result.append(self._load_sync(directory.name))
        return result

    @staticmethod
    def _reject_symlink(directory: Path, path: Path) -> None:
        if directory.is_symlink() or path.is_symlink():
            raise ArtifactStorageError(
                "Artifact candidate metadata must not use symlinks"
            )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _validate_id(candidate_id: str) -> None:
        if not is_artifact_candidate_id(candidate_id):
            raise ArtifactCandidateError("Invalid artifact candidate ID")

    def _acquire_lock(self, candidate_id: str) -> asyncio.Lock:
        with self._locks_guard:
            entry = self._locks.get(candidate_id)
            if entry is None:
                lock = asyncio.Lock()
                self._locks[candidate_id] = (lock, 1)
                return lock
            lock, users = entry
            self._locks[candidate_id] = (lock, users + 1)
            return lock

    def _release_lock(self, candidate_id: str, lock: asyncio.Lock) -> None:
        with self._locks_guard:
            entry = self._locks.get(candidate_id)
            if entry is None or entry[0] is not lock:
                return
            users = entry[1] - 1
            if users <= 0 and not lock.locked():
                self._locks.pop(candidate_id, None)
            else:
                self._locks[candidate_id] = (lock, users)
