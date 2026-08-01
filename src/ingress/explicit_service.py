"""Shared ingress admission for active explicit input collections."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

from .collection_models import (
    InputCollectionState,
    InputDraftControlStatus,
    InputDraftScope,
)
from .explicit_policy import (
    EXPLICIT_COLLECTION_GROUPING_MODE,
    EXPLICIT_COLLECTION_ROUTE_METADATA_KEY,
)
from .models import ClientInputEnvelope, InputGroupingMode, InputSubmissionResult
from .resilient_service import ResilientUnifiedArtifactIngressService
from .store import IngressConflictError


logger = logging.getLogger("API.Ingress.ExplicitCollection")


class ExplicitCollectionIngressService(ResilientUnifiedArtifactIngressService):
    """Route every event in one exact scope to its active explicit collection."""

    def __init__(
        self,
        *args,
        collection_store,
        draft_control_service,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.collection_store = collection_store
        self.draft_control_service = draft_control_service

    async def commit_ready_drafts(self):
        await self.draft_control_service.reconcile_active_collections()
        return await super().commit_ready_drafts()

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        scope = self._collection_scope(envelope, session_id=session_id)
        active = await self.collection_store.get_active(scope)
        if active is not None:
            active = await self.draft_control_service.reconcile_collection(active)
            if not active.is_active:
                active = None

        # COMMIT_REQUESTED is a closed admission boundary. A Telegram update
        # arriving after /send must not escape into AUTO/media_group mode and
        # start a second agent cycle. It is rejected against the same explicit
        # workflow and can be retried only in a newly opened collection.
        if (
            active is not None
            and active.state == InputCollectionState.COMMIT_REQUESTED
        ):
            raise IngressConflictError(
                "Explicit collection no longer accepts new input events"
            )

        collect_into = (
            active
            if active is not None
            and active.state == InputCollectionState.COLLECTING
            else None
        )
        if collect_into is not None:
            collect_into = await self.collection_store.touch(
                collect_into.collection_id
            )

        sanitized = self._with_collection_route(
            envelope,
            collection_id=(
                collect_into.collection_id if collect_into is not None else None
            ),
        )

        effective_mode = grouping_mode
        effective_key = grouping_key
        if collect_into is not None:
            effective_mode = EXPLICIT_COLLECTION_GROUPING_MODE
            effective_key = collect_into.collection_id
            logger.info(
                "ingress_explicit_collection_routed collection_id=%s "
                "session_id=%s source_message_id=%s bound_input_batch_id=%s",
                collect_into.collection_id,
                session_id,
                envelope.source_message_id,
                collect_into.bound_input_batch_id,
            )

        try:
            result = await super().submit_atomic(
                sanitized,
                session_id=session_id,
                upload_streams=upload_streams,
                grouping_mode=effective_mode,
                grouping_key=effective_key,
            )
        except Exception as error:
            # A rejected event (limits, conflict, invalid client input) is not a
            # terminal collection failure. The durable collection and already
            # stored members remain available for /cancel or a valid /send.
            # Infrastructure failures are reflected by the draft/result recovery
            # layer and reconciled from its authoritative state.
            if collect_into is not None:
                logger.warning(
                    "ingress_explicit_collection_event_rejected "
                    "collection_id=%s session_id=%s source_message_id=%s "
                    "error_type=%s",
                    collect_into.collection_id,
                    session_id,
                    envelope.source_message_id,
                    type(error).__name__,
                )
            raise

        if collect_into is None:
            return result
        result = self._with_explicit_presentation(
            result,
            collection_id=collect_into.collection_id,
        )
        if result.state == "failed":
            latest = await self.collection_store.get_active(scope)
            if latest is not None and latest.collection_id == collect_into.collection_id:
                await self.collection_store.mark_terminal(
                    latest.collection_id,
                    state=InputCollectionState.FAILED,
                    failure_code=result.error_code or "explicit_collection_ingress_failed",
                )
            return result

        binding = await self.draft_control_service.bind_batch(
            scope,
            input_batch_id=result.input_batch_id,
        )
        if binding.status == InputDraftControlStatus.CONFLICT:
            raise IngressConflictError(
                binding.error_code or "Explicit collection bind conflict"
            )
        if binding.status == InputDraftControlStatus.NOT_FOUND:
            raise IngressConflictError(
                binding.error_code or "Explicit collection is no longer active"
            )
        logger.info(
            "ingress_explicit_collection_bound collection_id=%s "
            "input_batch_id=%s source_message_id=%s file_count=%s "
            "text_part_count=%s",
            collect_into.collection_id,
            result.input_batch_id,
            envelope.source_message_id,
            binding.file_count,
            binding.text_part_count,
        )
        return result

    @staticmethod
    def _collection_scope(
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
    ) -> InputDraftScope:
        return InputDraftScope(
            session_id=session_id,
            client_type=envelope.client_type,
            client_instance_id=envelope.client_instance_id,
            conversation=envelope.conversation,
            principal_id=envelope.sender.principal_id,
        )

    @staticmethod
    def _with_collection_route(
        envelope: ClientInputEnvelope,
        *,
        collection_id: str | None,
    ) -> ClientInputEnvelope:
        metadata = dict(envelope.response_route.metadata or {})
        metadata.pop(EXPLICIT_COLLECTION_ROUTE_METADATA_KEY, None)
        if collection_id is not None:
            metadata[EXPLICIT_COLLECTION_ROUTE_METADATA_KEY] = collection_id
        route = envelope.response_route.model_copy(
            update={"metadata": metadata}
        )
        return envelope.model_copy(update={"response_route": route})

    @staticmethod
    def _with_explicit_presentation(
        result: InputSubmissionResult,
        *,
        collection_id: str,
    ) -> InputSubmissionResult:
        event = result.presentation_event
        if event is None:
            return result
        params = dict(event.params or {})
        params.update(
            {
                "assembly_mode": "explicit",
                "commit_policy": "explicit",
                "auto_commit_allowed": False,
                "collection_id": collection_id,
            }
        )
        return result.model_copy(
            update={
                "presentation_event": event.model_copy(
                    update={"params": params}
                )
            }
        )
