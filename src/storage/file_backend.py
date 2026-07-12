"""Local filesystem implementations of the storage protocols."""

import asyncio
import codecs
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .config import StorageConfigType
from .errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StorageSerializationError,
    StorageValidationError,
)
from .interfaces import ArtifactStore, ContentStore
from .models import (
    ArtifactRef,
    ContentMatch,
    ContentMetadata,
    ContentRange,
    ContentRef,
    is_artifact_id,
    is_content_id,
    new_artifact_id,
    new_content_id,
)
from .serializers import deserialize_model, serialize_model


_READ_BLOCK_BYTES = 64 * 1024
_EXCERPT_RADIUS_CHARS = 120


class _AtomicFileBackend:
    """Shared path, atomic-write, and integrity primitives for file stores."""

    def __init__(self, config: StorageConfigType):
        self.config = config
        configured_root = Path(config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = configured_root.resolve(strict=False)
        self.contents_dir = self.root / "contents"
        self.artifacts_dir = self.root / "artifacts"
        self.cycles_dir = self.root / "cycles"
        self.plans_dir = self.root / "plans"
        self.input_batches_dir = self.root / "input_batches"
        self.indexes_dir = self.root / "indexes"
        try:
            for directory in (
                self.contents_dir,
                self.artifacts_dir,
                self.cycles_dir,
                self.plans_dir,
                self.input_batches_dir,
                self.indexes_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError("Failed to initialize storage root") from error

    @staticmethod
    def hash_bytes(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def write_new_object(
        self,
        *,
        parent: Path,
        object_type: str,
        object_id: str,
        binary_name: str,
        content: bytes,
        metadata_bytes: bytes,
    ) -> None:
        """Write one complete object, exposing it only after all files succeed."""
        final_dir = parent / object_id
        if final_dir.exists() or final_dir.is_symlink():
            raise StorageValidationError(
                f"Cannot overwrite existing {object_type} {object_id}"
            )

        temporary_dir: Path | None = None
        try:
            if self.config.atomic_writes:
                temporary_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".tmp-{object_id}-",
                        dir=parent,
                    )
                )
                target_dir = temporary_dir
            else:
                final_dir.mkdir(parents=False, exist_ok=False)
                target_dir = final_dir

            self._write_file(target_dir / binary_name, content)
            self._write_file(target_dir / "metadata.json", metadata_bytes)

            if self.config.atomic_writes:
                os.replace(temporary_dir, final_dir)
                temporary_dir = None
        except StorageError:
            raise
        except (OSError, ValueError) as error:
            raise StorageError(
                f"Failed to save {object_type} {object_id}"
            ) from error
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
            if not self.config.atomic_writes and final_dir.exists():
                # A direct-write object is valid only when both files exist.
                if not (final_dir / binary_name).is_file() or not (
                    final_dir / "metadata.json"
                ).is_file():
                    shutil.rmtree(final_dir, ignore_errors=True)

    def update_metadata(
        self,
        *,
        object_dir: Path,
        object_type: str,
        object_id: str,
        metadata_bytes: bytes,
    ) -> None:
        """Replace metadata without rewriting the stored payload."""
        metadata_path = object_dir / "metadata.json"
        temporary_path = object_dir / f"metadata.json.tmp-{uuid4().hex}"
        try:
            if self.config.atomic_writes:
                self._write_file(temporary_path, metadata_bytes)
                os.replace(temporary_path, metadata_path)
            else:
                self._write_file(metadata_path, metadata_bytes)
        except (OSError, ValueError) as error:
            raise StorageError(
                f"Failed to update {object_type} {object_id} metadata"
            ) from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def load_metadata(
        self,
        *,
        parent: Path,
        object_type: str,
        object_id: str,
        model_type: type[ContentMetadata] | type[ArtifactRef],
    ) -> ContentMetadata | ArtifactRef:
        object_dir = self.require_object_dir(parent, object_type, object_id)
        metadata_path = object_dir / "metadata.json"
        self.require_regular_file(metadata_path, object_type, object_id, "metadata")
        try:
            data = metadata_path.read_bytes()
        except OSError as error:
            raise StorageSerializationError(
                f"Failed to read {object_type} {object_id} metadata"
            ) from error
        return deserialize_model(
            data,
            model_type,
            object_type=object_type,
            object_id=object_id,
        )

    def verify_payload(
        self,
        *,
        object_dir: Path,
        object_type: str,
        object_id: str,
        binary_name: str,
        expected_size: int,
        expected_hash: str,
    ) -> Path:
        binary_path = object_dir / binary_name
        self.require_regular_file(binary_path, object_type, object_id, "binary payload")
        try:
            actual_size = binary_path.lstat().st_size
            if actual_size != expected_size:
                raise StorageIntegrityError(
                    f"Size mismatch for {object_type} {object_id}"
                )

            if self.config.verify_content_hash:
                digest = hashlib.sha256()
                with binary_path.open("rb") as binary_file:
                    while block := binary_file.read(_READ_BLOCK_BYTES):
                        digest.update(block)
                if f"sha256:{digest.hexdigest()}" != expected_hash:
                    raise StorageIntegrityError(
                        f"Hash mismatch for {object_type} {object_id}"
                    )
        except StorageIntegrityError:
            raise
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to verify {object_type} {object_id}"
            ) from error
        return binary_path

    @staticmethod
    def require_object_dir(parent: Path, object_type: str, object_id: str) -> Path:
        object_dir = parent / object_id
        if not object_dir.exists() and not object_dir.is_symlink():
            raise StorageNotFoundError(f"Unknown {object_type} {object_id}")
        try:
            mode = object_dir.lstat().st_mode
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to inspect {object_type} {object_id}"
            ) from error
        if object_dir.is_symlink() or not stat.S_ISDIR(mode):
            raise StorageIntegrityError(
                f"Invalid object directory for {object_type} {object_id}"
            )
        return object_dir

    @staticmethod
    def require_regular_file(
        path: Path,
        object_type: str,
        object_id: str,
        role: str,
    ) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise StorageIntegrityError(
                f"Missing {role} for {object_type} {object_id}"
            ) from error
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to inspect {role} for {object_type} {object_id}"
            ) from error
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise StorageIntegrityError(
                f"Invalid {role} for {object_type} {object_id}"
            )

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with path.open("wb") as output:
            output.write(data)
            output.flush()
            try:
                os.fsync(output.fileno())
            except OSError:
                # Directory/file fsync support differs on Windows and filesystems.
                pass


class FileSystemContentStore(ContentStore):
    """Filesystem-backed storage for arbitrary content."""

    def __init__(self, backend: _AtomicFileBackend | StorageConfigType):
        self._backend = (
            backend if isinstance(backend, _AtomicFileBackend) else _AtomicFileBackend(backend)
        )
        self.config = self._backend.config

    async def save_content(
        self,
        content: bytes | str,
        *,
        source_type: str,
        source_name: str | None = None,
        mime_type: str | None = None,
        encoding: str | None = None,
        cycle_id: str | None = None,
        tool_call_id: str | None = None,
        size_tokens_estimate: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContentRef:
        """Persist content and return an opaque reference."""
        return await asyncio.to_thread(
            self._save_content_sync,
            content,
            source_type,
            source_name,
            mime_type,
            encoding,
            cycle_id,
            tool_call_id,
            size_tokens_estimate,
            metadata,
        )

    def _save_content_sync(
        self,
        content: bytes | str,
        source_type: str,
        source_name: str | None,
        mime_type: str | None,
        encoding: str | None,
        cycle_id: str | None,
        tool_call_id: str | None,
        size_tokens_estimate: int | None,
        metadata: dict[str, Any] | None,
    ) -> ContentRef:
        content_id = new_content_id()
        if isinstance(content, str):
            actual_encoding = encoding or "utf-8"
            try:
                content_bytes = content.encode(actual_encoding)
            except (LookupError, UnicodeEncodeError) as error:
                raise StorageValidationError(
                    f"Invalid encoding for content {content_id}"
                ) from error
            size_chars: int | None = len(content)
            actual_mime = mime_type or "text/plain"
        elif isinstance(content, bytes):
            content_bytes = content
            actual_encoding = encoding
            size_chars = None
            if actual_encoding is not None:
                try:
                    size_chars = len(content.decode(actual_encoding))
                except UnicodeDecodeError:
                    # Preserve the original bytes; callers can still use byte-range reads.
                    size_chars = None
                except LookupError as error:
                    raise StorageValidationError(
                        f"Invalid encoding for content {content_id}"
                    ) from error
            actual_mime = mime_type or "application/octet-stream"
        else:
            raise StorageValidationError(
                f"Content {content_id} must be bytes or str"
            )

        try:
            content_metadata = ContentMetadata(
                content_id=content_id,
                source_type=source_type,
                source_name=source_name,
                mime_type=actual_mime,
                size_bytes=len(content_bytes),
                size_chars=size_chars,
                size_tokens_estimate=size_tokens_estimate,
                content_hash=self._backend.hash_bytes(content_bytes),
                created_at=datetime.now(timezone.utc),
                metadata=dict(metadata or {}),
                encoding=actual_encoding,
                cycle_id=cycle_id,
                tool_call_id=tool_call_id,
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise StorageValidationError(
                f"Invalid metadata for content {content_id}"
            ) from error
        metadata_bytes = serialize_model(
            content_metadata,
            object_type="content",
            object_id=content_id,
        )
        self._backend.write_new_object(
            parent=self._backend.contents_dir,
            object_type="content",
            object_id=content_id,
            binary_name="content.bin",
            content=content_bytes,
            metadata_bytes=metadata_bytes,
        )
        return self._public_ref(content_metadata)

    async def get_metadata(self, content_id: str) -> ContentMetadata:
        """Load content metadata."""
        return await asyncio.to_thread(self._get_metadata_sync, content_id)

    def _get_metadata_sync(self, content_id: str) -> ContentMetadata:
        self._validate_content_id(content_id)
        result = self._backend.load_metadata(
            parent=self._backend.contents_dir,
            object_type="content",
            object_id=content_id,
            model_type=ContentMetadata,
        )
        assert isinstance(result, ContentMetadata)
        return result

    async def read_content(self, content_id: str) -> bytes:
        """Read complete content within the configured memory limit."""
        return await asyncio.to_thread(self._read_content_sync, content_id)

    def _read_content_sync(self, content_id: str) -> bytes:
        content_metadata = self._get_metadata_sync(content_id)
        self._enforce_memory_limit(content_metadata)
        binary_path = self._verified_binary_path(content_metadata)
        try:
            return binary_path.read_bytes()
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to read content {content_id}"
            ) from error

    async def read_text(self, content_id: str) -> str:
        """Read complete content using its declared encoding."""
        return await asyncio.to_thread(self._read_text_sync, content_id)

    def _read_text_sync(self, content_id: str) -> str:
        content_metadata = self._get_metadata_sync(content_id)
        if not content_metadata.encoding:
            raise StorageValidationError(
                f"Content {content_id} has no declared text encoding"
            )
        self._enforce_memory_limit(content_metadata)
        binary_path = self._verified_binary_path(content_metadata)
        try:
            return binary_path.read_bytes().decode(content_metadata.encoding)
        except (LookupError, UnicodeDecodeError) as error:
            raise StorageValidationError(
                f"Failed to decode content {content_id}"
            ) from error
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to read content {content_id}"
            ) from error

    async def read_range(
        self, content_id: str, *, offset: int, length: int
    ) -> ContentRange:
        """Read a byte range without loading the complete object."""
        return await asyncio.to_thread(
            self._read_range_sync,
            content_id,
            offset,
            length,
        )

    def _read_range_sync(self, content_id: str, offset: int, length: int) -> ContentRange:
        if offset < 0:
            raise StorageValidationError(
                f"Invalid offset for content {content_id}"
            )
        if length <= 0:
            raise StorageValidationError(
                f"Invalid length for content {content_id}"
            )
        content_metadata = self._get_metadata_sync(content_id)
        binary_path = self._verified_binary_path(content_metadata)
        try:
            with binary_path.open("rb") as binary_file:
                binary_file.seek(offset)
                data = binary_file.read(length)
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to read range from content {content_id}"
            ) from error
        return ContentRange(
            content_id=content_id,
            offset=offset,
            length=len(data),
            total_size_bytes=content_metadata.size_bytes,
            data=data,
            eof=offset + len(data) >= content_metadata.size_bytes,
        )

    async def search_text(
        self, content_id: str, *, query: str, limit: int = 10
    ) -> list[ContentMatch]:
        """Search decoded text sequentially without a persistent index."""
        return await asyncio.to_thread(
            self._search_text_sync,
            content_id,
            query,
            limit,
        )

    def _search_text_sync(
        self, content_id: str, query: str, limit: int
    ) -> list[ContentMatch]:
        if not query.strip():
            raise StorageValidationError(f"Empty query for content {content_id}")
        if limit <= 0:
            raise StorageValidationError(f"Invalid search limit for content {content_id}")

        content_metadata = self._get_metadata_sync(content_id)
        if not content_metadata.encoding:
            raise StorageValidationError(
                f"Content {content_id} has no declared text encoding"
            )
        binary_path = self._verified_binary_path(content_metadata)

        try:
            decoder = codecs.getincrementaldecoder(content_metadata.encoding)(errors="strict")
        except LookupError as error:
            raise StorageValidationError(
                f"Unknown encoding for content {content_id}"
            ) from error

        search_pattern = re.compile(re.escape(query), re.IGNORECASE)
        overlap_chars = max(len(query) + 2 * _EXCERPT_RADIUS_CHARS, 512)
        buffer = ""
        buffer_start = 0
        next_search_global = 0
        matches: list[ContentMatch] = []

        try:
            with binary_path.open("rb") as binary_file:
                while True:
                    block = binary_file.read(_READ_BLOCK_BYTES)
                    final = not block
                    buffer += decoder.decode(block, final=final)
                    safe_local_end = (
                        len(buffer)
                        if final
                        else max(0, len(buffer) - overlap_chars)
                    )
                    search_from = max(0, next_search_global - buffer_start)

                    while True:
                        match = search_pattern.search(buffer, search_from)
                        if match is None or (
                            not final and match.start() >= safe_local_end
                        ):
                            break
                        match_local = match.start()
                        match_start = buffer_start + match_local
                        match_end = buffer_start + match.end()
                        excerpt_start = max(0, match_local - _EXCERPT_RADIUS_CHARS)
                        excerpt_end = min(
                            len(buffer),
                            match.end() + _EXCERPT_RADIUS_CHARS,
                        )
                        matches.append(
                            ContentMatch(
                                content_id=content_id,
                                query=query,
                                char_start=match_start,
                                char_end=match_end,
                                excerpt=buffer[excerpt_start:excerpt_end],
                            )
                        )
                        if len(matches) >= limit:
                            return matches
                        next_search_global = match_start + 1
                        search_from = match_local + 1

                    if final:
                        break
                    if safe_local_end:
                        buffer = buffer[safe_local_end:]
                        buffer_start += safe_local_end
        except UnicodeDecodeError as error:
            raise StorageValidationError(
                f"Failed to decode content {content_id}"
            ) from error
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to search content {content_id}"
            ) from error
        return matches

    def _verified_binary_path(self, metadata: ContentMetadata) -> Path:
        object_dir = self._backend.require_object_dir(
            self._backend.contents_dir,
            "content",
            metadata.content_id,
        )
        return self._backend.verify_payload(
            object_dir=object_dir,
            object_type="content",
            object_id=metadata.content_id,
            binary_name="content.bin",
            expected_size=metadata.size_bytes,
            expected_hash=metadata.content_hash,
        )

    def _enforce_memory_limit(self, metadata: ContentMetadata) -> None:
        if metadata.size_bytes > self.config.max_in_memory_content_bytes:
            raise StorageContentTooLargeError(
                f"Content {metadata.content_id} exceeds the full-read memory limit; "
                "use read_range"
            )

    @staticmethod
    def _validate_content_id(content_id: str) -> None:
        if not isinstance(content_id, str) or not is_content_id(content_id):
            raise StorageValidationError(f"Invalid content ID {content_id!r}")

    @staticmethod
    def _public_ref(metadata: ContentMetadata) -> ContentRef:
        return ContentRef.model_validate(
            metadata.model_dump(
                exclude={"schema_version", "encoding", "cycle_id", "tool_call_id"}
            )
        )


class FileSystemArtifactStore(ArtifactStore):
    """Filesystem-backed storage for immutable artifact versions."""

    def __init__(self, backend: _AtomicFileBackend | StorageConfigType):
        self._backend = (
            backend if isinstance(backend, _AtomicFileBackend) else _AtomicFileBackend(backend)
        )
        self.config = self._backend.config
        self._metadata_locks: dict[str, asyncio.Lock] = {}
        self._metadata_locks_guard = threading.Lock()

    async def save_artifact(
        self,
        content: bytes,
        *,
        cycle_id: str,
        filename: str,
        mime_type: str | None = None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Persist an initial artifact version."""
        return await asyncio.to_thread(
            self._save_artifact_sync,
            content,
            cycle_id,
            filename,
            mime_type,
            source,
            metadata,
            1,
            None,
        )

    def _save_artifact_sync(
        self,
        content: bytes,
        cycle_id: str,
        filename: str,
        mime_type: str | None,
        source: str,
        metadata: dict[str, Any] | None,
        version: int,
        parent_artifact_id: str | None,
    ) -> ArtifactRef:
        artifact_id = new_artifact_id()
        if not isinstance(content, bytes):
            raise StorageValidationError(
                f"Artifact {artifact_id} content must be bytes"
            )
        try:
            detected_mime = mime_type or mimetypes.guess_type(filename)[0]
            artifact = ArtifactRef(
                artifact_id=artifact_id,
                cycle_id=cycle_id,
                filename=filename,
                mime_type=detected_mime or "application/octet-stream",
                size_bytes=len(content),
                content_hash=self._backend.hash_bytes(content),
                version=version,
                parent_artifact_id=parent_artifact_id,
                source=source,
                created_at=datetime.now(timezone.utc),
                metadata=dict(metadata or {}),
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise StorageValidationError(
                f"Invalid metadata for artifact {artifact_id}"
            ) from error
        metadata_bytes = serialize_model(
            artifact,
            object_type="artifact",
            object_id=artifact_id,
        )
        self._backend.write_new_object(
            parent=self._backend.artifacts_dir,
            object_type="artifact",
            object_id=artifact_id,
            binary_name="file.bin",
            content=content,
            metadata_bytes=metadata_bytes,
        )
        return artifact

    async def get_artifact(self, artifact_id: str) -> ArtifactRef:
        """Load an artifact reference."""
        return await asyncio.to_thread(self._get_artifact_sync, artifact_id)

    def _get_artifact_sync(self, artifact_id: str) -> ArtifactRef:
        self._validate_artifact_id(artifact_id)
        result = self._backend.load_metadata(
            parent=self._backend.artifacts_dir,
            object_type="artifact",
            object_id=artifact_id,
            model_type=ArtifactRef,
        )
        assert isinstance(result, ArtifactRef)
        return result

    async def open_artifact(self, artifact_id: str) -> bytes:
        """Read complete artifact bytes within the memory limit."""
        return await asyncio.to_thread(self._open_artifact_sync, artifact_id)

    def _open_artifact_sync(self, artifact_id: str) -> bytes:
        artifact = self._get_artifact_sync(artifact_id)
        if artifact.size_bytes > self.config.max_in_memory_content_bytes:
            raise StorageContentTooLargeError(
                f"Artifact {artifact_id} exceeds the full-read memory limit"
            )
        binary_path = self._verified_binary_path(artifact)
        try:
            return binary_path.read_bytes()
        except OSError as error:
            raise StorageIntegrityError(
                f"Failed to read artifact {artifact_id}"
            ) from error

    async def create_version(
        self,
        artifact_id: str,
        content: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: str = "agent_modified",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Create an immutable successor to an artifact."""
        return await asyncio.to_thread(
            self._create_version_sync,
            artifact_id,
            content,
            filename,
            mime_type,
            source,
            metadata,
        )

    def _create_version_sync(
        self,
        artifact_id: str,
        content: bytes,
        filename: str | None,
        mime_type: str | None,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> ArtifactRef:
        parent = self._get_artifact_sync(artifact_id)
        combined_metadata = dict(parent.metadata)
        combined_metadata.update(metadata or {})
        return self._save_artifact_sync(
            content,
            parent.cycle_id,
            filename if filename is not None else parent.filename,
            mime_type if mime_type is not None else parent.mime_type,
            source,
            combined_metadata,
            parent.version + 1,
            parent.artifact_id,
        )

    async def list_cycle_artifacts(self, cycle_id: str) -> list[ArtifactRef]:
        """List all artifact versions belonging to a cycle."""
        return await asyncio.to_thread(self._list_cycle_artifacts_sync, cycle_id)

    def _list_cycle_artifacts_sync(self, cycle_id: str) -> list[ArtifactRef]:
        if not isinstance(cycle_id, str) or not cycle_id.strip():
            raise StorageValidationError("Invalid cycle ID for artifact listing")
        artifacts: list[ArtifactRef] = []
        try:
            candidates = list(self._backend.artifacts_dir.iterdir())
        except OSError as error:
            raise StorageError(f"Failed to list artifacts for cycle {cycle_id}") from error
        for candidate in candidates:
            if not is_artifact_id(candidate.name):
                continue
            artifact = self._get_artifact_sync(candidate.name)
            if artifact.cycle_id == cycle_id:
                artifacts.append(artifact)
        artifacts.sort(key=lambda item: (item.created_at, item.version, item.artifact_id))
        return artifacts

    async def mark_for_delivery(
        self, artifact_id: str, *, client_type: str
    ) -> None:
        """Idempotently add a delivery target to artifact metadata."""
        self._validate_artifact_id(artifact_id)
        if not isinstance(client_type, str) or not client_type.strip():
            raise StorageValidationError(
                f"Invalid delivery target for artifact {artifact_id}"
            )
        lock = self._metadata_lock(artifact_id)
        async with lock:
            await asyncio.to_thread(
                self._mark_for_delivery_sync,
                artifact_id,
                client_type,
            )

    def _mark_for_delivery_sync(self, artifact_id: str, client_type: str) -> None:
        artifact = self._get_artifact_sync(artifact_id)
        if client_type in artifact.delivery_targets:
            return
        artifact.delivery_targets.append(client_type)
        metadata_bytes = serialize_model(
            artifact,
            object_type="artifact",
            object_id=artifact_id,
        )
        object_dir = self._backend.require_object_dir(
            self._backend.artifacts_dir,
            "artifact",
            artifact_id,
        )
        self._backend.update_metadata(
            object_dir=object_dir,
            object_type="artifact",
            object_id=artifact_id,
            metadata_bytes=metadata_bytes,
        )

    def _verified_binary_path(self, artifact: ArtifactRef) -> Path:
        object_dir = self._backend.require_object_dir(
            self._backend.artifacts_dir,
            "artifact",
            artifact.artifact_id,
        )
        return self._backend.verify_payload(
            object_dir=object_dir,
            object_type="artifact",
            object_id=artifact.artifact_id,
            binary_name="file.bin",
            expected_size=artifact.size_bytes,
            expected_hash=artifact.content_hash,
        )

    def _metadata_lock(self, artifact_id: str) -> asyncio.Lock:
        with self._metadata_locks_guard:
            lock = self._metadata_locks.get(artifact_id)
            if lock is None:
                lock = asyncio.Lock()
                self._metadata_locks[artifact_id] = lock
            return lock

    @staticmethod
    def _validate_artifact_id(artifact_id: str) -> None:
        if not isinstance(artifact_id, str) or not is_artifact_id(artifact_id):
            raise StorageValidationError(f"Invalid artifact ID {artifact_id!r}")
