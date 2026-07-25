"""Shared logical-input grouping above the transport-neutral ingress service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

from .models import (
    ClientInputEnvelope,
    InputBatchDraftState,
    InputGroupingMode,
    InputSubmissionResult,
)
from .routing import resolve_input_grouping
from .service import ArtifactIngressService
from .store import (
    IngressConflictError,
    IngressNotFoundError,
)


logger = logging.getLogger("API.Ingress.Grouping")

_OPEN_DRAFT_STATES = {
    InputBatchDraftState.COLLECTING,
    InputBatchDraftState.SEALING,
    InputBatchDraftState.INGESTING,
    InputBatchDraftState.READY_TO_COMMIT,
}


class UnifiedArtifactIngressService(ArtifactIngressService):
    """Apply one authoritative grouping policy for every transport entrypoint.

    Callers may still pass a grouping hint for compatibility, but the durable
    store and currently open drafts determine the final logical InputBatch.
    This class intentionally stops before cycle admission; ``CycleInbox`` and
    active-cycle continuation belong to ``v0.4-input-runtime``.
    """

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        if not self.config.enabled:
            return await super().submit_atomic(
                envelope,
                session_id=session_id,
                upload_streams=upload_streams,
                grouping_mode=grouping_mode,
                grouping_key=grouping_key,
            )

        list_open = getattr(self.batch_store, "list_open_drafts", None)
        open_drafts = (
            await list_open(session_id=session_id)
            if list_open is not None
            else []
        )
        decision = resolve_input_grouping(
            envelope,
            open_drafts=open_drafts,
        )
        # Preserve the established direct-ingress meaning of an explicit
        # atomic upload. Client adapters that group media pass a non-atomic
        # mode/key and still use the shared resolver/state machine.
        if (
            grouping_mode == InputGroupingMode.ATOMIC
            and grouping_key is None
            and envelope.attachment_slots
            and any(
                slot.upload_field_name is not None
                for slot in envelope.attachment_slots
            )
        ):
            from .routing import InputGroupingDecision

            decision = InputGroupingDecision(
                mode=InputGroupingMode.ATOMIC,
                key=(
                    f"{envelope.client_type.value}:"
                    f"{envelope.client_instance_id}:"
                    f"event:{envelope.idempotency_key}"
                ),
            )
        if (
            grouping_mode != decision.mode
            or (grouping_key is not None and grouping_key != decision.key)
        ):
            logger.info(
                "ingress_grouping_hint_overridden session_id=%s "
                "source_message_id=%s hinted_mode=%s resolved_mode=%s "
                "joined_input_batch_id=%s",
                session_id,
                envelope.source_message_id,
                grouping_mode.value,
                decision.mode.value,
                decision.joined_input_batch_id,
            )
        else:
            logger.info(
                "ingress_grouping_resolved session_id=%s source_message_id=%s "
                "mode=%s joined_input_batch_id=%s",
                session_id,
                envelope.source_message_id,
                decision.mode.value,
                decision.joined_input_batch_id,
            )

        if decision.joined_input_batch_id is not None:
            self._validate_envelope_limits(envelope)
            defer_commit = getattr(self.batch_store, "defer_commit", None)
            if defer_commit is not None:
                await defer_commit(decision.joined_input_batch_id)
            capability_snapshot, resolved_locale = await self._resolve_interaction(
                envelope
            )
            event, duplicate_event = await self.event_store.save_if_absent(
                envelope,
                capability_snapshot=capability_snapshot,
                resolved_locale=resolved_locale,
            )
            existing_draft, existing_committed = await self.batch_store.find_by_event(
                event.event_id
            )
            if existing_committed is not None:
                result = InputSubmissionResult(
                    event_id=event.event_id,
                    input_batch_id=existing_committed.input_batch_id,
                    state="committed",
                    duplicate=True,
                    committed_batch=existing_committed,
                )
                return await self._decorate_result(result, envelope=envelope)
            if existing_draft is not None:
                result = InputSubmissionResult(
                    event_id=event.event_id,
                    input_batch_id=existing_draft.input_batch_id,
                    state="collecting",
                    duplicate=True,
                )
                return await self._decorate_result(result, envelope=envelope)

            append_exact = getattr(self.batch_store, "append_event_to_batch", None)
            if append_exact is None:
                raise IngressConflictError(
                    "Exact input draft joins are not supported by the batch store"
                )
            try:
                draft = await append_exact(
                    decision.joined_input_batch_id,
                    event,
                )
            except IngressConflictError as error:
                # Only a true close/commit race may become a new atomic batch.
                # Validation, authority and per-batch limit conflicts on an open
                # draft must remain visible instead of being silently bypassed.
                try:
                    latest = await self.batch_store.get_draft(
                        decision.joined_input_batch_id
                    )
                except IngressNotFoundError:
                    latest = None
                if latest is not None and latest.state in _OPEN_DRAFT_STATES:
                    raise error

                logger.info(
                    "ingress_exact_join_raced_with_commit session_id=%s "
                    "source_message_id=%s target_input_batch_id=%s",
                    session_id,
                    envelope.source_message_id,
                    decision.joined_input_batch_id,
                )
                return await super().submit_atomic(
                    envelope,
                    session_id=session_id,
                    upload_streams=upload_streams,
                    grouping_mode=InputGroupingMode.ATOMIC,
                    grouping_key=None,
                )

            logger.info(
                "ingress_event_joined_exact_draft input_batch_id=%s event_id=%s "
                "text_part_count=%s attachment_count=%s",
                draft.input_batch_id,
                event.event_id,
                len(event.text_parts),
                len(event.attachment_slots),
            )
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="collecting",
                duplicate=duplicate_event,
            )
            return await self._decorate_result(result, envelope=envelope)

        return await super().submit_atomic(
            envelope,
            session_id=session_id,
            upload_streams=upload_streams,
            grouping_mode=decision.mode,
            grouping_key=decision.key,
        )
