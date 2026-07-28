"""Failure-safe grouped input batch persistence for filesystem v0.4."""

from __future__ import annotations

import asyncio

from .coordinated_store import FileSystemCoordinatedInputBatchStore
from .grouping import _OPEN_STATES, _TERMINAL_UNCOMMITTED_STATES
from .models import InputBatchDraft, InputBatchDraftState, utc_now
from .store import IngressConflictError


class ResilientFileSystemCoordinatedInputBatchStore(
    FileSystemCoordinatedInputBatchStore
):
    """Keep failed/cancelled drafts terminal and release their group indexes."""

    async def cancel_open_drafts(
        self,
        *,
        session_id: str,
        code: str = "session_reset",
    ) -> list[InputBatchDraft]:
        return await asyncio.to_thread(
            self._cancel_open_drafts_sync,
            session_id,
            code,
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
                self._release_group_index_sync(current)
                return current
            updated = super()._fail_sync(input_batch_id, code, slot_id)
            self._release_group_index_sync(updated)
            return updated

    def _cancel_open_drafts_sync(
        self,
        session_id: str,
        code: str,
    ) -> list[InputBatchDraft]:
        with self._lock:
            cancelled: list[InputBatchDraft] = []
            for draft in self._list_open_drafts_sync(session_id):
                current = self._load_draft_sync(draft.input_batch_id)
                if current.state not in _OPEN_STATES:
                    continue
                updated = current.model_copy(
                    update={
                        "state": InputBatchDraftState.CANCELLED,
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
                cancelled.append(updated)
            return cancelled
