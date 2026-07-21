"""Streaming extension for the filesystem content store."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, TypeVar

from pydantic import ValidationError

from .errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageValidationError,
)
from .file_backend import (
    FileSystemContentStore,
    _READ_BLOCK_BYTES,
    _fsync_directory,
)
from .models import ContentMetadata, ContentRef, new_content_id
from .serializers import serialize_model


_T = TypeVar("_T")


async def _await_blocking(callable_: Callable[..., _T], *args) -> _T:
    """Finish one bounded thread operation before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(callable_, *args))
    try:
        return await task
    except asyncio.CancelledError:
        # Closing or deleting a file while a worker thread still uses it is
        # racy. Finish only this bounded operation and preserve cancellation.
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise


class StreamingFileSystemContentStore(FileSystemContentStore):
    """Filesystem content store with bounded asynchronous stream I/O."""

    async def save_stream(
        self,
        chunks: AsyncIterator[bytes],
        *,
        source_type: str,
        source_name: str | None = None,
        mime_type: str | None = None,
        encoding: str | None = None,
        cycle_id: str | None = None,
        tool_call_id: str | None = None,
        size_tokens_estimate: int | None = None,
        metadata: dict | None = None,
        max_size_bytes: int,
    ) -> ContentRef:
        """Persist an async stream while hashing and enforcing a hard limit."""
        if (
            isinstance(max_size_bytes, bool)
            or not isinstance(max_size_bytes, int)
            or max_size_bytes <= 0
        ):
            raise StorageValidationError(
                "max_size_bytes must be a positive integer"
            )
        if not isinstance(source_type, str) or not source_type.strip():
            raise StorageValidationError("source_type must not be empty")
        if not hasattr(chunks, "__aiter__"):
            raise StorageValidationError(
                "chunks must be an async byte iterator"
            )

        decoder = None
        size_chars: int | None = None
        if encoding is not None:
            try:
                decoder = codecs.getincrementaldecoder(encoding)(
                    errors="strict"
                )
            except LookupError as error:
                raise StorageValidationError(
                    "Unknown streaming content encoding"
                ) from error
            size_chars = 0

        content_id = new_content_id()
        final_dir = self._backend.contents_dir / content_id
        if final_dir.exists() or final_dir.is_symlink():
            raise StorageValidationError(
                f"Cannot overwrite content {content_id}"
            )

        temporary_dir: Path | None = None
        output = None
        committed = False
        try:
            if self.config.atomic_writes:
                temporary_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".tmp-{content_id}-",
                        dir=self._backend.contents_dir,
                    )
                )
                target_dir = temporary_dir
            else:
                final_dir.mkdir(parents=False, exist_ok=False)
                target_dir = final_dir

            binary_path = target_dir / "content.bin"
            output = await _await_blocking(binary_path.open, "wb")
            digest = hashlib.sha256()
            size_bytes = 0

            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise StorageValidationError(
                        f"Streaming content {content_id} yielded "
                        "a non-bytes chunk"
                    )

                next_size = size_bytes + len(chunk)
                if next_size > max_size_bytes:
                    raise StorageContentTooLargeError(
                        f"Streaming content {content_id} exceeds "
                        "max_size_bytes"
                    )

                if chunk:
                    await _await_blocking(output.write, chunk)
                    digest.update(chunk)
                size_bytes = next_size

                if decoder is not None:
                    try:
                        decoded = decoder.decode(chunk, final=False)
                        size_chars = (size_chars or 0) + len(decoded)
                    except UnicodeDecodeError:
                        # Preserve exact bytes, matching save_content(bytes,
                        # encoding=...) semantics.
                        decoder = None
                        size_chars = None

            if decoder is not None:
                try:
                    decoded = decoder.decode(b"", final=True)
                    size_chars = (size_chars or 0) + len(decoded)
                except UnicodeDecodeError:
                    size_chars = None

            await _await_blocking(output.flush)
            try:
                file_descriptor = output.fileno()
                await _await_blocking(os.fsync, file_descriptor)
            except OSError:
                pass
            await _await_blocking(output.close)
            output = None

            actual_mime = mime_type or (
                "text/plain"
                if encoding is not None
                else "application/octet-stream"
            )
            try:
                content_metadata = ContentMetadata(
                    content_id=content_id,
                    source_type=source_type,
                    source_name=source_name,
                    mime_type=actual_mime,
                    size_bytes=size_bytes,
                    size_chars=size_chars,
                    size_tokens_estimate=size_tokens_estimate,
                    content_hash=f"sha256:{digest.hexdigest()}",
                    created_at=datetime.now(timezone.utc),
                    metadata=dict(metadata or {}),
                    encoding=encoding,
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
            await _await_blocking(
                self._backend._write_file,
                target_dir / "metadata.json",
                metadata_bytes,
            )

            if self.config.atomic_writes:
                # Atomic rename is the publication commit point. Keep it
                # synchronous so cancellation cannot make publication
                # ambiguous to the caller.
                os.replace(target_dir, final_dir)
                temporary_dir = None
                _fsync_directory(self._backend.contents_dir)

            committed = True
            return self._public_ref(content_metadata)
        except asyncio.CancelledError:
            raise
        except StorageError:
            raise
        except Exception as error:
            raise StorageError(
                f"Failed to save streaming content {content_id}"
            ) from error
        finally:
            if output is not None:
                try:
                    output.close()
                except OSError:
                    pass
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
            if (
                not self.config.atomic_writes
                and not committed
                and final_dir.exists()
            ):
                shutil.rmtree(final_dir, ignore_errors=True)

    async def iter_content(
        self,
        content_id: str,
        *,
        chunk_size: int = _READ_BLOCK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Yield verified bytes without applying the full-read memory limit."""
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
        ):
            raise StorageValidationError(
                "chunk_size must be a positive integer"
            )

        content_metadata = await self.get_metadata(content_id)
        binary_path = await _await_blocking(
            self._verified_binary_path,
            content_metadata,
        )
        binary_file = await _await_blocking(binary_path.open, "rb")
        try:
            while True:
                block = await _await_blocking(
                    binary_file.read,
                    chunk_size,
                )
                if not block:
                    break
                yield block
        finally:
            await _await_blocking(binary_file.close)
