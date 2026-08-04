"""Transport-neutral orchestration for explicit input collection."""

from __future__ import annotations

import logging

from .collection_models import (
    InputCollectionState,
    InputDraftControlAction,
    InputDraftControlResult,
    InputDraftControlStatus,
    InputDraftScope,
)
from .collection_store import FileSystemInputCollectionStore
from .grouping import _OPEN_STATES
from .models import (
    ClientResponseRoute,
    InputAttachmentState,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
)
from .store import FileSystemIngressEventStore


logger = logging.getLogger("API.Ingress.DraftControl")


class InputDraftControlService:
    """Coordinate user control without exposing filesystem mutation to clients."""

    def __init__(
        self,
        *,
        event_store: FileSystemIngressEventStore,
        batch_store,
        collection_store: FileSystemInputCollectionStore,
        presentation_coordinator=None,
    ) -> None:
        self.event_store = event_store
        self.batch_store = batch_store
        self.collection_store = collection_store
        self.presentation_coordinator = presentation_coordinator

    async def _finalize_presentation(
        self,
        draft: InputBatchDraft | None,
        *,
        state: str,
    ) -> None:
        if draft is None or self.presentation_coordinator is None:
            return
        try:
            await self.presentation_coordinator.finalize_batch(
                input_batch_id=draft.input_batch_id,
                state=state,
                file_count=len(draft.attachment_parts),
                text_part_count=len(draft.text_parts),
                response_anchor=draft.response_anchor,
            )
        except Exception:
            # Batch/collection terminality is the primary durable mutation.
            # Startup reconciliation can safely retry this secondary lifecycle
            # update, so a presentation-store outage must not turn a successful
            # /send or /cancel into an ambiguous client failure.
            logger.exception(
                "input_collection_presentation_finalize_deferred "
                "input_batch_id=%s state=%s",
                draft.input_batch_id,
                state,
            )

    async def start_collection(
        self,
        scope: InputDraftScope,
        *,
        response_route: ClientResponseRoute,
        locale: str | None,
        idempotency_key: str,
    ) -> InputDraftControlResult:
        cached = await self._cached(
            scope,
            InputDraftControlAction.START,
            idempotency_key,
        )
        if cached is not None:
            return cached

        active = await self.collection_store.get_active(scope)
        if active is not None:
            result = await self._result(
                action=InputDraftControlAction.START,
                status=InputDraftControlStatus.ALREADY_ACTIVE,
                collection=active,
            )
            return await self._save(
                scope,
                InputDraftControlAction.START,
                idempotency_key,
                result,
            )

        compatible = await self._compatible_open_drafts(scope)
        if len(compatible) > 1:
            result = InputDraftControlResult(
                action=InputDraftControlAction.START,
                status=InputDraftControlStatus.CONFLICT,
                error_code="multiple_compatible_open_drafts",
            )
            return await self._save(
                scope,
                InputDraftControlAction.START,
                idempotency_key,
                result,
            )

        collection, _ = await self.collection_store.create_or_get(
            scope,
            response_route=response_route,
            locale=locale,
        )
        status = InputDraftControlStatus.STARTED
        if compatible:
            collection = await self.collection_store.bind(
                collection.collection_id,
                compatible[0].input_batch_id,
            )
            status = InputDraftControlStatus.PROMOTED_AUTO_DRAFT

        result = await self._result(
            action=InputDraftControlAction.START,
            status=status,
            collection=collection,
        )
        return await self._save(
            scope,
            InputDraftControlAction.START,
            idempotency_key,
            result,
        )

    async def inspect(
        self,
        scope: InputDraftScope,
    ) -> InputDraftControlResult:
        collection = await self.collection_store.get_active(scope)
        if collection is None:
            return InputDraftControlResult(
                action=InputDraftControlAction.INSPECT,
                status=InputDraftControlStatus.NOT_FOUND,
            )
        return await self._result(
            action=InputDraftControlAction.INSPECT,
            status=InputDraftControlStatus.INSPECTED,
            collection=collection,
        )

    async def bind_batch(
        self,
        scope: InputDraftScope,
        *,
        input_batch_id: str,
    ) -> InputDraftControlResult:
        collection = await self.collection_store.get_active(scope)
        if collection is None:
            return InputDraftControlResult(
                action=InputDraftControlAction.BIND,
                status=InputDraftControlStatus.NOT_FOUND,
                input_batch_id=input_batch_id,
            )
        draft = await self.batch_store.get_draft(input_batch_id)
        if not await self._draft_matches_scope(draft, scope):
            return InputDraftControlResult(
                action=InputDraftControlAction.BIND,
                status=InputDraftControlStatus.CONFLICT,
                input_batch_id=input_batch_id,
                error_code="input_batch_scope_mismatch",
            )
        collection = await self.collection_store.bind(
            collection.collection_id,
            input_batch_id,
        )
        return await self._result(
            action=InputDraftControlAction.BIND,
            status=InputDraftControlStatus.INSPECTED,
            collection=collection,
        )

    async def commit(
        self,
        scope: InputDraftScope,
        *,
        idempotency_key: str,
    ) -> InputDraftControlResult:
        cached = await self._cached(
            scope,
            InputDraftControlAction.COMMIT,
            idempotency_key,
        )
        if cached is not None:
            return cached

        collection = await self.collection_store.get_active(scope)
        if collection is None:
            result = InputDraftControlResult(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.NOT_FOUND,
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if collection.bound_input_batch_id is None:
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.EMPTY,
                collection=collection,
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )

        draft = await self.batch_store.get_draft(
            collection.bound_input_batch_id
        )
        if not await self._draft_matches_scope(draft, scope):
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.CONFLICT,
                collection=collection,
                draft=draft,
                error_code="input_batch_scope_mismatch",
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if not draft.text_parts and not draft.attachment_parts and not draft.semantic_parts:
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.EMPTY,
                collection=collection,
                draft=draft,
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if draft.state == InputBatchDraftState.COMMITTED:
            committed = await self.batch_store.get_committed(
                draft.input_batch_id
            )
            await self._finalize_presentation(
                draft,
                state="committed",
            )
            collection = await self.collection_store.mark_terminal(
                collection.collection_id,
                state=InputCollectionState.COMMITTED,
            )
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.COMMITTED,
                collection=collection,
                draft=draft,
                committed_batch=committed,
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if draft.state not in _OPEN_STATES:
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.CONFLICT,
                collection=collection,
                draft=draft,
                error_code=f"input_batch_{draft.state.value}",
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if any(
            item.state == InputAttachmentState.FAILED
            for item in draft.attachment_parts
        ):
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.FAILED,
                collection=collection,
                draft=draft,
                error_code="attachment_ingestion_failed",
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )
        if any(
            item.state != InputAttachmentState.STORED
            for item in draft.attachment_parts
        ):
            collection = await self.collection_store.mark_commit_requested(
                collection.collection_id
            )
            result = await self._result(
                action=InputDraftControlAction.COMMIT,
                status=InputDraftControlStatus.COMMIT_REQUESTED,
                collection=collection,
                draft=draft,
            )
            return await self._save(
                scope,
                InputDraftControlAction.COMMIT,
                idempotency_key,
                result,
            )

        if draft.grouping_mode == InputGroupingMode.ATOMIC:
            committed = await self.batch_store.commit(
                draft.input_batch_id,
                reason="explicit_collection_commit",
            )
        else:
            committed, _ = await self.batch_store.commit_batch(
                draft.input_batch_id,
                session_id=scope.session_id,
                reason="explicit_collection_commit",
            )
        collection = await self.collection_store.mark_terminal(
            collection.collection_id,
            state=InputCollectionState.COMMITTED,
        )
        terminal_draft = await self.batch_store.get_draft(
            draft.input_batch_id
        )
        await self._finalize_presentation(
            terminal_draft,
            state="committed",
        )
        result = await self._result(
            action=InputDraftControlAction.COMMIT,
            status=InputDraftControlStatus.COMMITTED,
            collection=collection,
            draft=terminal_draft,
            committed_batch=committed,
        )
        return await self._save(
            scope,
            InputDraftControlAction.COMMIT,
            idempotency_key,
            result,
        )

    async def cancel(
        self,
        scope: InputDraftScope,
        *,
        idempotency_key: str,
    ) -> InputDraftControlResult:
        cached = await self._cached(
            scope,
            InputDraftControlAction.CANCEL,
            idempotency_key,
        )
        if cached is not None:
            return cached

        collection = await self.collection_store.get_active(scope)
        if collection is None:
            result = InputDraftControlResult(
                action=InputDraftControlAction.CANCEL,
                status=InputDraftControlStatus.NOT_FOUND,
            )
            return await self._save(
                scope,
                InputDraftControlAction.CANCEL,
                idempotency_key,
                result,
            )

        draft = None
        if collection.bound_input_batch_id is not None:
            draft = await self.batch_store.cancel_draft(
                collection.bound_input_batch_id,
                code="explicit_collection_cancelled",
            )
            await self._finalize_presentation(
                draft,
                state="cancelled",
            )
        collection = await self.collection_store.mark_terminal(
            collection.collection_id,
            state=InputCollectionState.CANCELLED,
        )
        result = await self._result(
            action=InputDraftControlAction.CANCEL,
            status=InputDraftControlStatus.CANCELLED,
            collection=collection,
            draft=draft,
        )
        return await self._save(
            scope,
            InputDraftControlAction.CANCEL,
            idempotency_key,
            result,
        )

    async def _compatible_open_drafts(
        self,
        scope: InputDraftScope,
    ) -> list[InputBatchDraft]:
        candidates = await self.batch_store.list_open_drafts(
            session_id=scope.session_id
        )
        result: list[InputBatchDraft] = []
        for draft in candidates:
            if await self._draft_matches_scope(draft, scope):
                result.append(draft)
        return result

    async def _draft_matches_scope(
        self,
        draft: InputBatchDraft,
        scope: InputDraftScope,
    ) -> bool:
        if (
            draft.session_id != scope.session_id
            or draft.client_type != scope.client_type
            or draft.conversation != scope.conversation
            or draft.sender.principal_id != scope.principal_id
        ):
            return False
        if not draft.source_event_ids:
            return False
        first_event = await self.event_store.get(draft.source_event_ids[0])
        return first_event.client_instance_id == scope.client_instance_id

    async def _result(
        self,
        *,
        action: InputDraftControlAction,
        status: InputDraftControlStatus,
        collection,
        draft: InputBatchDraft | None = None,
        committed_batch=None,
        error_code: str | None = None,
    ) -> InputDraftControlResult:
        if draft is None and collection.bound_input_batch_id is not None:
            draft = await self.batch_store.get_draft(
                collection.bound_input_batch_id
            )
        return InputDraftControlResult(
            action=action,
            status=status,
            collection=collection,
            input_batch_id=(
                draft.input_batch_id
                if draft is not None
                else collection.bound_input_batch_id
            ),
            draft_state=draft.state if draft is not None else None,
            file_count=len(draft.attachment_parts) if draft is not None else 0,
            text_part_count=len(draft.text_parts) if draft is not None else 0,
            semantic_part_count=len(draft.semantic_parts) if draft is not None else 0,
            committed_batch=committed_batch,
            error_code=error_code,
        )

    async def _cached(
        self,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
    ) -> InputDraftControlResult | None:
        cached = await self.collection_store.load_action(
            scope=scope,
            action=action,
            idempotency_key=idempotency_key,
        )
        return cached.model_copy(update={"duplicate": True}) if cached else None

    async def _save(
        self,
        scope: InputDraftScope,
        action: InputDraftControlAction,
        idempotency_key: str,
        result: InputDraftControlResult,
    ) -> InputDraftControlResult:
        return await self.collection_store.save_action(
            scope=scope,
            action=action,
            idempotency_key=idempotency_key,
            result=result,
        )
