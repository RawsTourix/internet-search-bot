"""Capability-driven semantic output planning with deterministic fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..localization.service import LocalizationService
from ..localization.models import LocalizationMessage
from .config import LocalizationConfigType
from .capabilities import ClientCapabilitySnapshot
from .errors import InteractionValidationError
from .ids import new_output_delivery_group_id
from .output_models import (
    AnimationOutputPart,
    ArtifactOutputPart,
    AudioOutputPart,
    ContactOutputPart,
    ImageOutputPart,
    LocationOutputPart,
    OutputBatch,
    OutputDeliveryGroup,
    OutputDeliveryPlan,
    OutputPart,
    StatusOutputPart,
    StickerOutputPart,
    TextOutputPart,
    TransportOperationKind,
    VideoNoteOutputPart,
    VideoOutputPart,
    VoiceOutputPart,
)


@dataclass(frozen=True)
class ClientRenderContext:
    locale: str
    capabilities: ClientCapabilitySnapshot


class ClientOutputRenderer(Protocol):
    def supports(
        self, part: OutputPart, capabilities: ClientCapabilitySnapshot
    ) -> bool: ...

    def plan(self, batch: OutputBatch) -> OutputDeliveryPlan: ...


def _has(snapshot: ClientCapabilitySnapshot, feature: str) -> bool:
    return feature in snapshot.features


def _operation_for(
    part: OutputPart, snapshot: ClientCapabilitySnapshot
) -> TransportOperationKind:
    mappings: tuple[tuple[type, str, TransportOperationKind], ...] = (
        (ImageOutputPart, "output.media.image", TransportOperationKind.IMAGE),
        (AudioOutputPart, "output.media.audio", TransportOperationKind.AUDIO),
        (VoiceOutputPart, "output.media.voice", TransportOperationKind.VOICE),
        (VideoNoteOutputPart, "output.media.video_note", TransportOperationKind.VIDEO_NOTE),
        (VideoOutputPart, "output.media.video", TransportOperationKind.VIDEO),
        (AnimationOutputPart, "output.media.animation", TransportOperationKind.ANIMATION),
        (StickerOutputPart, "output.media.sticker", TransportOperationKind.STICKER),
        (LocationOutputPart, "output.location", TransportOperationKind.LOCATION),
        (ContactOutputPart, "output.contact", TransportOperationKind.CONTACT),
        (ArtifactOutputPart, "output.artifact.document", TransportOperationKind.DOCUMENT),
        (TextOutputPart, "output.text", TransportOperationKind.TEXT),
        (
            StatusOutputPart,
            "presentation.status_updates",
            TransportOperationKind.STATUS,
        ),
    )
    for part_type, feature, operation in mappings:
        if isinstance(part, part_type):
            if _has(snapshot, feature):
                return operation
            if (
                not isinstance(part, TextOutputPart)
                and _has(snapshot, "output.text")
            ):
                return TransportOperationKind.TEXT
            return TransportOperationKind.UNSUPPORTED
    return TransportOperationKind.UNSUPPORTED


class CapabilityOutputRenderer:
    """Builds an ordered plan; transport execution remains adapter-owned."""

    def __init__(
        self,
        localization: LocalizationService | None = None,
        *,
        max_delivery_groups: int = 64,
        prefer_document_groups: bool = True,
    ) -> None:
        self.localization = localization or LocalizationService.from_directory(
            config=LocalizationConfigType()
        )
        self.max_delivery_groups = max_delivery_groups
        self.prefer_document_groups = prefer_document_groups

    def supports(
        self, part: OutputPart, capabilities: ClientCapabilitySnapshot
    ) -> bool:
        return _operation_for(part, capabilities) != TransportOperationKind.UNSUPPORTED

    def plan(self, batch: OutputBatch) -> OutputDeliveryPlan:
        groups: list[OutputDeliveryGroup] = []
        max_group = int(
            batch.capability_snapshot.limits.get(
                "transport.telegram.output.document_group.max_items",
                1,
            )
        )
        may_group_documents = _has(
            batch.capability_snapshot, "output.group.document"
        ) and max_group > 1 and self.prefer_document_groups
        pending_documents: list[ArtifactOutputPart] = []

        def flush_documents() -> None:
            nonlocal pending_documents
            while pending_documents:
                chunk = pending_documents[:max_group]
                pending_documents = pending_documents[max_group:]
                kind = (
                    TransportOperationKind.DOCUMENT_GROUP
                    if len(chunk) > 1
                    else TransportOperationKind.DOCUMENT
                )
                groups.append(
                    OutputDeliveryGroup(
                        group_id=new_output_delivery_group_id(),
                        index=len(groups),
                        operation_kind=kind,
                        part_ids=tuple(item.part_id for item in chunk),
                        required=any(item.required for item in chunk),
                    )
                )

        for part in batch.parts:
            operation = _operation_for(part, batch.capability_snapshot)
            if (
                may_group_documents
                and operation == TransportOperationKind.DOCUMENT
                and type(part) is ArtifactOutputPart
            ):
                pending_documents.append(part)
                continue
            flush_documents()
            groups.append(
                OutputDeliveryGroup(
                    group_id=new_output_delivery_group_id(),
                    index=len(groups),
                    operation_kind=operation,
                    part_ids=(part.part_id,),
                    required=part.required,
                    rendered_text=(
                        part.text
                        if isinstance(part, TextOutputPart)
                        else self._fallback_text(
                            part,
                            locale=batch.locale,
                        )
                        if (
                            operation == TransportOperationKind.TEXT
                            or isinstance(part, StatusOutputPart)
                        )
                        else None
                    ),
                )
            )
        flush_documents()
        if len(groups) > self.max_delivery_groups:
            raise InteractionValidationError(
                "delivery plan exceeds configured group limit"
            )
        return OutputDeliveryPlan(
            output_batch_id=batch.output_batch_id,
            groups=tuple(groups),
            created_at=batch.created_at,
        )

    def _fallback_text(self, part: OutputPart, *, locale: str) -> str:
        if isinstance(part, StatusOutputPart):
            if self.localization is not None:
                return self.localization.render(part.message, locale=locale)
            return part.message.message_key
        if isinstance(part, LocationOutputPart):
            message = LocalizationMessage(
                message_key="fallback.location",
                params={
                    "title": f"{part.title}: " if part.title else "",
                    "latitude": f"{part.latitude:.6f}",
                    "longitude": f"{part.longitude:.6f}",
                },
            )
        elif isinstance(part, ContactOutputPart):
            name = " ".join(
                value for value in (part.first_name, part.last_name) if value
            )
            message = LocalizationMessage(
                message_key="fallback.contact",
                params={"name": name, "phone_number": part.phone_number},
            )
        elif isinstance(part, ArtifactOutputPart):
            message = LocalizationMessage(
                message_key="fallback.artifact",
                params={"filename": part.filename},
            )
        else:
            message = LocalizationMessage(
                message_key="output.unsupported_part"
            )
        if self.localization is None:
            return message.message_key
        return self.localization.render(message, locale=locale)
