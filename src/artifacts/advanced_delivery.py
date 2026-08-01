"""Advanced v0.4 delivery safety policy layered over filesystem stores."""

from __future__ import annotations

from typing import Any

from .delivery import (
    ArtifactDeliveryRecord,
    ArtifactDeliveryService,
    FileSystemArtifactDeliveryStore,
    utc_now,
)
from .errors import ArtifactDeliveryError, ArtifactDeliveryNotFoundError
from .models import (
    ArtifactAccessContext,
    ArtifactDeliveryRef,
    ArtifactDeliveryState,
)


class AdvancedFileSystemArtifactDeliveryStore(FileSystemArtifactDeliveryStore):
    """Keep one retryable delivery head per lineage/client/cycle.

    Confirmed-not-started or confirmed-failed heads may be superseded by a new
    exact artifact version. DELIVERING and UNKNOWN heads are never silently
    replaced because their bytes may already have reached the client.
    """

    def __init__(
        self,
        storage_config,
        *,
        trace_service=None,
    ) -> None:
        super().__init__(storage_config)
        self.trace_service = trace_service

    async def select(self, record: ArtifactDeliveryRecord) -> ArtifactDeliveryRecord:
        selected = await super().select(record)
        await self._trace_records(
            [selected],
            event_type="artifact_delivery_selected",
            status="succeeded",
        )
        return selected

    async def select_many(
        self,
        records: list[ArtifactDeliveryRecord],
    ) -> list[ArtifactDeliveryRecord]:
        selected = await super().select_many(records)
        await self._trace_records(
            selected,
            event_type="artifact_delivery_selected",
            status="succeeded",
        )
        return selected

    async def transition(
        self,
        delivery_id: str,
        *,
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        error: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> ArtifactDeliveryRecord:
        record = await super().transition(
            delivery_id,
            target=target,
            allowed_from=allowed_from,
            error=error,
            receipt=receipt,
        )
        await self._trace_records(
            [record],
            event_type=self._transition_event_type(target),
            status=self._transition_status(target),
            error=error,
        )
        return record

    async def transition_many(
        self,
        delivery_ids: list[str],
        *,
        target: ArtifactDeliveryState,
        allowed_from: set[ArtifactDeliveryState],
        receipt_by_delivery_id: dict[str, dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> list[ArtifactDeliveryRecord]:
        records = await super().transition_many(
            delivery_ids,
            target=target,
            allowed_from=allowed_from,
            receipt_by_delivery_id=receipt_by_delivery_id,
            error=error,
        )
        await self._trace_records(
            records,
            event_type=self._transition_event_type(target),
            status=self._transition_status(target),
            error=error,
        )
        return records

    async def cancel_many(
        self,
        delivery_ids: list[str],
    ) -> list[ArtifactDeliveryRecord]:
        records = await super().cancel_many(delivery_ids)
        await self._trace_records(
            records,
            event_type="artifact_delivery_cancelled",
            status="succeeded",
        )
        return records

    async def _trace_records(
        self,
        records: list[ArtifactDeliveryRecord],
        *,
        event_type: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if self.trace_service is None:
            return
        for record in records:
            await self.trace_service.record(
                session_id=record.session_id,
                cycle_id=record.cycle_id,
                event_type=event_type,
                stage="delivery",
                status=status,
                direction="outbound",
                correlation={"delivery_id": record.delivery_id},
                transport={"client_type": record.client_type},
                artifact={
                    "artifact_id": record.artifact_id,
                    "artifact_lineage_id": record.artifact_lineage_id,
                    "content_id": record.content_id,
                    "filename": record.filename,
                    "format_id": record.format_id,
                    "mime_type": record.mime_type,
                    "size_bytes": record.size_bytes,
                    "content_hash": record.content_hash,
                },
                metrics={
                    "attempt_count": record.attempt_count,
                    "selection_index": record.selection_index,
                },
                error=(
                    {
                        "error_type": "ArtifactDeliveryError",
                        "message": error,
                    }
                    if error
                    else None
                ),
                data={"state": record.state.value},
            )

    @staticmethod
    def _transition_event_type(target: ArtifactDeliveryState) -> str:
        return {
            ArtifactDeliveryState.SELECTED: "artifact_delivery_selected",
            ArtifactDeliveryState.DELIVERING: "artifact_delivery_started",
            ArtifactDeliveryState.DELIVERED: "artifact_delivery_succeeded",
            ArtifactDeliveryState.FAILED: "artifact_delivery_failed",
            ArtifactDeliveryState.UNKNOWN: "artifact_delivery_unknown",
            ArtifactDeliveryState.CANCELLED: "artifact_delivery_cancelled",
        }[target]

    @staticmethod
    def _transition_status(target: ArtifactDeliveryState) -> str:
        return {
            ArtifactDeliveryState.SELECTED: "succeeded",
            ArtifactDeliveryState.DELIVERING: "started",
            ArtifactDeliveryState.DELIVERED: "succeeded",
            ArtifactDeliveryState.FAILED: "failed",
            ArtifactDeliveryState.UNKNOWN: "unknown",
            ArtifactDeliveryState.CANCELLED: "succeeded",
        }[target]

    def _select_many_sync(
        self,
        records: list[ArtifactDeliveryRecord],
    ) -> list[ArtifactDeliveryRecord]:
        if not records:
            return []
        with self._lock:
            first = records[0]
            if any(
                item.session_id != first.session_id
                or item.cycle_id != first.cycle_id
                or item.client_type != first.client_type
                for item in records
            ):
                raise ArtifactDeliveryError(
                    "Batch delivery records must share one runtime authority"
                )

            lineage_targets: dict[str, str] = {}
            for item in records:
                existing_target = lineage_targets.get(
                    item.artifact_lineage_id
                )
                if (
                    existing_target is not None
                    and existing_target != item.artifact_id
                ):
                    raise ArtifactDeliveryError(
                        "A delivery batch cannot select multiple versions "
                        "of one lineage"
                    )
                lineage_targets[item.artifact_lineage_id] = item.artifact_id

            existing_records = self._list_cycle_sync(
                first.session_id,
                first.cycle_id,
                set(),
            )
            updates: dict[str, tuple[ArtifactDeliveryRecord, bool]] = {}
            selected_by_artifact: dict[str, ArtifactDeliveryRecord] = {}
            next_index = (
                max(
                    (
                        item.selection_index
                        for item in existing_records
                        if item.state != ArtifactDeliveryState.CANCELLED
                    ),
                    default=-1,
                )
                + 1
            )
            replaceable = {
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
            }
            blocking = {
                ArtifactDeliveryState.DELIVERING,
                ArtifactDeliveryState.UNKNOWN,
            }

            for item in records:
                existing_exact = next(
                    (
                        existing
                        for existing in existing_records
                        if existing.artifact_id == item.artifact_id
                        and existing.client_type == item.client_type
                        and existing.state != ArtifactDeliveryState.CANCELLED
                    ),
                    None,
                )
                if existing_exact is not None:
                    if existing_exact.state in blocking:
                        raise ArtifactDeliveryError(
                            "Cannot select an exact artifact with an active or "
                            "unknown delivery outcome"
                        )
                    selected_by_artifact[item.artifact_id] = existing_exact
                    continue

                lineage_heads = [
                    existing
                    for existing in existing_records
                    if existing.artifact_lineage_id
                    == item.artifact_lineage_id
                    and existing.client_type == item.client_type
                    and existing.artifact_id != item.artifact_id
                    and existing.state != ArtifactDeliveryState.CANCELLED
                ]
                if any(existing.state in blocking for existing in lineage_heads):
                    raise ArtifactDeliveryError(
                        "Cannot replace a lineage with an active or unknown "
                        "delivery outcome"
                    )

                replaceable_heads = [
                    existing
                    for existing in lineage_heads
                    if existing.state in replaceable
                ]
                inherited_index = (
                    min(
                        existing.selection_index
                        for existing in replaceable_heads
                    )
                    if replaceable_heads
                    else None
                )
                replacement_time = utc_now()
                for existing in replaceable_heads:
                    updates[existing.delivery_id] = (
                        existing.model_copy(
                            update={
                                "state": ArtifactDeliveryState.CANCELLED,
                                "updated_at": replacement_time,
                            }
                        ),
                        True,
                    )

                selection_index = (
                    inherited_index
                    if inherited_index is not None
                    else next_index
                )
                if inherited_index is None:
                    next_index += 1
                ordered_item = item.model_copy(
                    update={"selection_index": selection_index}
                )
                updates[item.delivery_id] = (ordered_item, False)
                selected_by_artifact[item.artifact_id] = ordered_item

            self._commit_batch_sync(updates)
            return [
                selected_by_artifact[item.artifact_id] for item in records
            ]


class AdvancedArtifactDeliveryService(ArtifactDeliveryService):
    """Preserve ambiguous UNKNOWN outcomes until explicit reconciliation."""

    _CANCELLABLE = {
        ArtifactDeliveryState.SELECTED,
        ArtifactDeliveryState.FAILED,
    }

    async def claim(self, delivery_id: str) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=ArtifactDeliveryState.DELIVERING,
            allowed_from={
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
            },
        )
        return record.public_ref()

    async def cancel(self, delivery_id: str) -> ArtifactDeliveryRef:
        record = await self.store.transition(
            delivery_id,
            target=ArtifactDeliveryState.CANCELLED,
            allowed_from=set(self._CANCELLABLE),
        )
        return record.public_ref()

    async def cancel_many_by_artifact_ids(
        self,
        *,
        artifact_ids: list[str],
        access: ArtifactAccessContext,
        client_type: str,
    ) -> list[ArtifactDeliveryRef]:
        """Cancel exact retryable selections without erasing unknown evidence."""
        unique_ids = list(dict.fromkeys(artifact_ids))
        for artifact_id in unique_ids:
            await self.artifact_service.get_artifact(
                artifact_id,
                access=access,
            )

        records = await self.store.list_cycle(
            session_id=access.session_id,
            cycle_id=access.cycle_id,
        )
        delivery_ids_by_artifact: dict[str, str] = {}
        for artifact_id in unique_ids:
            matches = [
                item
                for item in records
                if item.artifact_id == artifact_id
                and item.client_type == client_type
                and item.state != ArtifactDeliveryState.CANCELLED
            ]
            if not matches:
                raise ArtifactDeliveryNotFoundError(
                    "No delivery selection exists for this artifact"
                )
            latest = max(
                matches,
                key=lambda item: (item.updated_at, item.delivery_id),
            )
            if latest.state not in self._CANCELLABLE:
                raise ArtifactDeliveryError(
                    "Cannot cancel an active, delivered, or unknown delivery "
                    f"in state {latest.state.value}"
                )
            delivery_ids_by_artifact[artifact_id] = latest.delivery_id

        cancelled = await self.store.cancel_many([
            delivery_ids_by_artifact[artifact_id]
            for artifact_id in unique_ids
        ])
        by_delivery_id = {
            item.delivery_id: item.public_ref() for item in cancelled
        }
        return [
            by_delivery_id[delivery_ids_by_artifact[artifact_id]]
            for artifact_id in artifact_ids
        ]
