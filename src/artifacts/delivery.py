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

    schema_version: Literal[2] = 2
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
    selection_index: int = Field(ge=0)
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
            artifact_lineage_id=self.artifact_lineage_id,
            selection_index=self.selection_index,
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

    async def select_many(
        self,
        records: list[ArtifactDeliveryRecord],
    ) -> list[ArtifactDeliveryRecord]:
        return await asyncio.to_thread(self._select_many_sync, records)

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

    async def transition_many(
        self,
        delivery_ids: list[str],
        *,
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        receipt_by_delivery_id: dict[str, dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> list[ArtifactDeliveryRecord]:
        """Atomically apply one group outcome while retaining part receipts."""
        return await asyncio.to_thread(
            self._transition_many_sync,
            delivery_ids,
            target,
            allowed_from,
            dict(receipt_by_delivery_id or {}),
            error,
        )

    async def cancel_many(
        self,
        delivery_ids: list[str],
    ) -> list[ArtifactDeliveryRecord]:
        return await asyncio.to_thread(
            self._cancel_many_sync,
            delivery_ids,
        )

    def _select_sync(self, record: ArtifactDeliveryRecord) -> ArtifactDeliveryRecord:
        return self._select_many_sync([record])[0]

    def _select_many_sync(
        self,
        records: list[ArtifactDeliveryRecord],
    ) -> list[ArtifactDeliveryRecord]:
        if not records:
            return []
        with self._lock:
            first = records[0]
            if any(
                item.session_id != first.session_id
                or item.cycle_id != first.cycle_id
                or item.client_type != first.client_type
                for item in records
            ):
                raise ArtifactDeliveryError(
                    "Batch delivery records must share one runtime authority"
                )
            lineage_targets: dict[str, str] = {}
            for item in records:
                existing_target = lineage_targets.get(
                    item.artifact_lineage_id
                )
                if (
                    existing_target is not None
                    and existing_target != item.artifact_id
                ):
                    raise ArtifactDeliveryError(
                        "A delivery batch cannot select multiple versions "
                        "of one lineage"
                    )
                lineage_targets[item.artifact_lineage_id] = item.artifact_id

            existing_records = self._list_cycle_sync(
                first.session_id,
                first.cycle_id,
                set(),
            )
            updates: dict[str, tuple[ArtifactDeliveryRecord, bool]] = {}
            selected_by_artifact: dict[str, ArtifactDeliveryRecord] = {}
            next_index = (
                max(
                    (
                        item.selection_index
                        for item in existing_records
                        if item.state != ArtifactDeliveryState.CANCELLED
                    ),
                    default=-1,
                )
                + 1
            )
            for item in records:
                existing_exact = next(
                    (
                        existing
                        for existing in existing_records
                        if existing.artifact_id == item.artifact_id
                        and existing.client_type == item.client_type
                        and existing.state
                        != ArtifactDeliveryState.CANCELLED
                    ),
                    None,
                )
                if existing_exact is not None:
                    if existing_exact.state == ArtifactDeliveryState.SELECTED:
                        selected_by_artifact[item.artifact_id] = existing_exact
                        continue
                    selected_by_artifact[item.artifact_id] = existing_exact
                    continue

                inherited_index: int | None = None
                for existing in existing_records:
                    if (
                        existing.artifact_lineage_id
                        == item.artifact_lineage_id
                        and existing.client_type == item.client_type
                        and existing.artifact_id != item.artifact_id
                        and existing.state
                        == ArtifactDeliveryState.SELECTED
                    ):
                        inherited_index = existing.selection_index
                        updates[existing.delivery_id] = (
                            existing.model_copy(update={
                                "state": ArtifactDeliveryState.CANCELLED,
                                "updated_at": utc_now(),
                            }),
                            True,
                        )
                selection_index = (
                    inherited_index
                    if inherited_index is not None
                    else next_index
                )
                if inherited_index is None:
                    next_index += 1
                ordered_item = item.model_copy(
                    update={"selection_index": selection_index}
                )
                updates[item.delivery_id] = (ordered_item, False)
                selected_by_artifact[item.artifact_id] = ordered_item

            self._commit_batch_sync(updates)
            return [
                selected_by_artifact[item.artifact_id] for item in records
            ]

    def _cancel_many_sync(
        self,
        delivery_ids: list[str],
    ) -> list[ArtifactDeliveryRecord]:
        if not delivery_ids:
            return []
        with self._lock:
            unique_ids = list(dict.fromkeys(delivery_ids))
            current_by_id = {
                delivery_id: self._load_sync(delivery_id)
                for delivery_id in unique_ids
            }
            cancellable = {
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
                ArtifactDeliveryState.UNKNOWN,
            }
            for current in current_by_id.values():
                if current.state not in cancellable:
                    raise ArtifactDeliveryError(
                        f"Cannot cancel delivery in state {current.state.value}"
                    )
            now = utc_now()
            updates = {
                delivery_id: (
                    current.model_copy(update={
                        "state": ArtifactDeliveryState.CANCELLED,
                        "updated_at": now,
                    }),
                    True,
                )
                for delivery_id, current in current_by_id.items()
            }
            self._commit_batch_sync(updates)
            cancelled = {
                delivery_id: record
                for delivery_id, (record, _) in updates.items()
            }
            return [cancelled[delivery_id] for delivery_id in delivery_ids]

    def _commit_batch_sync(
        self,
        updates: dict[str, tuple[ArtifactDeliveryRecord, bool]],
    ) -> None:
        if not updates:
            return
        backups: dict[Path, bytes | None] = {}
        for delivery_id in updates:
            path = self.root / f"{delivery_id}.json"
            try:
                backups[path] = (
                    path.read_bytes()
                    if path.exists() and not path.is_symlink()
                    else None
                )
            except OSError as error:
                raise ArtifactStorageError(
                    "Failed to prepare atomic delivery batch"
                ) from error
        try:
            for record, replace in updates.values():
                self._write_sync(record, replace=replace)
        except BaseException:
            self._restore_batch_sync(backups)
            raise

    @staticmethod
    def _restore_batch_sync(backups: dict[Path, bytes | None]) -> None:
        for path, payload in backups.items():
            try:
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(payload)
            except OSError as error:
                raise ArtifactStorageError(
                    "Failed to roll back delivery batch"
                ) from error

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

    def _transition_many_sync(
        self,
        delivery_ids: list[str],
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        receipt_by_delivery_id: dict[str, dict[str, Any]],
        error: str | None,
    ) -> list[ArtifactDeliveryRecord]:
        with self._lock:
            unique_ids = list(dict.fromkeys(delivery_ids))
            current = {
                delivery_id: self._load_sync(delivery_id)
                for delivery_id in unique_ids
            }
            for item in current.values():
                if item.state != target and item.state not in allowed_from:
                    raise ArtifactDeliveryError(
                        f"Cannot transition delivery group from "
                        f"{item.state.value} to {target.value}"
                    )
            updates: dict[str, tuple[ArtifactDeliveryRecord, bool]] = {}
            now = utc_now()
            for delivery_id, item in current.items():
                if item.state == target:
                    updates[delivery_id] = (item, True)
                    continue
                values: dict[str, Any] = {
                    "state": target,
                    "updated_at": now,
                    "last_error": error,
                    "receipt": {
                        **item.receipt,
                        **receipt_by_delivery_id.get(delivery_id, {}),
                    },
                }
                if target == ArtifactDeliveryState.DELIVERING:
                    values.update(
                        attempt_count=item.attempt_count + 1,
                        delivering_at=now,
                        failed_at=None,
                    )
                elif target == ArtifactDeliveryState.DELIVERED:
                    values.update(
                        delivered_at=now,
                        failed_at=None,
                        last_error=None,
                    )
                elif target in {
                    ArtifactDeliveryState.FAILED,
                    ArtifactDeliveryState.UNKNOWN,
                }:
                    values["failed_at"] = now
                updated = ArtifactDeliveryRecord.model_validate(
                    item.model_copy(update=values).model_dump(mode="python")
                )
                updates[delivery_id] = (updated, True)
            self._commit_batch_sync(updates)
            return [updates[delivery_id][0] for delivery_id in delivery_ids]

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
        result.sort(
            key=lambda item: (
                item.selection_index,
                item.created_at,
                item.delivery_id,
            )
        )
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
            if int(payload.get("schema_version", 1)) == 1:
                payload["schema_version"] = 2
                payload.setdefault("selection_index", 0)
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
        return (
            await self.select_many(
                artifact_ids=[artifact_id],
                access=access,
                client_type=client_type,
            )
        )[0]

    async def select_many(
        self,
        *,
        artifact_ids: list[str],
        access: ArtifactAccessContext,
        client_type: str,
    ) -> list[ArtifactDeliveryRef]:
        """Validate the whole batch before one store-level commit."""

        unique_ids = list(dict.fromkeys(artifact_ids))
        records: list[ArtifactDeliveryRecord] = []
        lineage_targets: dict[str, str] = {}
        now = utc_now()
        for artifact_id in unique_ids:
            artifact = await self.artifact_service.get_artifact(
                artifact_id,
                access=access,
            )
            if ArtifactCapability.DELIVER not in artifact.capabilities:
                raise ArtifactAccessError("Artifact format is not deliverable")
            existing_target = lineage_targets.get(
                artifact.artifact_lineage_id
            )
            if (
                existing_target is not None
                and existing_target != artifact.artifact_id
            ):
                raise ArtifactDeliveryError(
                    "A delivery batch cannot select multiple versions "
                    "of one lineage"
                )
            lineage_targets[artifact.artifact_lineage_id] = (
                artifact.artifact_id
            )
            version = await self.artifact_service.artifact_store.get_version(
                artifact_id
            )
            metadata = await self.content_store.get_metadata(
                version.content_id
            )
            if (
                metadata.size_bytes != artifact.size_bytes
                or metadata.content_hash != artifact.content_hash
            ):
                raise ArtifactIntegrityError(
                    "Artifact metadata and content metadata disagree "
                    "before delivery"
                )
            records.append(ArtifactDeliveryRecord(
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
                selection_index=len(records),
                state=ArtifactDeliveryState.SELECTED,
                created_at=now,
                updated_at=now,
            ))
        selected = await self.store.select_many(records)
        by_artifact_id = {
            item.artifact_id: item.public_ref() for item in selected
        }
        return [by_artifact_id[artifact_id] for artifact_id in artifact_ids]

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

    async def cancel_many_by_artifact_ids(
        self,
        *,
        artifact_ids: list[str],
        access: ArtifactAccessContext,
        client_type: str,
    ) -> list[ArtifactDeliveryRef]:
        """Atomically cancel all requested exact artifact selections."""

        unique_ids = list(dict.fromkeys(artifact_ids))
        for artifact_id in unique_ids:
            await self.artifact_service.get_artifact(
                artifact_id,
                access=access,
            )
        records = await self.store.list_cycle(
            session_id=access.session_id,
            cycle_id=access.cycle_id,
        )
        delivery_ids_by_artifact: dict[str, str] = {}
        cancellable = {
            ArtifactDeliveryState.SELECTED,
            ArtifactDeliveryState.FAILED,
            ArtifactDeliveryState.UNKNOWN,
        }
        for artifact_id in unique_ids:
            matches = [
                item
                for item in records
                if item.artifact_id == artifact_id
                and item.client_type == client_type
                and item.state != ArtifactDeliveryState.CANCELLED
            ]
            if not matches:
                raise ArtifactDeliveryNotFoundError(
                    "No delivery selection exists for this artifact"
                )
            latest = matches[-1]
            if latest.state not in cancellable:
                raise ArtifactDeliveryError(
                    f"Cannot cancel delivery in state {latest.state.value}"
                )
            delivery_ids_by_artifact[artifact_id] = latest.delivery_id
        cancelled = await self.store.cancel_many([
            delivery_ids_by_artifact[artifact_id]
            for artifact_id in unique_ids
        ])
        by_delivery_id = {
            item.delivery_id: item.public_ref() for item in cancelled
        }
        return [
            by_delivery_id[delivery_ids_by_artifact[artifact_id]]
            for artifact_id in artifact_ids
        ]

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
