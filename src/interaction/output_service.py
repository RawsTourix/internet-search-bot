"""Ordered AgentResult and artifact selection to committed OutputBatch."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..artifacts.delivery import FileSystemArtifactDeliveryStore
from ..artifacts.models import ArtifactDeliveryState
from ..core.models import AgentResult
from ..ingress.models import CommittedInputBatch
from .capabilities import ClientCapabilitySnapshot
from .config import OutputRuntimeConfig
from .errors import InteractionValidationError
from .ids import new_output_part_id
from .output_models import (
    ArtifactOutputPart,
    OutputBatch,
    OutputBatchKind,
    OutputPart,
    TextOutputPart,
)
from .output_store import FileSystemOutputBatchStore, build_ready_output_batch
from .rendering import CapabilityOutputRenderer, ClientOutputRenderer


_OUTPUT_PARTS = TypeAdapter(list[OutputPart])
logger = logging.getLogger("Interaction.Output")


class OutputBatchAssembler:
    """Assemble exactly once from authoritative result and delivery records."""

    def __init__(
        self,
        *,
        config: OutputRuntimeConfig,
        delivery_store: FileSystemArtifactDeliveryStore,
        output_store: FileSystemOutputBatchStore,
        renderer: ClientOutputRenderer | None = None,
    ) -> None:
        self.config = config
        self.delivery_store = delivery_store
        self.output_store = output_store
        self.renderer = renderer or CapabilityOutputRenderer(
            max_delivery_groups=config.max_delivery_groups
        )

    async def assemble_final(
        self,
        *,
        result: AgentResult,
        input_batch: CommittedInputBatch,
        capability_snapshot: ClientCapabilitySnapshot | None = None,
        locale: str | None = None,
    ) -> OutputBatch:
        if not self.config.enabled:
            raise InteractionValidationError("output runtime is disabled")
        snapshot = capability_snapshot or input_batch.capability_snapshot
        if snapshot is None:
            raise InteractionValidationError(
                "final OutputBatch requires a capability snapshot"
            )
        cycle_id = result.cycle_id
        if not cycle_id:
            raise InteractionValidationError(
                "final OutputBatch requires an authoritative cycle_id"
            )
        resolved_locale = locale or input_batch.locale or "ru"

        parts: list[OutputPart] = []
        if result.content:
            parts.append(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text=result.content,
                )
            )
        semantic_parts = self._parse_semantic_parts(result.semantic_outputs)
        for item in semantic_parts:
            if isinstance(item, ArtifactOutputPart):
                # Artifact selections below own exact delivery identity/order.
                continue
            parts.append(
                item.model_copy(
                    update={
                        "part_id": item.part_id or new_output_part_id(),
                        "index": len(parts),
                    }
                )
            )

        records = await self.delivery_store.list_cycle(
            session_id=input_batch.session_id,
            cycle_id=cycle_id,
            states={
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.DELIVERING,
                ArtifactDeliveryState.UNKNOWN,
                ArtifactDeliveryState.FAILED,
            },
        )
        records = [
            item
            for item in records
            if item.state != ArtifactDeliveryState.CANCELLED
        ]
        records.sort(key=lambda item: (item.selection_index, item.delivery_id))
        if len(records) > self.config.max_total_artifacts:
            raise InteractionValidationError(
                "selected artifacts exceed OutputBatch policy"
            )
        for record in records:
            parts.append(
                ArtifactOutputPart(
                    part_id=new_output_part_id(),
                    index=len(parts),
                    artifact_id=record.artifact_id,
                    delivery_id=record.delivery_id,
                    filename=record.filename,
                    mime_type=record.mime_type,
                    size_bytes=record.size_bytes,
                    metadata={
                        "selection_index": record.selection_index,
                        "format_id": record.format_id,
                    },
                )
            )
        if not parts:
            parts.append(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text=(
                        "Done."
                        if resolved_locale.lower().startswith("en")
                        else "Готово."
                    ),
                )
            )
        if len(parts) > self.config.max_parts_per_batch:
            raise InteractionValidationError(
                "output parts exceed OutputBatch policy"
            )
        if sum(
            len(item.text)
            for item in parts
            if isinstance(item, TextOutputPart)
        ) > self.config.max_text_chars:
            raise InteractionValidationError("output text exceeds policy")
        metadata_size = len(json.dumps(
            [item.metadata for item in parts],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        if metadata_size > self.config.max_metadata_bytes:
            raise InteractionValidationError("output metadata exceeds policy")

        batch = build_ready_output_batch(
            session_id=input_batch.session_id,
            cycle_id=cycle_id,
            sequence_number=input_batch.sequence_number,
            kind=OutputBatchKind.FINAL,
            response_route=input_batch.response_route,
            response_anchor=input_batch.response_anchor,
            locale=resolved_locale,
            capability_snapshot=snapshot,
            parts=tuple(parts),
        )
        self.renderer.plan(batch)
        committed, _ = await self.output_store.commit(batch)
        logger.info(
            "output_batch_ready output_batch_id=%s session_id=%s cycle_id=%s "
            "part_count=%s",
            committed.output_batch_id,
            committed.session_id,
            committed.cycle_id,
            len(committed.parts),
        )
        result.output_batch = committed.model_dump(mode="json")
        return committed

    @staticmethod
    def _parse_semantic_parts(values: list[dict[str, Any]]) -> list[OutputPart]:
        if not values:
            return []
        normalized = []
        for index, value in enumerate(values):
            item = dict(value)
            item.setdefault("part_id", new_output_part_id())
            item.setdefault("index", index)
            normalized.append(item)
        try:
            return _OUTPUT_PARTS.validate_python(normalized)
        except ValidationError as error:
            raise InteractionValidationError(
                "AgentResult contains invalid semantic output"
            ) from error
