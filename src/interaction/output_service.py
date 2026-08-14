"""Ordered AgentResult and artifact selection to committed OutputBatch."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..artifacts.delivery import ArtifactDeliveryRecord, FileSystemArtifactDeliveryStore
from ..artifacts.models import ArtifactDeliveryState
from ..core.models import AgentResult
from ..ingress.models import CommittedInputBatch
from ..runtime.finalization_bridge import bind_final_output_assembler
from .capabilities import ClientCapabilitySnapshot
from .config import OutputRuntimeConfig
from .errors import InteractionValidationError
from .ids import new_output_part_id
from .output_models import (
    AnimationOutputPart,
    ArtifactOutputPart,
    AudioOutputPart,
    ImageOutputPart,
    OutputBatch,
    OutputBatchKind,
    OutputPart,
    StickerOutputPart,
    TextOutputPart,
    VideoNoteOutputPart,
    VideoOutputPart,
    VoiceOutputPart,
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
        bind_claim_validator = getattr(
            self.output_store,
            "bind_claim_validator",
            None,
        )
        if bind_claim_validator is not None:
            bind_claim_validator(self.renderer.plan)
        bind_final_output_assembler(self)

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
        if snapshot.client_type != input_batch.client_type.value:
            raise InteractionValidationError(
                "capability snapshot client type does not match input authority"
            )
        cycle_id = result.cycle_id
        if not cycle_id:
            raise InteractionValidationError(
                "final OutputBatch requires an authoritative cycle_id"
            )

        # A committed output is the immutable authority for this cycle. Repeated
        # finalization after delivery must not rebuild from mutable delivery states.
        existing = await self.output_store.get_for_cycle(
            session_id=input_batch.session_id,
            cycle_id=cycle_id,
            kind=OutputBatchKind.FINAL,
        )
        if existing is not None:
            await self._ensure_output_bindings(
                existing,
                input_batch=input_batch,
                client_instance_id=snapshot.client_instance_id,
            )
            result.output_batch = existing.model_dump(mode="json")
            return existing

        resolved_locale = locale or input_batch.locale or "ru"
        parts: list[OutputPart] = []
        if result.content:
            parts.append(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text=result.content,
                    parse_mode="markdown",
                )
            )

        semantic_parts = self._parse_semantic_parts(result.semantic_outputs)
        # A new OutputBatch may start only from deliveries whose previous outcome
        # is known not to be in flight. DELIVERING and UNKNOWN belong to an
        # existing attempt and must be resolved there, never assembled again.
        records = await self.delivery_store.list_cycle(
            session_id=input_batch.session_id,
            cycle_id=cycle_id,
            states={
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
            },
        )
        records = [
            item
            for item in records
            if item.state != ArtifactDeliveryState.CANCELLED
            and item.client_type == snapshot.client_type
        ]
        records.sort(key=lambda item: (item.selection_index, item.delivery_id))
        if len(records) > self.config.max_total_artifacts:
            raise InteractionValidationError(
                "selected artifacts exceed OutputBatch policy"
            )
        records_by_delivery = {
            item.delivery_id: item for item in records
        }

        semantic_artifacts: dict[str, ArtifactOutputPart] = {}
        non_artifact_semantic_parts: list[OutputPart] = []
        for item in semantic_parts:
            if not isinstance(item, ArtifactOutputPart):
                non_artifact_semantic_parts.append(item)
                continue
            record = records_by_delivery.get(item.delivery_id)
            if record is None or record.artifact_id != item.artifact_id:
                raise InteractionValidationError(
                    "semantic artifact output is not an exact safe delivery selection"
                )
            if item.delivery_id in semantic_artifacts:
                raise InteractionValidationError(
                    "semantic output contains duplicate artifact delivery intent"
                )
            self._validate_semantic_artifact_compatibility(item, record)
            semantic_artifacts[item.delivery_id] = item

        # Non-artifact semantic output follows the final text in declared order.
        # Artifact output is appended separately from the authoritative delivery
        # selection order, so an LLM intent can enrich but never reorder delivery.
        for item in non_artifact_semantic_parts:
            parts.append(
                item.model_copy(
                    update={
                        "part_id": item.part_id or new_output_part_id(),
                        "index": len(parts),
                    }
                )
            )

        for record in records:
            semantic = semantic_artifacts.get(record.delivery_id)
            if semantic is None:
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
                continue
            parts.append(
                semantic.model_copy(
                    update={
                        "part_id": semantic.part_id or new_output_part_id(),
                        "index": len(parts),
                        "required": True,
                        "artifact_id": record.artifact_id,
                        "delivery_id": record.delivery_id,
                        "filename": record.filename,
                        "mime_type": record.mime_type,
                        "size_bytes": record.size_bytes,
                        "metadata": {
                            **semantic.metadata,
                            "selection_index": record.selection_index,
                            "format_id": record.format_id,
                        },
                    }
                )
            )

        if not parts:
            raise InteractionValidationError(
                "final AgentResult contains no deliverable output"
            )
        if not any(part.required for part in parts):
            raise InteractionValidationError(
                "final OutputBatch requires at least one required output part"
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

        # max_metadata_bytes is the budget for the complete non-primary-text
        # semantic manifest, not only for each part's open-ended metadata dict.
        # Captions, titles, vCards, localization params and future typed fields
        # therefore cannot bypass the bounded-output policy.
        manifest_projection: list[dict[str, Any]] = []
        for item in parts:
            payload = item.model_dump(mode="json")
            if isinstance(item, TextOutputPart):
                payload.pop("text", None)
            manifest_projection.append(payload)
        semantic_manifest_size = len(
            json.dumps(
                manifest_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if semantic_manifest_size > self.config.max_metadata_bytes:
            raise InteractionValidationError(
                "output semantic manifest exceeds metadata policy"
            )

        batch = build_ready_output_batch(
            input_batch_id=input_batch.input_batch_id,
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
        committed, bound_records = await asyncio.to_thread(
            self._commit_with_output_bindings_sync,
            batch,
            records,
            input_batch.input_batch_id,
            snapshot.client_instance_id,
        )
        trace_bindings = getattr(
            self.delivery_store,
            "trace_output_bindings",
            None,
        )
        if trace_bindings is not None and bound_records:
            await trace_bindings(bound_records)
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

    def _commit_with_output_bindings_sync(
        self,
        batch: OutputBatch,
        records: list[ArtifactDeliveryRecord],
        input_batch_id: str,
        client_instance_id: str,
    ) -> tuple[OutputBatch, list[ArtifactDeliveryRecord]]:
        """Commit aggregate authority and artifact ownership as one unit."""

        with self.output_store._lock, self.delivery_store._lock:
            committed, created = self.output_store._commit_sync(batch)
            try:
                bound = self.delivery_store._bind_output_batch_sync(
                    [record.delivery_id for record in records],
                    committed.output_batch_id,
                    input_batch_id,
                    client_instance_id,
                )
            except BaseException:
                if created:
                    self.output_store._rollback_new_commit_sync(committed)
                raise
        return committed, bound if created else []

    async def _ensure_output_bindings(
        self,
        batch: OutputBatch,
        *,
        input_batch: CommittedInputBatch,
        client_instance_id: str,
    ) -> None:
        delivery_ids = [
            part.delivery_id
            for part in batch.parts
            if isinstance(part, ArtifactOutputPart)
        ]
        if not delivery_ids:
            return
        prior = [await self.delivery_store.get(item) for item in delivery_ids]
        bound = await self.delivery_store.bind_output_batch(
            delivery_ids,
            output_batch_id=batch.output_batch_id,
            input_batch_id=input_batch.input_batch_id,
            client_instance_id=client_instance_id,
        )
        newly_bound = [
            record
            for previous, record in zip(prior, bound, strict=True)
            if previous.output_batch_id is None
        ]
        trace_bindings = getattr(
            self.delivery_store,
            "trace_output_bindings",
            None,
        )
        if trace_bindings is not None and newly_bound:
            await trace_bindings(newly_bound)

    @staticmethod
    def _validate_semantic_artifact_compatibility(
        intent: ArtifactOutputPart,
        record: ArtifactDeliveryRecord,
    ) -> None:
        mime_type = record.mime_type.split(";", maxsplit=1)[0].strip().lower()
        compatible = True
        if isinstance(intent, ImageOutputPart):
            compatible = mime_type.startswith("image/")
        elif isinstance(intent, (AudioOutputPart, VoiceOutputPart)):
            compatible = mime_type.startswith("audio/")
        elif isinstance(intent, (VideoOutputPart, VideoNoteOutputPart)):
            compatible = mime_type.startswith("video/")
        elif isinstance(intent, AnimationOutputPart):
            compatible = (
                mime_type == "image/gif"
                or mime_type.startswith("video/")
            )
        elif isinstance(intent, StickerOutputPart):
            compatible = mime_type in {
                "image/webp",
                "application/x-tgsticker",
                "video/webm",
            }
        if not compatible:
            raise InteractionValidationError(
                "semantic artifact subtype is incompatible with authoritative MIME"
            )

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
