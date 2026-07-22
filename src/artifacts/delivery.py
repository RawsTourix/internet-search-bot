"""Durable artifact delivery selection and transport-facing state machine."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..storage.config import StorageConfigType
from ..storage.interfaces import ContentStore
from ..storage.models import is_content_id
from .config import ArtifactConfigType
from .errors import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactStorageError,
)
from .models import (
    ArtifactAccessContext,
    ArtifactCapability,
    ArtifactDeliveryRef,
    ArtifactDeliveryState,
    is_artifact_delivery_id,
    is_artifact_id,
    is_artifact_lineage_id,
    new_artifact_delivery_id,
    utc_now,
)
from .service import ArtifactService


class ArtifactDeliveryRecord(BaseModel):
    """Persistent transport-independent delivery outbox item."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    delivery_id: str
    session_id: str
    cycle_id: str

    artifact_id: str
    artifact_lineage_id: str
    content_id: str

    filename: str
    format_id: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    content_hash: str

    client_type: str
    state: ArtifactDeliveryState = ArtifactDeliveryState.SELECTED
    attempt_count: int = Field(default=0, ge=0)

    created_at: datetime
    updated_at: datetime
    delivering_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None

    last_error: str | None = None
    receipt: dict[str, Any] = Field(default_factory=dict)

    @field_validator("delivery_id")
    @classmethod
    def validate_delivery_id(cls, value: str) -> str:
        if not is_artifact_delivery_id(value):
            raise ValueError("invalid delivery_id")
        return value

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not is_artifact_id(value):
            raise ValueError("invalid artifact_id")
        return value

    @field_validator("artifact_lineage_id")
    @classmethod
    def validate_lineage_id(cls, value: str) -> str:
        if not is_artifact_lineage_id(value):
            raise ValueError("invalid artifact_lineage_id")
        return value

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, value: str) -> str:
        if not is_content_id(value):
            raise ValueError("invalid content_id")
        return value

    @field_validator(
        "session_id",
        "cycle_id",
        "filename",
        "format_id",
        "mime_type",
        "content_hash",
        "client_type",
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator(
        "created_at",
        "updated_at",
        "delivering_at",
        "delivered_at",
        "failed_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delivery timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("last_error")
    @classmethod
    def normalize_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized[:2_000] or None

    @model_validator(mode="after")
    def validate_state_timestamps(self) -> "ArtifactDeliveryRecord":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.state == ArtifactDeliveryState.DELIVERING and self.delivering_at is None:
            raise ValueError("delivering state requires delivering_at")
        if self.state == ArtifactDeliveryState.DELIVERED and self.delivered_at is None:
            raise ValueError("delivered state requires delivered_at")
        if self.state in {ArtifactDeliveryState.FAILED, ArtifactDeliveryState.UNKNOWN}:
            if self.failed_at is None:
                raise ValueError("failed/unknown state requires failed_at")
        return self

    def public_ref(self) -> ArtifactDeliveryRef:
        return ArtifactDeliveryRef(
            delivery_id=self.delivery_id,
            artifact_id=self.artifact_id,
            filename=self.filename,
            format_id=self.format_id,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            content_hash=self.content_hash,
            client_type=self.client_type,
            state=self.state,
        )


class FileSystemArtifactDeliveryStore:
    """Atomic filesystem store for artifact delivery records."""

    def __init__(self, storage_config: StorageConfigType) -> None:
        configured_root = Path(storage_config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = configured_root.resolve(strict=False) / "deliveries" / "records"
        self.atomic_writes = storage_config.atomic_writes
        self._lock = threading.RLock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError("Failed to initialize delivery store") from error

    async def select(self, record: ArtifactDeliveryRecord) -> ArtifactDeliveryRecord:
        return await asyncio.to_thread(self._select_sync, record)

    async def get(self, delivery_id: str) -> ArtifactDeliveryRecord:
        return await asyncio.to_thread(self._load_sync, delivery_id)

    async def list_cycle(
        self,
        *,
        session_id: str,
        cycle_id: str,
        states: Iterable[ArtifactDeliveryState] | None = None,
    ) -> list[ArtifactDeliveryRecord]:
        return await asyncio.to_thread(
            self._list_cycle_sync,
            session_id,
            cycle_id,
            set(states or []),
        )

    async def transition(
        self,
        delivery_id: str,
        *,
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        error: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> ArtifactDeliveryRecord:
        return await asyncio.to_thread(
            self._transition_sync,
            delivery_id,
            target,
            allowed_from,
            error,
            dict(receipt or {}),
        )

    def _select_sync(self, record: ArtifactDeliveryRecord) -> ArtifactDeliveryRecord:
        with self._lock:
            records = self._list_cycle_sync(record.session_id, record.cycle_id, set())
            for existing in records:
                if (
                    existing.artifact_id == record.artifact_id
                    and existing.client_type == record.client_type
                    and existing.state != ArtifactDeliveryState.CANCELLED
                ):
                    return existing

            for existing in records:
                if (
                    existing.artifact_lineage_id == record.artifact_lineage_id
                    and existing.client_type == record.client_type
                    and existing.artifact_id != record.artifact_id
                    and existing.state == ArtifactDeliveryState.SELECTED
                ):
                    cancelled = existing.model_copy(update={
                        "state": ArtifactDeliveryState.CANCELLED,
                        "updated_at": utc_now(),
                    })
                    self._write_sync(cancelled, replace=True)

            self._write_sync(record, replace=False)
            return record

    def _transition_sync(
        self,
        delivery_id: str,
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        error: str | None,
        receipt: dict[str, Any],
    ) -> ArtifactDeliveryRecord:
        with self._lock:
            current = self._load_sync(delivery_id)
            if current.state == target:
                return current
            if current.state not in allowed_from:
                raise ArtifactDeliveryError(
                    f"Cannot transition delivery {delivery_id} from "
                    f"{current.state.value} to {target.value}"
                )

            now = utc_now()
            updates: dict[str, Any] = {
                "state": target,
                "updated_at": now,
                "last_error": error,
            }
            if receipt:
                updates["receipt"] = {**current.receipt, **receipt}
            if target == ArtifactDeliveryState.DELIVERING:
                updates["attempt_count"] = current.attempt_count + 1
                updates["delivering_at"] = now
                updates["failed_at"] = None
            elif target == ArtifactDeliveryState.DELIVERED:
                updates["delivered_at"] = now
                updates["failed_at"] = None
                updates["last_error"] = None
            elif target in {
                ArtifactDeliveryState.FAILED,
                ArtifactDeliveryState.UNKNOWN,
            }:
                updates["failed_at"] = now
            elif target == ArtifactDeliveryState.SELECTED:
                updates["failed_at"] = None
                updates["last_error"] = None

            updated = current.model_copy(update=updates)
            updated = ArtifactDeliveryRecord.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_sync(updated, replace=True)
            return updated

    def _list_cycle_sync(
        self,
        session_id: str,
        cycle_id: str,
        states: set[ArtifactDeliveryState],
    ) -> list[ArtifactDeliveryRecord]:
        result: list[ArtifactDeliveryRecord] = []
        try:
            paths = list(self.root.glob("dlv_*.json"))
        except OSError as error:
            raise ArtifactStorageError("Failed to list delivery records") from error
        for path in paths:
            record = self._load_path_sync(path)
            if record.session_id != session_id or record.cycle_id != cycle_id:
                continue
            if states and record.state not in states:
                continue
            result.append(record)
        result.sort(key=lambda item: (item.created_at, item.delivery_id))
        return result

    def _load_sync(self, delivery_id: str) -> ArtifactDeliveryRecord:
        if not is_artifact_delivery_id(delivery_id):
            raise ArtifactDeliveryNotFoundError("Invalid delivery ID")
        path = self.root / f"{delivery_id}.json"
        if not path.exists() and not path.is_symlink():
            raise ArtifactDeliveryNotFoundError(f"Unknown delivery {delivery_id}")
        return self._load_path_sync(path)

    def _load_path_sync(self, path: Path) -> ArtifactDeliveryRecord:
        try:
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise ArtifactIntegrityError("Invalid delivery metadata file")
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ArtifactDeliveryRecord.model_validate(payload)
        except (ArtifactIntegrityError, ArtifactDeliveryNotFoundError):
            raise
        except Exception as error:
            raise ArtifactStorageError("Failed to load delivery metadata") from error

    def _write_sync(
        self,
        record: ArtifactDeliveryRecord,
        *,
        replace: bool,
    ) -> None:
        path = self.root / f"{record.delivery_id}.json"
        if not replace and (path.exists() or path.is_symlink()):
            raise ArtifactDeliveryError(
                f"Delivery {record.delivery_id} already exists"
            )
        payload = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            if self.atomic_writes:
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{record.delivery_id}.",
                    suffix=".tmp",
                    dir=self.root,
                )
                os.close(descriptor)
                temporary_path = Path(name)
                with temporary_path.open("wb") as output:
                    output.write(payload)
                    output.flush()
                    try:
                        os.fsync(output.fileno())
                    except OSError:
                        pass
                os.replace(temporary_path, path)
                temporary_path = None
            else:
                path.write_bytes(payload)
        except OSError as error:
            raise ArtifactStorageError("Failed to persist delivery metadata") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


class ArtifactDeliveryService:
    """Select committed artifacts and expose exact bytes to transport adapters."""

    def __init__(
        self,
        *,
        config: ArtifactConfigType,
        artifact_service: ArtifactService,
        content_store: ContentStore,
        store: FileSystemArtifactDeliveryStore,
    ) -> None:
        self.config = config
        self.artifact_service = artifact_service
        self.content_store = content_store
        self.store = store

    async def select(
        self,
        *,
        artifact_id: str,
        access: ArtifactAccessContext,
        client_type: str,
    ) -> ArtifactDeliveryRef:
        artifact = await self.artifact_service.get_artifact(
            artifact_id,
            access=access,
        )
        if ArtifactCapability.DELIVER not in artifact.capabilities:
            raise ArtifactAccessError("Artifact format is not deliverable")
        version = await self.artifact_service.artifact_store.get_version(artifact_id)
        metadata = await self.content_store.get_metadata(version.content_id)
        if (
            metadata.size_bytes != artifact.size_bytes
            or metadata.content_hash != artifact.content_hash
        ):
            raise ArtifactIntegrityError(
                "Artifact metadata and content metadata disagree before delivery"
            )
        now = utc_now()
        record = ArtifactDeliveryRecord(
            delivery_id=new_artifact_delivery_id(),
            session_id=access.session_id,
            cycle_id=access.cycle_id,
            artifact_id=artifact.artifact_id,
            artifact_lineage_id=artifact.artifact_lineage_id,
            content_id=version.content_id,
            filename=artifact.filename,
            format_id=artifact.format_id,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            content_hash=artifact.content_hash,
            client_type=client_type,
            state=ArtifactDeliveryState.SELECTED,
            created_at=now,
            updated_at=now,
        )
        return (await self.store.select(record)).public_ref()

    async def list_cycle_refs(
        self,
        *,
        session_id: str,
        cycle_id: str,
        include_terminal: bool = True,
    ) -> list[ArtifactDeliveryRef]:
        states = None
        if not include_terminal:
            states = {
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.DELIVERING,
                ArtifactDeliveryState.UNKNOWN,
            }
        records = await self.store.list_cycle(
            session_id=session_id,
            cycle_id=cycle_id,
            states=states,
        )
        return [
            item.public_ref()
            for item in records
            if item.state != ArtifactDeliveryState.CANCELLED
        ]

    async def claim(self, delivery_id: str) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=ArtifactDeliveryState.DELIVERING,
            allowed_from={
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
                ArtifactDeliveryState.UNKNOWN,
            },
        )
        return record.public_ref()

    async def complete(
        self,
        delivery_id: str,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=ArtifactDeliveryState.DELIVERED,
            allowed_from={ArtifactDeliveryState.DELIVERING},
            receipt=receipt,
        )
        return record.public_ref()

    async def fail(
        self,
        delivery_id: str,
        *,
        error: str,
        ambiguous: bool = False,
        receipt: dict[str, Any] | None = None,
    ) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=(
                ArtifactDeliveryState.UNKNOWN
                if ambiguous
                else ArtifactDeliveryState.FAILED
            ),
            allowed_from={ArtifactDeliveryState.DELIVERING},
            error=error,
            receipt=receipt,
        )
        return record.public_ref()

    async def cancel(self, delivery_id: str) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=ArtifactDeliveryState.CANCELLED,
            allowed_from={
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
                ArtifactDeliveryState.UNKNOWN,
            },
        )
        return record.public_ref()

    async def iter_content(
        self,
        delivery_id: str,
        *,
        session_id: str,
        client_type: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        record = await self.store.get(delivery_id)
        if record.session_id != session_id or record.client_type != client_type:
            raise ArtifactAccessError("Delivery is outside the current client authority")
        metadata = await self.content_store.get_metadata(record.content_id)
        if (
            metadata.size_bytes != record.size_bytes
            or metadata.content_hash != record.content_hash
        ):
            raise ArtifactIntegrityError("Delivery content integrity mismatch")
        async for chunk in self.content_store.iter_content(
            record.content_id,
            chunk_size=chunk_size,
        ):
            yield chunk
