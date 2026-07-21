"""Filesystem metadata store for immutable artifact versions."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..storage.config import StorageConfigType
from ..storage.errors import StorageError
from ..storage.file_backend import _fsync_directory
from ..storage.interfaces import ContentStore
from ..storage.serializers import deserialize_model, serialize_model
from .config import ArtifactConfigType
from .errors import (
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactValidationError,
    ArtifactVersionConflictError,
)
from .interfaces import ArtifactStore
from .models import (
    ArtifactLineage,
    ArtifactLineageStatus,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    is_artifact_id,
    is_artifact_lineage_id,
    new_artifact_id,
    new_artifact_lineage_id,
    utc_now,
)


class FileSystemArtifactStore(ArtifactStore):
    """Store lineage/version metadata while ContentStore owns all bytes."""

    def __init__(
        self,
        *,
        storage_config: StorageConfigType,
        artifact_config: ArtifactConfigType,
        content_store: ContentStore,
        allow_legacy_layout: bool = False,
    ) -> None:
        self.storage_config = storage_config
        self.artifact_config = artifact_config
        self.content_store = content_store

        configured_root = Path(storage_config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = configured_root.resolve(strict=False)
        self.artifacts_dir = self.root / "artifacts"
        self.lineages_dir = self.artifacts_dir / "lineages"
        self.versions_dir = self.artifacts_dir / "versions"
        self.candidates_dir = self.artifacts_dir / "candidates"

        try:
            for directory in (
                self.artifacts_dir,
                self.lineages_dir,
                self.versions_dir,
                self.candidates_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to initialize artifact metadata storage"
            ) from error

        self.legacy_layout_detected = self._detect_legacy_layout()
        self._allow_legacy_layout = allow_legacy_layout

        self._lineage_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._lineage_locks_guard = threading.Lock()

    async def create_lineage(
        self,
        *,
        session_id: str,
        cycle_id: str,
        content_id: str,
        filename: str,
        format_id: str,
        detected_mime_type: str,
        provenance: ArtifactProvenance,
        purpose: ArtifactPurpose = ArtifactPurpose.WORKING,
        declared_mime_type: str | None = None,
        encoding: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        self._require_writable_layout()
        content_metadata = await self._content_metadata(content_id)
        self._enforce_content_limit(content_metadata.size_bytes)

        lineage_id = new_artifact_lineage_id()
        artifact_id = new_artifact_id()
        now = utc_now()
        try:
            version = ArtifactVersion(
                artifact_id=artifact_id,
                artifact_lineage_id=lineage_id,
                version=1,
                parent_artifact_id=None,
                content_id=content_id,
                filename=filename,
                format_id=format_id,
                encoding=encoding,
                declared_mime_type=declared_mime_type,
                detected_mime_type=detected_mime_type,
                size_bytes=content_metadata.size_bytes,
                content_hash=content_metadata.content_hash,
                created_cycle_id=cycle_id,
                created_at=now,
                provenance=provenance,
                metadata=dict(metadata or {}),
            )
            lineage = ArtifactLineage(
                artifact_lineage_id=lineage_id,
                session_id=session_id,
                created_cycle_id=cycle_id,
                current_artifact_id=artifact_id,
                current_version=1,
                committed_artifact_ids=[artifact_id],
                purpose=purpose,
                status=ArtifactLineageStatus.ACTIVE,
                title=title,
                created_at=now,
                updated_at=now,
                metadata={},
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise ArtifactValidationError(
                "invalid_artifact_metadata",
                "Artifact metadata is invalid.",
                retryable=True,
            ) from error

        await asyncio.to_thread(self._write_version_metadata, version)
        try:
            await asyncio.to_thread(self._write_lineage_metadata, lineage)
        except BaseException:
            # The version remains an uncommitted orphan and is intentionally
            # invisible to public reads until a later sweeper removes it.
            raise
        return lineage, version

    async def create_version(
        self,
        *,
        artifact_lineage_id: str,
        expected_current_artifact_id: str,
        cycle_id: str,
        content_id: str,
        filename: str | None,
        format_id: str | None,
        detected_mime_type: str | None,
        provenance: ArtifactProvenance,
        declared_mime_type: str | None = None,
        encoding: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        self._require_writable_layout()
        self._validate_lineage_id(artifact_lineage_id)
        self._validate_artifact_id(expected_current_artifact_id)
        lock = self._acquire_lineage_lock(artifact_lineage_id)
        try:
            async with lock:
                lineage = await asyncio.to_thread(
                    self._load_lineage_metadata,
                    artifact_lineage_id,
                )
                current = await asyncio.to_thread(
                    self._load_committed_version_metadata,
                    lineage.current_artifact_id,
                    lineage,
                )
                if lineage.current_artifact_id != expected_current_artifact_id:
                    raise ArtifactVersionConflictError(
                        artifact_lineage_id,
                        expected_current_artifact_id=(
                            expected_current_artifact_id
                        ),
                        current_artifact_id=lineage.current_artifact_id,
                        current_version=lineage.current_version,
                        current_ref=current.model_dump(mode="json"),
                    )
                if (
                    len(lineage.committed_artifact_ids)
                    >= self.artifact_config.max_versions_per_lineage
                ):
                    raise ArtifactLimitError(
                        "Artifact lineage version limit exceeded"
                    )

                content_metadata = await self._content_metadata(content_id)
                self._enforce_content_limit(content_metadata.size_bytes)
                artifact_id = new_artifact_id()
                now = utc_now()
                try:
                    version = ArtifactVersion(
                        artifact_id=artifact_id,
                        artifact_lineage_id=artifact_lineage_id,
                        version=lineage.current_version + 1,
                        parent_artifact_id=current.artifact_id,
                        content_id=content_id,
                        filename=filename or current.filename,
                        format_id=format_id or current.format_id,
                        encoding=(
                            encoding
                            if encoding is not None
                            else current.encoding
                        ),
                        declared_mime_type=(
                            declared_mime_type
                            if declared_mime_type is not None
                            else current.declared_mime_type
                        ),
                        detected_mime_type=(
                            detected_mime_type
                            or current.detected_mime_type
                        ),
                        size_bytes=content_metadata.size_bytes,
                        content_hash=content_metadata.content_hash,
                        created_cycle_id=cycle_id,
                        created_at=now,
                        provenance=provenance,
                        metadata={
                            **current.metadata,
                            **dict(metadata or {}),
                        },
                    )
                    next_lineage = lineage.model_copy(
                        update={
                            "current_artifact_id": artifact_id,
                            "current_version": lineage.current_version + 1,
                            "committed_artifact_ids": [
                                *lineage.committed_artifact_ids,
                                artifact_id,
                            ],
                            "updated_at": now,
                        }
                    )
                    next_lineage = ArtifactLineage.model_validate(
                        next_lineage.model_dump()
                    )
                except (ValidationError, TypeError, ValueError) as error:
                    raise ArtifactValidationError(
                        "invalid_artifact_metadata",
                        "Artifact version metadata is invalid.",
                        retryable=True,
                    ) from error

                await asyncio.to_thread(
                    self._write_version_metadata,
                    version,
                )
                await asyncio.to_thread(
                    self._replace_lineage_metadata,
                    next_lineage,
                )
                return next_lineage, version
        finally:
            self._release_lineage_lock(artifact_lineage_id, lock)

    async def get_lineage(
        self,
        artifact_lineage_id: str,
    ) -> ArtifactLineage:
        self._validate_lineage_id(artifact_lineage_id)
        return await asyncio.to_thread(
            self._load_lineage_metadata,
            artifact_lineage_id,
        )

    async def get_version(self, artifact_id: str) -> ArtifactVersion:
        self._validate_artifact_id(artifact_id)
        version = await asyncio.to_thread(
            self._load_version_metadata,
            artifact_id,
        )
        lineage = await asyncio.to_thread(
            self._load_lineage_metadata,
            version.artifact_lineage_id,
        )
        version = await asyncio.to_thread(
            self._load_committed_version_metadata,
            artifact_id,
            lineage,
        )
        content_metadata = await self._content_metadata(version.content_id)
        if (
            content_metadata.size_bytes != version.size_bytes
            or content_metadata.content_hash != version.content_hash
        ):
            raise ArtifactIntegrityError(
                "Artifact version and content metadata disagree"
            )
        return version

    async def get_current_version(
        self,
        artifact_lineage_id: str,
    ) -> ArtifactVersion:
        lineage = await self.get_lineage(artifact_lineage_id)
        return await self.get_version(lineage.current_artifact_id)

    async def list_versions(
        self,
        artifact_lineage_id: str,
    ) -> list[ArtifactVersion]:
        lineage = await self.get_lineage(artifact_lineage_id)
        return [
            await self.get_version(artifact_id)
            for artifact_id in lineage.committed_artifact_ids
        ]

    async def list_cycle_artifacts(
        self,
        cycle_id: str,
    ) -> list[ArtifactVersion]:
        cycle_id = self._required_text(cycle_id, "cycle_id")
        result: list[ArtifactVersion] = []
        for lineage in await self._all_lineages():
            for artifact_id in lineage.committed_artifact_ids:
                version = await self.get_version(artifact_id)
                if version.created_cycle_id == cycle_id:
                    result.append(version)
        result.sort(
            key=lambda item: (
                item.created_at,
                item.version,
                item.artifact_id,
            )
        )
        return result

    async def list_lineages(
        self,
        *,
        session_id: str,
        include_archived: bool = False,
    ) -> list[ArtifactLineage]:
        session_id = self._required_text(session_id, "session_id")
        result = [
            lineage
            for lineage in await self._all_lineages()
            if lineage.session_id == session_id
            and (
                include_archived
                or lineage.status == ArtifactLineageStatus.ACTIVE
            )
        ]
        result.sort(
            key=lambda item: (
                item.created_at,
                item.artifact_lineage_id,
            )
        )
        return result

    async def archive_lineage(
        self,
        artifact_lineage_id: str,
        *,
        expected_current_artifact_id: str,
    ) -> ArtifactLineage:
        self._require_writable_layout()
        self._validate_lineage_id(artifact_lineage_id)
        self._validate_artifact_id(expected_current_artifact_id)
        lock = self._acquire_lineage_lock(artifact_lineage_id)
        try:
            async with lock:
                lineage = await asyncio.to_thread(
                    self._load_lineage_metadata,
                    artifact_lineage_id,
                )
                if lineage.current_artifact_id != expected_current_artifact_id:
                    current = await asyncio.to_thread(
                        self._load_committed_version_metadata,
                        lineage.current_artifact_id,
                        lineage,
                    )
                    raise ArtifactVersionConflictError(
                        artifact_lineage_id,
                        expected_current_artifact_id=(
                            expected_current_artifact_id
                        ),
                        current_artifact_id=lineage.current_artifact_id,
                        current_version=lineage.current_version,
                        current_ref=current.model_dump(mode="json"),
                    )
                updated = ArtifactLineage.model_validate(
                    lineage.model_copy(
                        update={
                            "status": ArtifactLineageStatus.ARCHIVED,
                            "updated_at": utc_now(),
                        }
                    ).model_dump()
                )
                await asyncio.to_thread(
                    self._replace_lineage_metadata,
                    updated,
                )
                return updated
        finally:
            self._release_lineage_lock(artifact_lineage_id, lock)

    async def list_orphan_version_ids(self) -> list[str]:
        committed: set[str] = set()
        for lineage in await self._all_lineages():
            committed.update(lineage.committed_artifact_ids)

        result: list[str] = []
        try:
            candidates = list(self.versions_dir.iterdir())
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list artifact versions"
            ) from error
        for candidate in candidates:
            if (
                is_artifact_id(candidate.name)
                and not candidate.is_symlink()
                and candidate.is_dir()
                and candidate.name not in committed
            ):
                result.append(candidate.name)
        result.sort()
        return result

    async def _all_lineages(self) -> list[ArtifactLineage]:
        try:
            candidates = list(self.lineages_dir.iterdir())
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list artifact lineages"
            ) from error

        result: list[ArtifactLineage] = []
        for candidate in candidates:
            if is_artifact_lineage_id(candidate.name):
                result.append(
                    await asyncio.to_thread(
                        self._load_lineage_metadata,
                        candidate.name,
                    )
                )
        return result

    async def _content_metadata(self, content_id: str):
        try:
            return await self.content_store.get_metadata(content_id)
        except Exception as error:
            raise ArtifactIntegrityError(
                "Artifact references unavailable content"
            ) from error

    def _enforce_content_limit(self, size_bytes: int) -> None:
        if size_bytes > self.artifact_config.max_artifact_size_bytes:
            raise ArtifactLimitError(
                "Artifact content exceeds max_artifact_size_bytes"
            )

    def _write_version_metadata(self, version: ArtifactVersion) -> None:
        self._write_new_metadata_object(
            parent=self.versions_dir,
            object_type="artifact version",
            object_id=version.artifact_id,
            model=version,
        )

    def _write_lineage_metadata(self, lineage: ArtifactLineage) -> None:
        self._write_new_metadata_object(
            parent=self.lineages_dir,
            object_type="artifact lineage",
            object_id=lineage.artifact_lineage_id,
            model=lineage,
        )

    def _replace_lineage_metadata(
        self,
        lineage: ArtifactLineage,
    ) -> None:
        object_dir = self._require_object_dir(
            self.lineages_dir,
            "artifact lineage",
            lineage.artifact_lineage_id,
        )
        metadata_path = object_dir / "metadata.json"
        temporary_path = object_dir / (
            f"metadata.json.tmp-{new_artifact_id()}"
        )
        data = self._serialize(
            lineage,
            "artifact lineage",
            lineage.artifact_lineage_id,
        )
        try:
            self._write_file(temporary_path, data)
            os.replace(temporary_path, metadata_path)
            _fsync_directory(object_dir)
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to replace artifact lineage metadata"
            ) from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_new_metadata_object(
        self,
        *,
        parent: Path,
        object_type: str,
        object_id: str,
        model,
    ) -> None:
        final_dir = parent / object_id
        if final_dir.exists() or final_dir.is_symlink():
            raise ArtifactStorageError(
                f"Cannot overwrite existing {object_type}"
            )
        temporary_dir: Path | None = None
        try:
            temporary_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".tmp-{object_id}-",
                    dir=parent,
                )
            )
            data = self._serialize(model, object_type, object_id)
            self._write_file(temporary_dir / "metadata.json", data)
            os.replace(temporary_dir, final_dir)
            temporary_dir = None
            _fsync_directory(parent)
        except ArtifactStorageError:
            raise
        except (OSError, ValueError) as error:
            raise ArtifactStorageError(
                f"Failed to save {object_type}"
            ) from error
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def _load_lineage_metadata(
        self,
        artifact_lineage_id: str,
    ) -> ArtifactLineage:
        result = self._load_metadata(
            parent=self.lineages_dir,
            object_type="artifact lineage",
            object_id=artifact_lineage_id,
            model_type=ArtifactLineage,
        )
        assert isinstance(result, ArtifactLineage)
        return result

    def _load_version_metadata(
        self,
        artifact_id: str,
    ) -> ArtifactVersion:
        result = self._load_metadata(
            parent=self.versions_dir,
            object_type="artifact version",
            object_id=artifact_id,
            model_type=ArtifactVersion,
        )
        assert isinstance(result, ArtifactVersion)
        return result

    def _load_committed_version_metadata(
        self,
        artifact_id: str,
        lineage: ArtifactLineage,
    ) -> ArtifactVersion:
        if artifact_id not in lineage.committed_artifact_ids:
            raise ArtifactNotFoundError(
                f"Unknown committed artifact version {artifact_id}"
            )
        version = self._load_version_metadata(artifact_id)
        expected_version = (
            lineage.committed_artifact_ids.index(artifact_id) + 1
        )
        if (
            version.artifact_lineage_id
            != lineage.artifact_lineage_id
            or version.version != expected_version
        ):
            raise ArtifactIntegrityError(
                "Artifact lineage and version metadata disagree"
            )
        return version

    def _load_metadata(
        self,
        *,
        parent: Path,
        object_type: str,
        object_id: str,
        model_type,
    ):
        object_dir = self._require_object_dir(
            parent,
            object_type,
            object_id,
        )
        metadata_path = object_dir / "metadata.json"
        self._require_regular_file(
            metadata_path,
            object_type,
            object_id,
        )
        try:
            data = metadata_path.read_bytes()
            return deserialize_model(
                data,
                model_type,
                object_type=object_type,
                object_id=object_id,
            )
        except ArtifactStorageError:
            raise
        except StorageError as error:
            raise ArtifactStorageError(
                f"Failed to load {object_type} metadata"
            ) from error
        except OSError as error:
            raise ArtifactStorageError(
                f"Failed to read {object_type} metadata"
            ) from error

    @staticmethod
    def _serialize(model, object_type: str, object_id: str) -> bytes:
        try:
            return serialize_model(
                model,
                object_type=object_type,
                object_id=object_id,
            )
        except StorageError as error:
            raise ArtifactStorageError(
                f"Failed to serialize {object_type} metadata"
            ) from error

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with path.open("wb") as output:
            output.write(data)
            output.flush()
            try:
                os.fsync(output.fileno())
            except OSError:
                pass

    @staticmethod
    def _require_object_dir(
        parent: Path,
        object_type: str,
        object_id: str,
    ) -> Path:
        path = parent / object_id
        if not path.exists() and not path.is_symlink():
            raise ArtifactNotFoundError(
                f"Unknown {object_type} {object_id}"
            )
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise ArtifactIntegrityError(
                f"Failed to inspect {object_type}"
            ) from error
        if path.is_symlink() or not stat.S_ISDIR(mode):
            raise ArtifactIntegrityError(
                f"Invalid {object_type} directory"
            )
        return path

    @staticmethod
    def _require_regular_file(
        path: Path,
        object_type: str,
        object_id: str,
    ) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise ArtifactIntegrityError(
                f"Missing metadata for {object_type} {object_id}"
            ) from error
        except OSError as error:
            raise ArtifactIntegrityError(
                f"Failed to inspect metadata for {object_type}"
            ) from error
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise ArtifactIntegrityError(
                f"Invalid metadata for {object_type}"
            )

    def _detect_legacy_layout(self) -> bool:
        try:
            return any(
                not candidate.is_symlink()
                and candidate.is_dir()
                and is_artifact_id(candidate.name)
                for candidate in self.artifacts_dir.iterdir()
            )
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to inspect artifact storage layout"
            ) from error

    def _require_writable_layout(self) -> None:
        if self.legacy_layout_detected and not self._allow_legacy_layout:
            raise ArtifactStorageError(
                "Legacy artifact layout detected; run the artifact migration"
            )

    def _acquire_lineage_lock(
        self,
        lineage_id: str,
    ) -> asyncio.Lock:
        with self._lineage_locks_guard:
            entry = self._lineage_locks.get(lineage_id)
            if entry is None:
                lock = asyncio.Lock()
                count = 0
            else:
                lock, count = entry
            self._lineage_locks[lineage_id] = (lock, count + 1)
            return lock

    def _release_lineage_lock(
        self,
        lineage_id: str,
        lock: asyncio.Lock,
    ) -> None:
        with self._lineage_locks_guard:
            entry = self._lineage_locks.get(lineage_id)
            if entry is None or entry[0] is not lock:
                return
            count = entry[1] - 1
            if count <= 0:
                self._lineage_locks.pop(lineage_id, None)
            else:
                self._lineage_locks[lineage_id] = (lock, count)

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ArtifactValidationError(
                f"invalid_{field_name}",
                f"{field_name} must not be empty.",
            )
        return value.strip()

    @staticmethod
    def _validate_lineage_id(value: str) -> None:
        if not isinstance(value, str) or not is_artifact_lineage_id(value):
            raise ArtifactValidationError(
                "invalid_artifact_lineage_id",
                "artifact_lineage_id is invalid.",
            )

    @staticmethod
    def _validate_artifact_id(value: str) -> None:
        if not isinstance(value, str) or not is_artifact_id(value):
            raise ArtifactValidationError(
                "invalid_artifact_id",
                "artifact_id is invalid.",
            )
