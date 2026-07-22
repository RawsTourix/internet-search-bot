"""Durable grouping for Telegram media groups and standalone attachments."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

from ..artifacts.errors import ArtifactIntegrityError
from ..storage.config import StorageConfigType
from .config import IngressConfigType
from .models import (
    ClientIngressEvent,
    CommittedInputBatch,
    InputAttachmentPart,
    InputAttachmentState,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
    utc_now,
)
from .store import (
    FileSystemInputBatchStore,
    IngressConflictError,
    IngressNotFoundError,
)


_OPEN_STATES = {
    InputBatchDraftState.COLLECTING,
    InputBatchDraftState.SEALING,
    InputBatchDraftState.INGESTING,
    InputBatchDraftState.READY_TO_COMMIT,
}
_TERMINAL_UNCOMMITTED_STATES = {
    InputBatchDraftState.FAILED,
    InputBatchDraftState.CANCELLED,
    InputBatchDraftState.ABANDONED,
}


class FileSystemGroupedInputBatchStore(FileSystemInputBatchStore):
    """Extend the base batch store with one durable open-draft index."""

    def __init__(
        self,
        storage_config: StorageConfigType,
        ingress_config: IngressConfigType,
    ) -> None:
        super().__init__(storage_config)
        self.ingress_config = ingress_config
        self.group_index_dir = self.root / "group_index"
        self.group_index_dir.mkdir(parents=True, exist_ok=True)

    async def create_for_event(
        self,
        event: ClientIngressEvent,
        *,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> tuple[InputBatchDraft, bool]:
        return await asyncio.to_thread(
            self._get_or_append_sync,
            event,
            session_id,
            grouping_mode,
            grouping_key,
        )

    async def mark_collecting(self, input_batch_id: str) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._set_state_sync,
            input_batch_id,
            InputBatchDraftState.COLLECTING,
            None,
        )

    async def commit_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> tuple[CommittedInputBatch, bool]:
        """Commit and release the group index under one re-entrant store lock."""
        return await asyncio.to_thread(
            self._commit_grouped_sync,
            input_batch_id,
            session_id,
            reason,
        )

    async def list_open_drafts(
        self,
        *,
        session_id: str | None = None,
    ) -> list[InputBatchDraft]:
        return await asyncio.to_thread(self._list_open_drafts_sync, session_id)

    async def list_ready_drafts(self) -> list[InputBatchDraft]:
        """Return drafts whose sealing grace or hard deadline has elapsed."""
        now = utc_now()
        drafts = await self.list_open_drafts()
        ready = []
        for draft in drafts:
            if any(
                item.state != InputAttachmentState.STORED
                for item in draft.attachment_parts
            ):
                continue
            if draft.grouping_mode == InputGroupingMode.MEDIA_GROUP:
                if (
                    draft.sealing_deadline is not None
                    and now >= draft.sealing_deadline
                ) or (
                    draft.maximum_deadline is not None
                    and now >= draft.maximum_deadline
                ):
                    ready.append(draft)
            elif draft.grouping_mode == InputGroupingMode.STANDALONE_ATTACHMENT:
                if (
                    draft.maximum_deadline is not None
                    and now >= draft.maximum_deadline
                ):
                    ready.append(draft)
        return ready

    def _commit_grouped_sync(
        self,
        input_batch_id: str,
        session_id: str,
        reason: str,
    ) -> tuple[CommittedInputBatch, bool]:
        with self._lock:
            draft = self._load_draft_sync(input_batch_id)
            if draft.session_id != session_id:
                raise IngressConflictError(
                    "Input batch belongs to another session"
                )
            committed_path = self.root / input_batch_id / "committed.json"
            duplicate = committed_path.exists() or committed_path.is_symlink()
            if not duplicate and draft.state in _TERMINAL_UNCOMMITTED_STATES:
                raise IngressConflictError(
                    f"Input batch in {draft.state.value!r} state cannot be committed"
                )
            if not duplicate and draft.state == InputBatchDraftState.COMMITTED:
                raise ArtifactIntegrityError(
                    "Committed input batch draft is missing its immutable manifest"
                )
            committed = self._commit_sync(input_batch_id, reason)
            self._release_group_index_sync(draft)
            return committed, duplicate

    def _get_or_append_sync(
        self,
        event: ClientIngressEvent,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> tuple[InputBatchDraft, bool]:
        with self._lock:
            existing_draft, existing_committed = self._find_by_event_sync(
                event.event_id
            )
            if existing_committed is not None:
                return self._load_draft_sync(existing_committed.input_batch_id), True
            if existing_draft is not None:
                return existing_draft, True

            if grouping_mode == InputGroupingMode.ATOMIC:
                return super()._create_for_event_sync(
                    event,
                    session_id,
                    grouping_mode,
                    grouping_key,
                )

            group_path = self._group_index_path(
                session_id=session_id,
                grouping_mode=grouping_mode,
                grouping_key=grouping_key,
            )
            if group_path.exists() or group_path.is_symlink():
                index = self._read_json(group_path)
                batch_id = str(index.get("input_batch_id") or "")
                try:
                    draft = self._load_draft_sync(batch_id)
                except IngressNotFoundError:
                    group_path.unlink(missing_ok=True)
                else:
                    if draft.state in _OPEN_STATES:
                        return self._append_event_sync(draft, event), False
                    group_path.unlink(missing_ok=True)

            draft, duplicate = super()._create_for_event_sync(
                event,
                session_id,
                grouping_mode,
                grouping_key,
            )
            draft = self._apply_deadlines_sync(draft, reset_quiet=True)
            self._write_json(
                self.root / draft.input_batch_id / "draft.json",
                draft.model_dump(mode="json"),
            )
            self._write_json(group_path, {
                "schema_version": 1,
                "session_id": session_id,
                "grouping_mode": grouping_mode.value,
                "grouping_key": grouping_key,
                "input_batch_id": draft.input_batch_id,
            })
            return draft, duplicate

    def _append_event_sync(
        self,
        draft: InputBatchDraft,
        event: ClientIngressEvent,
    ) -> InputBatchDraft:
        if event.event_id in draft.source_event_ids:
            return draft
        if len(draft.source_event_ids) >= self.ingress_config.max_events_per_batch:
            raise IngressConflictError("Input batch event limit exceeded")
        if draft.client_type != event.client_type:
            raise IngressConflictError("Grouped input client type mismatch")
        if draft.conversation != event.conversation or draft.sender != event.sender:
            raise IngressConflictError("Grouped input authority mismatch")

        existing_part_ids = {item.part_id for item in draft.text_parts}
        existing_slot_ids = {item.slot_id for item in draft.attachment_parts}
        new_part_ids = {item.part_id for item in event.text_parts}
        new_slot_ids = {item.slot_id for item in event.attachment_slots}
        if existing_part_ids & new_part_ids:
            raise IngressConflictError("Grouped input text part ID collision")
        if existing_slot_ids & new_slot_ids:
            raise IngressConflictError("Grouped input attachment slot collision")
        if (
            len(draft.text_parts) + len(event.text_parts)
            > self.ingress_config.max_text_parts_per_batch
        ):
            raise IngressConflictError("Input batch text part limit exceeded")
        if (
            len(draft.attachment_parts) + len(event.attachment_slots)
            > self.ingress_config.max_attachments_per_batch
        ):
            raise IngressConflictError("Input batch attachment limit exceeded")

        now = utc_now()
        updated = draft.model_copy(update={
            "source_event_ids": [*draft.source_event_ids, event.event_id],
            "text_parts": [*draft.text_parts, *event.text_parts],
            "attachment_parts": [
                *draft.attachment_parts,
                *[
                    InputAttachmentPart(
                        slot_id=slot.slot_id,
                        original_filename=slot.original_filename,
                        declared_mime_type=slot.declared_mime_type,
                        declared_size_bytes=slot.declared_size_bytes,
                    )
                    for slot in event.attachment_slots
                ],
            ],
            "last_event_at": event.occurred_at,
            "updated_at": now,
            "state": InputBatchDraftState.COLLECTING,
        })
        updated = InputBatchDraft.model_validate(
            updated.model_dump(mode="python")
        )
        updated = self._apply_deadlines_sync(updated, reset_quiet=True)
        self._write_json(
            self.root / updated.input_batch_id / "draft.json",
            updated.model_dump(mode="json"),
        )
        self._write_json(
            self.event_index_dir / f"{event.event_id}.json",
            {
                "schema_version": 1,
                "event_id": event.event_id,
                "input_batch_id": updated.input_batch_id,
            },
        )
        return updated

    def _apply_deadlines_sync(
        self,
        draft: InputBatchDraft,
        *,
        reset_quiet: bool,
    ) -> InputBatchDraft:
        now = utc_now()
        updates = {"updated_at": now}
        if draft.grouping_mode == InputGroupingMode.MEDIA_GROUP:
            if reset_quiet:
                quiet = now + timedelta(
                    seconds=self.ingress_config.media_group_quiet_timeout_seconds
                )
                updates["quiet_deadline"] = quiet
                updates["sealing_deadline"] = quiet + timedelta(
                    seconds=self.ingress_config.media_group_sealing_grace_seconds
                )
            if draft.maximum_deadline is None:
                updates["maximum_deadline"] = draft.opened_at + timedelta(
                    seconds=self.ingress_config.media_group_maximum_wait_seconds
                )
        elif draft.grouping_mode == InputGroupingMode.STANDALONE_ATTACHMENT:
            if draft.maximum_deadline is None:
                updates["maximum_deadline"] = draft.opened_at + timedelta(
                    seconds=(
                        self.ingress_config
                        .standalone_attachment_maximum_wait_seconds
                    )
                )
        result = draft.model_copy(update=updates)
        return InputBatchDraft.model_validate(result.model_dump(mode="python"))

    def _list_open_drafts_sync(
        self,
        session_id: str | None,
    ) -> list[InputBatchDraft]:
        result: list[InputBatchDraft] = []
        for draft_path in self.root.glob("ibat_*/draft.json"):
            try:
                draft = InputBatchDraft.model_validate(
                    self._read_json(draft_path)
                )
            except Exception as error:
                raise ArtifactIntegrityError(
                    "Invalid input batch draft during recovery scan"
                ) from error
            if draft.state not in _OPEN_STATES:
                continue
            if session_id is not None and draft.session_id != session_id:
                continue
            result.append(draft)
        result.sort(key=lambda item: (item.opened_at, item.input_batch_id))
        return result

    def _release_group_index_sync(self, draft: InputBatchDraft) -> None:
        if draft.grouping_mode == InputGroupingMode.ATOMIC:
            return
        path = self._group_index_path(
            session_id=draft.session_id,
            grouping_mode=draft.grouping_mode,
            grouping_key=draft.grouping_key,
        )
        if not path.exists() and not path.is_symlink():
            return
        index = self._read_json(path)
        if index.get("input_batch_id") == draft.input_batch_id:
            path.unlink(missing_ok=True)

    def _group_index_path(
        self,
        *,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> Path:
        digest = hashlib.sha256(
            (
                session_id
                + "\x00"
                + grouping_mode.value
                + "\x00"
                + grouping_key
            ).encode("utf-8")
        ).hexdigest()
        return self.group_index_dir / f"{digest}.json"
