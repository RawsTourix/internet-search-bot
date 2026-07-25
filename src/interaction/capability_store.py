"""Atomic filesystem persistence for immutable capability snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from ..storage.config import StorageConfigType
from .capabilities import (
    ClientCapabilityDeclaration,
    ClientCapabilityRegistry,
    ClientCapabilitySnapshot,
)
from .config import ClientCapabilitiesConfig
from .errors import (
    CapabilityValidationError,
    CapabilityNotFoundError,
    InteractionIntegrityError,
    InteractionStorageError,
)
from .ids import is_interaction_id


logger = logging.getLogger("Interaction.Capabilities")


class FileSystemCapabilitySnapshotStore:
    def __init__(
        self,
        storage_config: StorageConfigType,
        registry: ClientCapabilityRegistry,
        config: ClientCapabilitiesConfig,
    ) -> None:
        configured = Path(storage_config.root_dir).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        root = configured.resolve(strict=False) / "client_bindings"
        self.snapshots_dir = root / "capability_snapshots"
        self.fingerprints_dir = root / "capability_fingerprints"
        self.atomic_writes = storage_config.atomic_writes
        self.registry = registry
        self.config = config
        self._lock = threading.RLock()
        try:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            self.fingerprints_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InteractionStorageError(
                "Failed to initialize capability snapshot store"
            ) from error

    async def resolve(
        self,
        declaration: ClientCapabilityDeclaration,
        *,
        client_type: str,
        client_instance_id: str,
    ) -> tuple[ClientCapabilitySnapshot, bool]:
        try:
            candidate = self.registry.resolve(
                declaration,
                client_type=client_type,
                client_instance_id=client_instance_id,
                reject_unknown=self.config.reject_unknown_features,
                max_feature_count=self.config.max_feature_count,
                max_limit_count=self.config.max_limit_count,
            )
        except CapabilityValidationError:
            logger.warning(
                "client_capabilities_rejected client_type=%s client_instance_id=%s",
                client_type,
                client_instance_id,
            )
            raise
        snapshot, duplicate = await asyncio.to_thread(
            self._resolve_sync,
            candidate,
        )
        logger.info(
            "client_capabilities_resolved snapshot_id=%s client_type=%s "
            "feature_count=%s limit_count=%s duplicate=%s",
            snapshot.capability_snapshot_id,
            snapshot.client_type,
            len(snapshot.features),
            len(snapshot.limits),
            duplicate,
        )
        return snapshot, duplicate

    async def get(self, snapshot_id: str) -> ClientCapabilitySnapshot:
        return await asyncio.to_thread(self._load_sync, snapshot_id)

    def _resolve_sync(
        self, candidate: ClientCapabilitySnapshot
    ) -> tuple[ClientCapabilitySnapshot, bool]:
        digest = candidate.fingerprint.removeprefix("sha256:")
        index_path = self.fingerprints_dir / f"{digest}.json"
        with self._lock:
            if index_path.exists() or index_path.is_symlink():
                index = self._read_json(index_path)
                snapshot = self._load_sync(str(index.get("capability_snapshot_id", "")))
                if snapshot.fingerprint != candidate.fingerprint:
                    raise InteractionIntegrityError(
                        "Capability fingerprint index conflicts with snapshot"
                    )
                return snapshot, True
            self._write_json(
                self.snapshots_dir / f"{candidate.capability_snapshot_id}.json",
                candidate.model_dump(mode="json"),
                replace=False,
            )
            self._write_json(
                index_path,
                {
                    "schema_version": 1,
                    "fingerprint": candidate.fingerprint,
                    "capability_snapshot_id": candidate.capability_snapshot_id,
                },
                replace=False,
            )
            return candidate, False

    def _load_sync(self, snapshot_id: str) -> ClientCapabilitySnapshot:
        if not is_interaction_id(snapshot_id, prefix="cbs"):
            raise CapabilityNotFoundError("Invalid capability snapshot ID")
        path = self.snapshots_dir / f"{snapshot_id}.json"
        try:
            return ClientCapabilitySnapshot.model_validate(self._read_json(path))
        except CapabilityNotFoundError:
            raise
        except (ValidationError, ValueError, TypeError) as error:
            raise InteractionIntegrityError(
                "Invalid capability snapshot metadata"
            ) from error

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        if not path.exists() and not path.is_symlink():
            raise CapabilityNotFoundError(f"Unknown capability snapshot {path.stem}")
        try:
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise InteractionIntegrityError("Unsafe capability metadata path")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (CapabilityNotFoundError, InteractionIntegrityError):
            raise
        except Exception as error:
            raise InteractionStorageError(
                "Failed to read capability metadata"
            ) from error
        if not isinstance(value, dict):
            raise InteractionIntegrityError("Capability metadata must be an object")
        return value

    def _write_json(
        self,
        path: Path,
        payload: dict[str, object],
        *,
        replace: bool,
    ) -> None:
        if path.parent not in {self.snapshots_dir, self.fingerprints_dir}:
            raise InteractionStorageError("Capability metadata path escaped store")
        if not replace and (path.exists() or path.is_symlink()):
            raise InteractionStorageError("Capability metadata already exists")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            os.close(descriptor)
            temporary = Path(name)
            with temporary.open("wb") as output:
                output.write(encoded)
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise InteractionStorageError(
                "Failed to persist capability metadata"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
