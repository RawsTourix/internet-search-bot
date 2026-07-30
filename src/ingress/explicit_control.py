"""Crash-safe control orchestration for explicit input collections."""

from __future__ import annotations

from .collection_models import (
    InputCollectionRecord,
    InputCollectionState,
    InputDraftControlAction,
    InputDraftControlResult,
    InputDraftControlStatus,
    InputDraftScope,
)
from .draft_control import InputDraftControlService
from .explicit_policy import is_explicit_collection_draft
from .models import ClientResponseRoute, InputBatchDraftState


class ExplicitInputDraftControlService(InputDraftControlService):
    """Promote and reconcile bound drafts before exposing control results."""

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
            current = await self.reconcile_collection(collection)
            if current.bound_input_batch_id is not None:
                draft = await self.batch_store.get_draft(
                    current.bound_input_batch_id
                )
                terminal_state = {
                    InputBatchDraftState.COMMITTED: InputCollectionState.COMMITTED,
                    InputBatchDraftState.CANCELLED: InputCollectionState.CANCELLED,
                    InputBatchDraftState.ABANDONED: InputCollectionState.ABANDONED,
                    InputBatchDraftState.FAILED: InputCollectionState.FAILED,
                }.get(draft.state)
                if terminal_state is not None:
                    current = await self.collection_store.mark_terminal(
                        current.collection_id,
                        state=terminal_state,
                        failure_code=draft.failure_code,
                    )
            result.append(current)
        return result

    async def reconcile_collection(
        self,
        collection: InputCollectionRecord,
    ) -> InputCollectionRecord:
        if not collection.is_active:
            return collection

        if collection.bound_input_batch_id is not None:
            draft = await self.batch_store.get_draft(
                collection.bound_input_batch_id
            )
            if draft.state in {
                InputBatchDraftState.COLLECTING,
                InputBatchDraftState.SEALING,
                InputBatchDraftState.INGESTING,
                InputBatchDraftState.READY_TO_COMMIT,
            } and not is_explicit_collection_draft(draft):
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
        return await self.collection_store.bind(
            collection.collection_id,
            draft.input_batch_id,
        )
