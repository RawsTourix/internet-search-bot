"""Transport-independent semantic input parts and bounded artifact manifests."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..artifacts.models import is_artifact_id, is_artifact_lineage_id
from ..storage.models import is_content_id


class _PartModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextInputPart(_PartModel):
    type: Literal["text_input"] = "text_input"
    part_id: str
    text: str
    role: Literal["message_text", "caption"] = "message_text"
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BinaryInputPart(_PartModel):
    part_id: str
    slot_id: str | None = None
    artifact_id: str | None = None
    content_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str | None) -> str | None:
        if value is not None and not is_artifact_id(value):
            raise ValueError("invalid artifact_id")
        return value

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, value: str | None) -> str | None:
        if value is not None and not is_content_id(value):
            raise ValueError("invalid content_id")
        return value


class ArtifactInputPart(BinaryInputPart):
    type: Literal["artifact_input"] = "artifact_input"


class ImageInputPart(BinaryInputPart):
    type: Literal["image_input"] = "image_input"


class AudioInputPart(BinaryInputPart):
    type: Literal["audio_input"] = "audio_input"


class VoiceInputPart(BinaryInputPart):
    type: Literal["voice_input"] = "voice_input"


class VideoInputPart(BinaryInputPart):
    type: Literal["video_input"] = "video_input"


class VideoNoteInputPart(BinaryInputPart):
    type: Literal["video_note_input"] = "video_note_input"


class AnimationInputPart(BinaryInputPart):
    type: Literal["animation_input"] = "animation_input"


class StickerInputPart(BinaryInputPart):
    type: Literal["sticker_input"] = "sticker_input"
    emoji: str | None = None
    set_name: str | None = None
    is_animated: bool = False
    is_video: bool = False


class LocationInputPart(_PartModel):
    type: Literal["location_input"] = "location_input"
    part_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    horizontal_accuracy_meters: float | None = Field(default=None, ge=0)
    live_period_seconds: int | None = Field(default=None, ge=0)
    heading: int | None = Field(default=None, ge=1, le=360)
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContactInputPart(_PartModel):
    type: Literal["contact_input"] = "contact_input"
    part_id: str
    phone_number: str
    first_name: str
    last_name: str | None = None
    user_id: str | None = None
    vcard: str | None = None
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PollInputPart(_PartModel):
    type: Literal["poll_input"] = "poll_input"
    part_id: str
    poll_id: str | None = None
    question: str
    options: tuple[str, ...]
    is_anonymous: bool = True
    allows_multiple_answers: bool = False
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ForwardedMessageInputPart(_PartModel):
    type: Literal["forwarded_message_input"] = "forwarded_message_input"
    part_id: str
    origin_type: str
    origin_name: str | None = None
    origin_id: str | None = None
    origin_message_id: str | None = None
    forwarded_at: str | None = None
    trusted: Literal[False] = False
    source_event_id: str | None = None
    source_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


InputPart = Annotated[
    TextInputPart
    | ArtifactInputPart
    | ImageInputPart
    | AudioInputPart
    | VoiceInputPart
    | VideoInputPart
    | VideoNoteInputPart
    | AnimationInputPart
    | StickerInputPart
    | LocationInputPart
    | ContactInputPart
    | PollInputPart
    | ForwardedMessageInputPart,
    Field(discriminator="type"),
]


class ArtifactManifestItem(_PartModel):
    artifact_id: str
    artifact_lineage_id: str
    version: int = Field(ge=1)
    filename: str
    format_id: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    purpose: Literal["input", "working", "deliverable"]
    capabilities: tuple[str, ...] = ()
    activation_reason: str | None = None
    activation_scope: str | None = None
    activation_source_operation_id: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not is_artifact_id(value):
            raise ValueError("invalid artifact_id")
        return value

    @field_validator("artifact_lineage_id")
    @classmethod
    def validate_lineage_id(cls, value: str) -> str:
        if not is_artifact_lineage_id(value):
            raise ValueError("invalid artifact_lineage_id")
        return value

    @field_validator(
        "activation_reason",
        "activation_scope",
        "activation_source_operation_id",
    )
    @classmethod
    def normalize_activation_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ArtifactInputManifest(_PartModel):
    items: tuple[ArtifactManifestItem, ...] = ()
    available_count: int = Field(ge=0)
    truncated: bool = False

    @field_validator("items")
    @classmethod
    def unique_exact_ids(
        cls, values: tuple[ArtifactManifestItem, ...]
    ) -> tuple[ArtifactManifestItem, ...]:
        ids = [item.artifact_id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact manifest IDs must be unique")
        return values


class ArtifactDeliverableProjection(_PartModel):
    created_deliverables: tuple[ArtifactManifestItem, ...] = ()
    selected_artifact_ids: tuple[str, ...] = ()
    unselected_artifact_ids: tuple[str, ...] = ()
    delivery_states: dict[str, str] = Field(default_factory=dict)
