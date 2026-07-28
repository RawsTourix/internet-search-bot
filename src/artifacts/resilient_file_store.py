"""Transient-safe filesystem publication for immutable artifact metadata."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from ..storage.file_backend import _fsync_directory
from .errors import ArtifactStorageError
from .file_store import FileSystemArtifactStore
from .models import new_artifact_id


logger = logging.getLogger("Artifacts.FileStore")


class ResilientFileSystemArtifactStore(FileSystemArtifactStore):
    """Retry only a transient atomic directory publish.

    On Windows, antivirus/indexing processes may briefly hold a newly written
    temporary metadata directory and make ``os.replace`` fail with WinError 5
    or 32. Retrying the whole ingress event would duplicate higher-level work,
    so this store retries only the final immutable directory publication.
    """

    _PUBLISH_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8)

    @staticmethod
    def _replace_directory(source: Path, target: Path) -> None:
        os.replace(source, target)

    @staticmethod
    def _is_retryable_publish_error(error: OSError, target: Path) -> bool:
        return isinstance(error, PermissionError) and not (
            target.exists() or target.is_symlink()
        )

    def _publish_directory(self, source: Path, target: Path) -> None:
        for attempt, delay in enumerate(
            (*self._PUBLISH_RETRY_DELAYS, None),
            start=1,
        ):
            try:
                self._replace_directory(source, target)
                return
            except OSError as error:
                if (
                    delay is None
                    or not self._is_retryable_publish_error(error, target)
                ):
                    raise
                logger.warning(
                    "artifact_metadata_publish_retry target=%s attempt=%s "
                    "delay_seconds=%s error_type=%s winerror=%s",
                    target.name,
                    attempt,
                    delay,
                    type(error).__name__,
                    getattr(error, "winerror", None),
                )
                time.sleep(delay)

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
            self._publish_directory(temporary_dir, final_dir)
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
