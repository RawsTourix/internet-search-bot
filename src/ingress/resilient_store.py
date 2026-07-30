"""Failure-safe grouped input batch persistence for filesystem v0.4."""

from __future__ import annotations

import asyncio

from .coordinated_store import FileSystemCoordinatedInputBatchStore
from .grouping import _OPEN_STATES, _TERMINAL_UNCOMMITTED_STATES
from .models import (
    ClientIngressEvent,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
    utc_now,
)
from .store import IngressConflictError, IngressNotFoundError


class ResilientFileSystemCoordinatedInputBatchStore(
    FileSystemCoordinatedInputBatchStore
):
    """Keep terminal drafts immutable and isolate their grouping keys."""

    async def cancel_draft(
        self,
        input_batch_id: str,
        *,
        code: str = "explicit_collection_cancelled",
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._transition_draft_sync,
            input_batch_id,
            InputBatchDraftState.CANCELLED,
            code,
        )

    async def abandon_draft(
        self,
        input_batch_id: str,
        *,
        code: str = "explicit_collection_timeout",
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._transition_draft_sync,
            input_batch_id,
            InputBatchDraftState.ABANDONED,
            code,
        )

    async def cancel_open_drafts(
        self,
        *,
        session_id: str,
        code: str = "session_reset",
    ) -> list[InputBatchDraft]:
        return await asyncio.to_thread(
            self._transition_open_drafts_sync,
            session_id,
            InputBatchDraftState.CANCELLED,
            code,
        )

    async def abandon_open_drafts(
        self,
        *,
        session_id: str | None = None,
        code: str = "process_restart",
    ) -> list[InputBatchDraft]:
        """Abandon open drafts whose process-local owners no longer exist."""

        return await asyncio.to_thread(
            self._transition_open_drafts_sync,
            session_id,
            InputBatchDraftState.ABANDONED,
            code,
        )

    def _get_or_append_sync(
        self,
        event: ClientIngressEvent,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> tuple[InputBatchDraft, bool]:
        """Reject late members of an exact failed group without a new draft."""

        with self._lock:
            existing_draft, existing_committed = self._find_by_event_sync(
                event.event_id
            )
            if existing_committed is not None:
                return self._load_draft_sync(
                    existing_committed.input_batch_id
                ), True
            if existing_draft is not None:
                return existing_draft, True

            if grouping_mode != InputGroupingMode.ATOMIC:
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
                        if draft.state in _TERMINAL_UNCOMMITTED_STATES:
                            return draft, True

            return super()._get_or_append_sync(
                event,
                session_id,
                grouping_mode,
                grouping_key,
            )

    def _set_state_sync(
        self,
        input_batch_id: str,
        state: InputBatchDraftState,
        failure_code: str | None,
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state == InputBatchDraftState.COMMITTED:
                return current
            if current.state in _TERMINAL_UNCOMMITTED_STATES:
                if current.state == state:
                    return current
                raise IngressConflictError(
                    f"Input batch in {current.state.value!r} state is terminal"
                )
            return super()._set_state_sync(
                input_batch_id,
                state,
                failure_code,
            )

    def _update_attachment_sync(
        self,
        input_batch_id: str,
        slot_id: str,
        changes,
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state in _TERMINAL_UNCOMMITTED_STATES:
                raise IngressConflictError(
                    f"Input batch in {current.state.value!r} state is terminal"
                )
            return super()._update_attachment_sync(
                input_batch_id,
                slot_id,
                changes,
            )

    def _fail_sync(
        self,
        input_batch_id: str,
        code: str,
        slot_id: str | None,
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state == InputBatchDraftState.COMMITTED:
                return current
            if current.state in _TERMINAL_UNCOMMITTED_STATES:
                return current
            # Keep the exact failed group index as a terminal tombstone. The
            # draft is excluded from generic open-draft grouping, while a late
            # member of the same media_group_id receives the same failed batch.
            return super()._fail_sync(input_batch_id, code, slot_id)

    def _transition_draft_sync(
        self,
        input_batch_id: str,
        state: InputBatchDraftState,
        code: str,
    ) -> InputBatchDraft:
        if state not in {
            InputBatchDraftState.CANCELLED,
            InputBatchDraftState.ABANDONED,
        }:
            raise ValueError("Unsupported terminal transition for input draft")
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state == InputBatchDraftState.COMMITTED:
                return current
            if current.state in _TERMINAL_UNCOMMITTED_STATES:
                if current.state == state:
                    return current
                raise IngressConflictError(
                    f"Input batch in {current.state.value!r} state is terminal"
                )
            updated = current.model_copy(
                update={
                    "state": state,
                    "failure_code": code,
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
            self._release_group_index_sync(updated)
            return updated

    def _transition_open_drafts_sync(
        self,
        session_id: str | None,
        state: InputBatchDraftState,
        code: str,
    ) -> list[InputBatchDraft]:
        if state not in {
            InputBatchDraftState.CANCELLED,
            InputBatchDraftState.ABANDONED,
        }:
            raise ValueError("Unsupported terminal transition for open drafts")

        with self._lock:
            transitioned: list[InputBatchDraft] = []
            for draft in self._list_open_drafts_sync(session_id):
                current = self._load_draft_sync(draft.input_batch_id)
                if current.state not in _OPEN_STATES:
                    continue
                updated = self._transition_draft_sync(
                    current.input_batch_id,
                    state,
                    code,
                )
                transitioned.append(updated)
            return transitioned
