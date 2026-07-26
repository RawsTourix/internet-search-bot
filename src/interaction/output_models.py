"""Semantic output, delivery planning and receipt contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifacts.models import is_artifact_delivery_id, is_artifact_id
from ..ingress.models import ClientResponseRoute
from ..localization.models import LocalizationMessage
from .anchors import ClientResponseAnchor
from .capabilities import ClientCapabilitySnapshot
from .ids import is_interaction_id


class OutputBatchKind(str, Enum):
    STATUS = "status"
    PROGRESS = "progress"
    INTERMEDIATE = "intermediate"
    INTERACTIVE = "interactive"
    FINAL = "final"


class OutputBatchState(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    DELIVERING = "delivering"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class OutputPartReceiptState(str, Enum):
    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially_delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class OutputDeliveryReceiptState(str, Enum):
    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially_delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TransportOperationKind(str, Enum):
    TEXT = "text"
    DOCUMENT = "document"
    DOCUMENT_GROUP = "document_group"
    IMAGE = "image"
    AUDIO = "audio"
    VOICE = "voice"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    ANIMATION = "animation"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"
    STATUS = "status"
    UNSUPPORTED = "unsupported"


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputPartBase(_OutputModel):
    part_id: str
    index: int = Field(ge=0)
    required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("part_id")
    @classmethod
    def validate_part_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="opart"):
            raise ValueError("invalid output part ID")
        return value


class TextOutputPart(OutputPartBase):
    type: Literal["text_output"] = "text_output"
    text: str
    parse_mode: str | None = None


class ArtifactOutputPart(OutputPartBase):
    type: Literal["artifact_output"] = "artifact_output"
    artifact_id: str
    delivery_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not is_artifact_id(value):
            raise ValueError("invalid artifact_id")
        return value

    @field_validator("delivery_id")
    @classmethod
    def validate_delivery_id(cls, value: str) -> str:
        if not is_artifact_delivery_id(value):
            raise ValueError("invalid delivery_id")
        return value


class ImageOutputPart(ArtifactOutputPart):
    type: Literal["image_output"] = "image_output"
    caption: str | None = None


class AudioOutputPart(ArtifactOutputPart):
    type: Literal["audio_output"] = "audio_output"
    title: str | None = None
    performer: str | None = None


class VoiceOutputPart(ArtifactOutputPart):
    type: Literal["voice_output"] = "voice_output"


class VideoOutputPart(ArtifactOutputPart):
    type: Literal["video_output"] = "video_output"
    caption: str | None = None


class VideoNoteOutputPart(ArtifactOutputPart):
    type: Literal["video_note_output"] = "video_note_output"


class AnimationOutputPart(ArtifactOutputPart):
    type: Literal["animation_output"] = "animation_output"
    caption: str | None = None


class StickerOutputPart(ArtifactOutputPart):
    type: Literal["sticker_output"] = "sticker_output"
    emoji: str | None = None


class LocationOutputPart(OutputPartBase):
    type: Literal["location_output"] = "location_output"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    title: str | None = None
    description: str | None = None


class ContactOutputPart(OutputPartBase):
    type: Literal["contact_output"] = "contact_output"
    phone_number: str
    first_name: str
    last_name: str | None = None
    vcard: str | None = None


class StatusOutputPart(OutputPartBase):
    type: Literal["status_output"] = "status_output"
    message: LocalizationMessage
    severity: Literal["info", "warning", "error"] = "info"


OutputPart = Annotated[
    TextOutputPart
    | ArtifactOutputPart
    | ImageOutputPart
    | AudioOutputPart
    | VoiceOutputPart
    | VideoOutputPart
    | VideoNoteOutputPart
    | AnimationOutputPart
    | StickerOutputPart
    | LocationOutputPart
    | ContactOutputPart
    | StatusOutputPart,
    Field(discriminator="type"),
]


class OutputBatch(_OutputModel):
    """Immutable semantic manifest plus mutable lifecycle state."""

    schema_version: Literal[1] = 1
    output_batch_id: str
    session_id: str
    cycle_id: str
    sequence_number: int = Field(ge=1)
    kind: OutputBatchKind
    response_route: ClientResponseRoute
    response_anchor: ClientResponseAnchor | None = None
    locale: str
    capability_snapshot: ClientCapabilitySnapshot
    parts: tuple[OutputPart, ...]
    state: OutputBatchState = OutputBatchState.READY
    created_at: datetime
    ready_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("output_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="obat"):
            raise ValueError("invalid output_batch_id")
        return value

    @field_validator("session_id", "cycle_id", "locale")
    @classmethod
    def validate_required(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator("created_at", "ready_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("output timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_order(self) -> "OutputBatch":
        indices = [part.index for part in self.parts]
        if indices != list(range(len(indices))):
            raise ValueError("output part indices must be contiguous and monotonic")
        ids = [part.part_id for part in self.parts]
        if len(ids) != len(set(ids)):
            raise ValueError("output part IDs must be unique")
        if self.state == OutputBatchState.READY and self.ready_at is None:
            raise ValueError("ready output batch requires ready_at")
        if self.state in {
            OutputBatchState.DELIVERED,
            OutputBatchState.PARTIALLY_DELIVERED,
            OutputBatchState.FAILED,
            OutputBatchState.UNKNOWN,
            OutputBatchState.CANCELLED,
        } and self.completed_at is None:
            raise ValueError("terminal output state requires completed_at")
        return self


class OutputDeliveryGroup(_OutputModel):
    group_id: str
    index: int = Field(ge=0)
    operation_kind: TransportOperationKind
    part_ids: tuple[str, ...]
    required: bool = True
    rendered_text: str | None = None

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="odgrp"):
            raise ValueError("invalid output delivery group ID")
        return value

    @field_validator("part_ids")
    @classmethod
    def validate_part_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(
            not is_interaction_id(value, prefix="opart") for value in values
        ):
            raise ValueError("invalid output part IDs")
        return values


class OutputDeliveryPlan(_OutputModel):
    output_batch_id: str
    groups: tuple[OutputDeliveryGroup, ...]
    created_at: datetime

    @field_validator("output_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="obat"):
            raise ValueError("invalid output_batch_id")
        return value

    @model_validator(mode="after")
    def validate_group_order(self) -> "OutputDeliveryPlan":
        if [item.index for item in self.groups] != list(range(len(self.groups))):
            raise ValueError("delivery group indices must be contiguous")
        part_ids = [
            part_id for group in self.groups for part_id in group.part_ids
        ]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("an output part cannot belong to multiple groups")
        return self


class OutputDeliveryAttempt(_OutputModel):
    output_batch_id: str
    attempt_id: str
    plan: OutputDeliveryPlan
    state: Literal["claimed", "delivering", "completed", "failed", "unknown"]
    claimed_at: datetime
    updated_at: datetime

    @field_validator("output_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="obat"):
            raise ValueError("invalid output_batch_id")
        return value

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="odat"):
            raise ValueError("invalid output attempt ID")
        return value


class OutputPartReceipt(_OutputModel):
    part_id: str
    index: int = Field(ge=0)
    state: OutputPartReceiptState
    required: bool = True
    delivery_id: str | None = None
    client_message_ids: tuple[str, ...] = ()
    error_category: str | None = None
    delivered_at: datetime | None = None

    @field_validator("part_id")
    @classmethod
    def validate_part_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="opart"):
            raise ValueError("invalid output part ID")
        return value

    @field_validator("delivered_at")
    @classmethod
    def normalize_delivered_at(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_outcome(self) -> "OutputPartReceipt":
        confirmed_delivery = self.state in {
            OutputPartReceiptState.DELIVERED,
            OutputPartReceiptState.PARTIALLY_DELIVERED,
        }
        if confirmed_delivery:
            if self.delivered_at is None:
                raise ValueError("delivered part requires delivered_at")
            if not self.client_message_ids:
                raise ValueError(
                    "delivered part requires exact client message IDs"
                )
        elif self.delivered_at is not None:
            raise ValueError(
                "non-delivered part cannot have delivered_at"
            )
        if (
            self.state
            in {
                OutputPartReceiptState.PARTIALLY_DELIVERED,
                OutputPartReceiptState.FAILED,
                OutputPartReceiptState.UNKNOWN,
                OutputPartReceiptState.SKIPPED,
            }
            and not self.error_category
        ):
            raise ValueError(
                "non-delivered or partial part requires an error category"
            )
        return self


class OutputDeliveryReceipt(_OutputModel):
    output_batch_id: str
    attempt_id: str
    state: OutputDeliveryReceiptState
    part_receipts: tuple[OutputPartReceipt, ...]
    started_at: datetime
    completed_at: datetime

    @field_validator("output_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="obat"):
            raise ValueError("invalid output_batch_id")
        return value

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="odat"):
            raise ValueError("invalid output attempt ID")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_receipt(self) -> "OutputDeliveryReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("receipt completion precedes delivery start")
        part_ids = [item.part_id for item in self.part_receipts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("duplicate output part receipt")
        if not self.part_receipts:
            raise ValueError("delivery receipt requires output part receipts")

        all_receipts = list(self.part_receipts)
        required = [item for item in all_receipts if item.required] or all_receipts
        required_states = [item.state for item in required]
        all_states = [item.state for item in all_receipts]

        if self.state == OutputDeliveryReceiptState.UNKNOWN:
            if OutputPartReceiptState.UNKNOWN not in all_states:
                raise ValueError("unknown aggregate requires an unknown part")
            return self

        if OutputPartReceiptState.UNKNOWN in all_states:
            raise ValueError("confirmed aggregate cannot hide an unknown part")

        if self.state == OutputDeliveryReceiptState.DELIVERED:
            if any(
                state != OutputPartReceiptState.DELIVERED
                for state in required_states
            ):
                raise ValueError(
                    "delivered aggregate requires every required part delivered"
                )
            return self

        required_has_delivery = any(
            state
            in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
            }
            for state in required_states
        )
        required_all_delivered = all(
            state == OutputPartReceiptState.DELIVERED
            for state in required_states
        )

        if self.state == OutputDeliveryReceiptState.PARTIALLY_DELIVERED:
            if not required_has_delivery or required_all_delivered:
                raise ValueError(
                    "partial aggregate requires confirmed incomplete required output"
                )
            return self

        if self.state == OutputDeliveryReceiptState.FAILED and required_has_delivery:
            raise ValueError(
                "failed aggregate cannot hide delivered required output"
            )
        return self
