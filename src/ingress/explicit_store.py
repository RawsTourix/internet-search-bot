"""Filesystem stores for explicit user-controlled input collection."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..artifacts.errors import ArtifactIntegrityError
from ..storage.config import StorageConfigType
from .collection_models import InputCollectionRecord, InputDraftScope
from .collection_store import FileSystemInputCollectionStore
from .explicit_policy import (
    EXPLICIT_COLLECTION_COMMIT_REASON,
    EXPLICIT_COLLECTION_GROUPING_MODE,
    is_explicit_collection_draft,
)
from .grouping import _OPEN_STATES
from .models import InputBatchDraft, InputBatchDraftState, utc_now
from .resilient_store import ResilientFileSystemCoordinatedInputBatchStore
from .store import IngressConflictError


class FileSystemExplicitInputCollectionStore(FileSystemInputCollectionStore):
    """Add bounded recovery scans for durable active collection records."""

    async def list_active(self) -> list[InputCollectionRecord]:
        return await asyncio.to_thread(self._list_active_sync)

    def _list_active_sync(self) -> list[InputCollectionRecord]:
        with self._lock:
            active: list[InputCollectionRecord] = []
            by_scope: dict[str, list[InputCollectionRecord]] = defaultdict(list)
            for path in self.records_dir.glob("icol_*.json"):
                collection_id = path.stem
                record = self._load_record_sync(collection_id)
                if not record.is_active:
                    continue
                active.append(record)
                by_scope[str(self._scope_index_path(record.scope))].append(record)

            for records in by_scope.values():
                if len(records) > 1:
                    raise ArtifactIntegrityError(
                        "Multiple active input collections share one exact scope"
                    )
                record = records[0]
                index_path = self._scope_index_path(record.scope)
                if not index_path.exists() and not index_path.is_symlink():
                    self._write_json(
                        index_path,
                        {
                            "schema_version": 1,
                            "scope": record.scope.model_dump(mode="json"),
                            "collection_id": record.collection_id,
                        },
                    )
                    continue
                indexed = self._read_json(index_path)
                if indexed.get("collection_id") != record.collection_id:
                    raise ArtifactIntegrityError(
                        "Input collection scope index points to another active record"
                    )

            active.sort(key=lambda item: (item.opened_at, item.collection_id))
            return active


class ExplicitCollectionInputBatchStore(
    ResilientFileSystemCoordinatedInputBatchStore
):
    """Persist explicit drafts without transport quiet/deadline semantics."""

    def __init__(
        self,
        storage_config: StorageConfigType,
        ingress_config,
        *,
        collection_store: FileSystemExplicitInputCollectionStore,
    ) -> None:
        super().__init__(storage_config, ingress_config)
        self.collection_store = collection_store

    async def promote_to_explicit_collection(
        self,
        input_batch_id: str,
        *,
        collection_id: str,
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._promote_to_explicit_collection_sync,
            input_batch_id,
            collection_id,
        )

    async def find_explicit_draft(
        self,
        *,
        session_id: str,
        collection_id: str,
    ) -> InputBatchDraft | None:
        drafts = await self.list_open_drafts(session_id=session_id)
        matches = [
            draft
            for draft in drafts
            if is_explicit_collection_draft(draft)
            and draft.grouping_key == collection_id
        ]
        if len(matches) > 1:
            raise ArtifactIntegrityError(
                "Multiple open drafts are bound to one explicit collection"
            )
        return matches[0] if matches else None

    async def commit_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ):
        draft = await self.get_draft(input_batch_id)
        if (
            is_explicit_collection_draft(draft)
            and reason != EXPLICIT_COLLECTION_COMMIT_REASON
        ):
            raise IngressConflictError(
                "Explicit input collection requires an explicit commit action"
            )
        return await super().commit_batch(
            input_batch_id,
            session_id=session_id,
            reason=reason,
        )

    async def abandon_open_drafts(
        self,
        *,
        session_id: str | None = None,
        code: str = "process_restart",
    ) -> list[InputBatchDraft]:
        active_collections = await self.collection_store.list_active()
        active_ids = {
            item.collection_id
            for item in active_collections
            if session_id is None or item.scope.session_id == session_id
        }
        return await asyncio.to_thread(
            self._abandon_unowned_open_drafts_sync,
            session_id,
            code,
            active_ids,
        )

    def _promote_to_explicit_collection_sync(
        self,
        input_batch_id: str,
        collection_id: str,
    ) -> InputBatchDraft:
        normalized_collection_id = collection_id.strip()
        if not normalized_collection_id:
            raise ValueError("collection_id must not be empty")

        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state not in _OPEN_STATES:
                raise IngressConflictError(
                    "Only an open input draft can enter explicit collection mode"
                )
            if is_explicit_collection_draft(current):
                if current.grouping_key != normalized_collection_id:
                    raise IngressConflictError(
                        "Input draft belongs to another explicit collection"
                    )
                if any(
                    value is not None
                    for value in (
                        current.quiet_deadline,
                        current.sealing_deadline,
                        current.maximum_deadline,
                    )
                ):
                    current = self._write_explicit_draft_sync(
                        current,
                        normalized_collection_id,
                    )
                return current

            target_path = self._group_index_path(
                session_id=current.session_id,
                grouping_mode=EXPLICIT_COLLECTION_GROUPING_MODE,
                grouping_key=normalized_collection_id,
            )
            if target_path.exists() or target_path.is_symlink():
                index = self._read_json(target_path)
                if index.get("input_batch_id") != current.input_batch_id:
                    raise IngressConflictError(
                        "Explicit collection is already bound to another draft"
                    )

            self._release_group_index_sync(current)
            return self._write_explicit_draft_sync(
                current,
                normalized_collection_id,
            )

    def _write_explicit_draft_sync(
        self,
        current: InputBatchDraft,
        collection_id: str,
    ) -> InputBatchDraft:
        updated = current.model_copy(
            update={
                "grouping_mode": EXPLICIT_COLLECTION_GROUPING_MODE,
                "grouping_key": collection_id,
                "quiet_deadline": None,
                "sealing_deadline": None,
                "maximum_deadline": None,
                "updated_at": utc_now(),
            }
        )
        updated = InputBatchDraft.model_validate(
            updated.model_dump(mode="python")
        )
        self._write_json(
            self.root / updated.input_batch_id / "draft.json",
            updated.model_dump(mode="json"),
        )
        self._write_json(
            self._group_index_path(
                session_id=updated.session_id,
                grouping_mode=updated.grouping_mode,
                grouping_key=updated.grouping_key,
            ),
            {
                "schema_version": 1,
                "session_id": updated.session_id,
                "grouping_mode": updated.grouping_mode.value,
                "grouping_key": updated.grouping_key,
                "input_batch_id": updated.input_batch_id,
            },
        )
        return updated

    def _abandon_unowned_open_drafts_sync(
        self,
        session_id: str | None,
        code: str,
        active_collection_ids: set[str],
    ) -> list[InputBatchDraft]:
        with self._lock:
            transitioned: list[InputBatchDraft] = []
            for draft in self._list_open_drafts_sync(session_id):
                current = self._load_draft_sync(draft.input_batch_id)
                if current.state not in _OPEN_STATES:
                    continue
                if (
                    is_explicit_collection_draft(current)
                    and current.grouping_key in active_collection_ids
                ):
                    continue
                transitioned.append(
                    self._transition_draft_sync(
                        current.input_batch_id,
                        InputBatchDraftState.ABANDONED,
                        code,
                    )
                )
            return transitioned
