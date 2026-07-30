"""Filesystem persistence for explicit input-collection control records."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..artifacts.errors import ArtifactIntegrityError
from ..storage.config import StorageConfigType
from .collection_models import (
    InputCollectionRecord,
    InputCollectionState,
    InputDraftControlAction,
    InputDraftControlConflictError,
    InputDraftControlResult,
    InputDraftScope,
    new_input_collection_id,
)
from .models import ClientResponseRoute, utc_now
from .store import _AtomicJsonStore, _canonical_json, IngressNotFoundError


class FileSystemInputCollectionStore(_AtomicJsonStore):
    """Keep one durable active explicit collection per exact client scope."""

    def __init__(self, storage_config: StorageConfigType) -> None:
        configured = Path(storage_config.root_dir).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        super().__init__(
            configured.resolve(strict=False) / "input_collections",
            atomic_writes=storage_config.atomic_writes,
        )
        self.records_dir = self.root / "records"
        self.scope_index_dir = self.root / "scope_index"
        self.action_index_dir = self.root / "action_index"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.scope_index_dir.mkdir(parents=True, exist_ok=True)
        self.action_index_dir.mkdir(parents=True, exist_ok=True)

    async def create_or_get(
        self,
        scope: InputDraftScope,
        *,
        response_route: ClientResponseRoute,
        locale: str | None,
    ) -> tuple[InputCollectionRecord, bool]:
        return await asyncio.to_thread(
            self._create_or_get_sync,
            scope,
            response_route,
            locale,
        )

    async def get(self, collection_id: str) -> InputCollectionRecord:
        return await asyncio.to_thread(self._load_record_sync, collection_id)

    async def get_active(
        self,
        scope: InputDraftScope,
    ) -> InputCollectionRecord | None:
        return await asyncio.to_thread(self._get_active_sync, scope)

    async def bind(
        self,
        collection_id: str,
        input_batch_id: str,
    ) -> InputCollectionRecord:
        return await asyncio.to_thread(
            self._bind_sync,
            collection_id,
            input_batch_id,
        )

    async def mark_commit_requested(
        self,
        collection_id: str,
    ) -> InputCollectionRecord:
        return await asyncio.to_thread(
            self._mark_commit_requested_sync,
            collection_id,
        )

    async def mark_terminal(
        self,
        collection_id: str,
        *,
        state: InputCollectionState,
        failure_code: str | None = None,
    ) -> InputCollectionRecord:
        return await asyncio.to_thread(
            self._mark_terminal_sync,
            collection_id,
            state,
            failure_code,
        )

    async def load_action(
        self,
        *,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
    ) -> InputDraftControlResult | None:
        return await asyncio.to_thread(
            self._load_action_sync,
            scope,
            action,
            idempotency_key,
        )

    async def save_action(
        self,
        *,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
        result: InputDraftControlResult,
    ) -> InputDraftControlResult:
        return await asyncio.to_thread(
            self._save_action_sync,
            scope,
            action,
            idempotency_key,
            result,
        )

    def _create_or_get_sync(
        self,
        scope: InputDraftScope,
        response_route: ClientResponseRoute,
        locale: str | None,
    ) -> tuple[InputCollectionRecord, bool]:
        with self._lock:
            current = self._get_active_sync(scope)
            if current is not None:
                return current, True
            now = utc_now()
            record = InputCollectionRecord(
                collection_id=new_input_collection_id(),
                scope=scope,
                response_route=response_route,
                locale=locale,
                opened_at=now,
                updated_at=now,
            )
            self._write_record_sync(record)
            self._write_json(
                self._scope_index_path(scope),
                {
                    "schema_version": 1,
                    "scope": scope.model_dump(mode="json"),
                    "collection_id": record.collection_id,
                },
            )
            return record, False

    def _get_active_sync(
        self,
        scope: InputDraftScope,
    ) -> InputCollectionRecord | None:
        path = self._scope_index_path(scope)
        if not path.exists() and not path.is_symlink():
            return None
        index = self._read_json(path)
        indexed_scope = InputDraftScope.model_validate(index.get("scope"))
        if indexed_scope != scope:
            raise ArtifactIntegrityError("Input collection scope index mismatch")
        collection_id = str(index.get("collection_id") or "")
        try:
            record = self._load_record_sync(collection_id)
        except IngressNotFoundError:
            path.unlink(missing_ok=True)
            return None
        if not record.is_active:
            path.unlink(missing_ok=True)
            return None
        if record.scope != scope:
            raise ArtifactIntegrityError("Input collection authority mismatch")
        return record

    def _bind_sync(
        self,
        collection_id: str,
        input_batch_id: str,
    ) -> InputCollectionRecord:
        with self._lock:
            current = self._load_record_sync(collection_id)
            if not current.is_active:
                raise InputDraftControlConflictError(
                    "Terminal input collection cannot bind an input batch"
                )
            if current.bound_input_batch_id is not None:
                if current.bound_input_batch_id == input_batch_id:
                    return current
                raise InputDraftControlConflictError(
                    "Input collection is already bound to another batch"
                )
            updated = current.model_copy(
                update={
                    "bound_input_batch_id": input_batch_id,
                    "updated_at": utc_now(),
                }
            )
            updated = InputCollectionRecord.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_record_sync(updated)
            return updated

    def _mark_commit_requested_sync(
        self,
        collection_id: str,
    ) -> InputCollectionRecord:
        with self._lock:
            current = self._load_record_sync(collection_id)
            if current.state == InputCollectionState.COMMIT_REQUESTED:
                return current
            if current.state != InputCollectionState.COLLECTING:
                raise InputDraftControlConflictError(
                    "Only a collecting input collection can request commit"
                )
            now = utc_now()
            updated = current.model_copy(
                update={
                    "state": InputCollectionState.COMMIT_REQUESTED,
                    "commit_requested_at": now,
                    "updated_at": now,
                }
            )
            updated = InputCollectionRecord.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_record_sync(updated)
            return updated

    def _mark_terminal_sync(
        self,
        collection_id: str,
        state: InputCollectionState,
        failure_code: str | None,
    ) -> InputCollectionRecord:
        if state not in {
            InputCollectionState.COMMITTED,
            InputCollectionState.CANCELLED,
            InputCollectionState.ABANDONED,
            InputCollectionState.FAILED,
        }:
            raise ValueError("Unsupported terminal input collection state")
        with self._lock:
            current = self._load_record_sync(collection_id)
            if not current.is_active:
                if current.state == state:
                    return current
                raise InputDraftControlConflictError(
                    "Input collection is already terminal"
                )
            now = utc_now()
            updated = current.model_copy(
                update={
                    "state": state,
                    "updated_at": now,
                    "terminal_at": now,
                    "failure_code": failure_code,
                }
            )
            updated = InputCollectionRecord.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_record_sync(updated)
            self._release_scope_index_sync(updated)
            return updated

    def _load_action_sync(
        self,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
    ) -> InputDraftControlResult | None:
        normalized = idempotency_key.strip()
        if not normalized:
            raise ValueError("control idempotency key must not be empty")
        path = self._action_index_path(normalized)
        if not path.exists() and not path.is_symlink():
            return None
        payload = self._read_json(path)
        fingerprint = self._action_fingerprint(scope, action)
        if payload.get("fingerprint") != fingerprint:
            raise InputDraftControlConflictError(
                "Control idempotency key was reused with another action or scope"
            )
        try:
            return InputDraftControlResult.model_validate(payload.get("result"))
        except (ValidationError, ValueError) as error:
            raise ArtifactIntegrityError(
                "Invalid persisted input draft control result"
            ) from error

    def _save_action_sync(
        self,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
        result: InputDraftControlResult,
    ) -> InputDraftControlResult:
        normalized = idempotency_key.strip()
        if not normalized:
            raise ValueError("control idempotency key must not be empty")
        with self._lock:
            existing = self._load_action_sync(scope, action, normalized)
            if existing is not None:
                return existing.model_copy(update={"duplicate": True})
            self._write_json(
                self._action_index_path(normalized),
                {
                    "schema_version": 1,
                    "idempotency_key": normalized,
                    "fingerprint": self._action_fingerprint(scope, action),
                    "result": result.model_dump(mode="json"),
                },
            )
            return result

    def _load_record_sync(self, collection_id: str) -> InputCollectionRecord:
        try:
            return InputCollectionRecord.model_validate(
                self._read_json(self.records_dir / f"{collection_id}.json")
            )
        except (ValidationError, ValueError) as error:
            raise ArtifactIntegrityError(
                "Invalid input collection record"
            ) from error

    def _write_record_sync(self, record: InputCollectionRecord) -> None:
        self._write_json(
            self.records_dir / f"{record.collection_id}.json",
            record.model_dump(mode="json"),
        )

    def _release_scope_index_sync(self, record: InputCollectionRecord) -> None:
        path = self._scope_index_path(record.scope)
        if not path.exists() and not path.is_symlink():
            return
        index = self._read_json(path)
        if index.get("collection_id") == record.collection_id:
            path.unlink(missing_ok=True)

    def _scope_index_path(self, scope: InputDraftScope) -> Path:
        digest = hashlib.sha256(
            _canonical_json(scope.canonical_payload())
        ).hexdigest()
        return self.scope_index_dir / f"{digest}.json"

    def _action_index_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self.action_index_dir / f"{digest}.json"

    @staticmethod
    def _action_fingerprint(
        scope: InputDraftScope,
        action: InputDraftControlAction,
    ) -> str:
        payload: dict[str, Any] = {
            "scope": scope.canonical_payload(),
            "action": action.value,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
