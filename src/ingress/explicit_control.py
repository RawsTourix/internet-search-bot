"""Crash-safe control orchestration for explicit input collections."""

from __future__ import annotations

import logging

from .collection_models import (
    InputCollectionRecord,
    InputCollectionState,
    InputDraftControlAction,
    InputDraftControlResult,
    InputDraftControlStatus,
    InputDraftScope,
)
from .draft_control import InputDraftControlService
from .explicit_policy import (
    is_explicit_collection_draft,
    is_legacy_explicit_collection_draft,
)
from .models import ClientResponseRoute, InputBatchDraftState, utc_now


logger = logging.getLogger("API.Ingress.ExplicitControl")

_OPEN_DRAFT_STATES = {
    InputBatchDraftState.COLLECTING,
    InputBatchDraftState.SEALING,
    InputBatchDraftState.INGESTING,
    InputBatchDraftState.READY_TO_COMMIT,
}
_TERMINAL_COLLECTION_BY_DRAFT = {
    InputBatchDraftState.COMMITTED: InputCollectionState.COMMITTED,
    InputBatchDraftState.CANCELLED: InputCollectionState.CANCELLED,
    InputBatchDraftState.ABANDONED: InputCollectionState.ABANDONED,
    InputBatchDraftState.FAILED: InputCollectionState.FAILED,
}


class ExplicitInputDraftControlService(InputDraftControlService):
    """Promote, expire and reconcile durable explicit input collections."""

    def __init__(
        self,
        *args,
        idle_timeout_seconds: float = 3600.0,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        if idle_timeout_seconds <= 0:
            raise ValueError("explicit collection idle timeout must be positive")
        self.idle_timeout_seconds = float(idle_timeout_seconds)

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
            active = await self.reconcile_collection(active)
            if active.is_active:
                active = await self.collection_store.touch(active.collection_id)
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
            try:
                draft = await self.batch_store.promote_to_explicit_collection(
                    compatible[0].input_batch_id,
                    collection_id=collection.collection_id,
                )
                collection = await self.collection_store.bind(
                    collection.collection_id,
                    draft.input_batch_id,
                )
            except Exception:
                await self.collection_store.mark_terminal(
                    collection.collection_id,
                    state=InputCollectionState.FAILED,
                    failure_code="explicit_collection_promotion_failed",
                )
                raise
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
        collection = await self.reconcile_collection(collection)
        if not collection.is_active:
            return InputDraftControlResult(
                action=InputDraftControlAction.INSPECT,
                status=InputDraftControlStatus.NOT_FOUND,
                collection=collection,
                input_batch_id=collection.bound_input_batch_id,
                error_code=collection.failure_code,
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
        collection = await self.reconcile_collection(collection)
        if not collection.is_active:
            return InputDraftControlResult(
                action=InputDraftControlAction.BIND,
                status=InputDraftControlStatus.NOT_FOUND,
                collection=collection,
                input_batch_id=input_batch_id,
                error_code=collection.failure_code,
            )
        if (
            collection.bound_input_batch_id is not None
            and collection.bound_input_batch_id != input_batch_id
        ):
            return InputDraftControlResult(
                action=InputDraftControlAction.BIND,
                status=InputDraftControlStatus.CONFLICT,
                collection=collection,
                input_batch_id=input_batch_id,
                error_code="collection_already_bound",
            )

        draft = await self.batch_store.get_draft(input_batch_id)
        if not await self._draft_matches_scope(draft, scope):
            return InputDraftControlResult(
                action=InputDraftControlAction.BIND,
                status=InputDraftControlStatus.CONFLICT,
                collection=collection,
                input_batch_id=input_batch_id,
                error_code="input_batch_scope_mismatch",
            )
        draft = await self.batch_store.promote_to_explicit_collection(
            input_batch_id,
            collection_id=collection.collection_id,
        )
        collection = await self.collection_store.bind(
            collection.collection_id,
            draft.input_batch_id,
        )
        return await self._result(
            action=InputDraftControlAction.BIND,
            status=InputDraftControlStatus.INSPECTED,
            collection=collection,
            draft=draft,
        )

    async def commit(
        self,
        scope: InputDraftScope,
        *,
        idempotency_key: str,
    ) -> InputDraftControlResult:
        collection = await self.collection_store.get_active(scope)
        if collection is not None:
            await self.reconcile_collection(collection)
        return await super().commit(scope, idempotency_key=idempotency_key)

    async def cancel(
        self,
        scope: InputDraftScope,
        *,
        idempotency_key: str,
    ) -> InputDraftControlResult:
        collection = await self.collection_store.get_active(scope)
        if collection is not None:
            await self.reconcile_collection(collection)
        return await super().cancel(scope, idempotency_key=idempotency_key)

    async def reconcile_active_collections(self) -> list[InputCollectionRecord]:
        result: list[InputCollectionRecord] = []
        for collection in await self.collection_store.list_active():
            result.append(await self.reconcile_collection(collection))
        return result

    async def reconcile_collection(
        self,
        collection: InputCollectionRecord,
    ) -> InputCollectionRecord:
        if not collection.is_active:
            return collection
        if self._is_idle_expired(collection):
            return await self._abandon_idle_collection(collection)

        if collection.bound_input_batch_id is not None:
            draft = await self.batch_store.get_draft(
                collection.bound_input_batch_id
            )
            terminal = _TERMINAL_COLLECTION_BY_DRAFT.get(draft.state)
            if terminal is not None:
                return await self.collection_store.mark_terminal(
                    collection.collection_id,
                    state=terminal,
                    failure_code=draft.failure_code,
                )
            if draft.state in _OPEN_DRAFT_STATES and (
                not is_explicit_collection_draft(draft)
                or is_legacy_explicit_collection_draft(draft)
            ):
                await self.batch_store.promote_to_explicit_collection(
                    draft.input_batch_id,
                    collection_id=collection.collection_id,
                )
            return collection

        draft = await self.batch_store.find_explicit_draft(
            session_id=collection.scope.session_id,
            collection_id=collection.collection_id,
        )
        if draft is None:
            return collection
        if not await self._draft_matches_scope(draft, collection.scope):
            await self.collection_store.mark_terminal(
                collection.collection_id,
                state=InputCollectionState.FAILED,
                failure_code="explicit_collection_scope_mismatch",
            )
            raise RuntimeError("Explicit collection draft authority mismatch")

        terminal = _TERMINAL_COLLECTION_BY_DRAFT.get(draft.state)
        if terminal is not None:
            return await self.collection_store.mark_terminal(
                collection.collection_id,
                state=terminal,
                failure_code=draft.failure_code,
            )
        if is_legacy_explicit_collection_draft(draft):
            draft = await self.batch_store.promote_to_explicit_collection(
                draft.input_batch_id,
                collection_id=collection.collection_id,
            )
        return await self.collection_store.bind(
            collection.collection_id,
            draft.input_batch_id,
        )

    def _is_idle_expired(self, collection: InputCollectionRecord) -> bool:
        idle_seconds = (utc_now() - collection.updated_at).total_seconds()
        return idle_seconds >= self.idle_timeout_seconds

    async def _abandon_idle_collection(
        self,
        collection: InputCollectionRecord,
    ) -> InputCollectionRecord:
        batch_id = collection.bound_input_batch_id
        if batch_id is not None:
            draft = await self.batch_store.get_draft(batch_id)
            terminal = _TERMINAL_COLLECTION_BY_DRAFT.get(draft.state)
            if terminal is not None:
                return await self.collection_store.mark_terminal(
                    collection.collection_id,
                    state=terminal,
                    failure_code=draft.failure_code,
                )
            if draft.state in _OPEN_DRAFT_STATES:
                await self.batch_store.abandon_draft(
                    batch_id,
                    code="explicit_collection_idle_timeout",
                )

        abandoned = await self.collection_store.mark_terminal(
            collection.collection_id,
            state=InputCollectionState.ABANDONED,
            failure_code="explicit_collection_idle_timeout",
        )
        logger.warning(
            "ingress_explicit_collection_abandoned_idle collection_id=%s "
            "session_id=%s input_batch_id=%s idle_timeout_seconds=%s",
            collection.collection_id,
            collection.scope.session_id,
            batch_id,
            self.idle_timeout_seconds,
        )
        return abandoned
