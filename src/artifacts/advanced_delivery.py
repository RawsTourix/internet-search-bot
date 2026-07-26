"""Advanced v0.4 delivery safety policy layered over filesystem stores."""

from __future__ import annotations

from .delivery import (
    ArtifactDeliveryRecord,
    ArtifactDeliveryService,
    FileSystemArtifactDeliveryStore,
    utc_now,
)
from .errors import ArtifactDeliveryError
from .models import ArtifactDeliveryRef, ArtifactDeliveryState


class AdvancedFileSystemArtifactDeliveryStore(FileSystemArtifactDeliveryStore):
    """Keep one retryable delivery head per lineage/client/cycle.

    Confirmed-not-started or confirmed-failed heads may be superseded by a new
    exact artifact version. DELIVERING and UNKNOWN heads are never silently
    replaced because their bytes may already have reached the client.
    """

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
    """Disallow normal retry of an ambiguous UNKNOWN delivery."""

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
