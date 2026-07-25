"""Composable Telegram semantic input resolvers.

Resolvers only normalize exact transport data. They never perform OCR,
transcription, media understanding, conversion, or an LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..interaction.parts import (
    AnimationInputPart,
    ArtifactInputPart,
    AudioInputPart,
    ContactInputPart,
    ForwardedMessageInputPart,
    ImageInputPart,
    InputPart,
    LocationInputPart,
    PollInputPart,
    StickerInputPart,
    TextInputPart,
    VideoInputPart,
    VideoNoteInputPart,
    VoiceInputPart,
)


@dataclass(frozen=True)
class TelegramResolverContext:
    message: Any
    source_message_id: str
    attachment_slots: tuple[Any, ...] = ()


class ClientInputResolver(Protocol):
    def supports(self, event: TelegramResolverContext) -> bool: ...

    async def resolve(
        self, event: TelegramResolverContext
    ) -> list[InputPart]: ...


class TelegramTextResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return bool(
            (getattr(event.message, "text", None) or "").strip()
            or (getattr(event.message, "caption", None) or "").strip()
        )

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        result: list[InputPart] = []
        text = (getattr(event.message, "text", None) or "").strip()
        caption = (getattr(event.message, "caption", None) or "").strip()
        if text:
            result.append(TextInputPart(
                part_id=f"semantic-text-{event.source_message_id}",
                text=text,
                role="message_text",
                source_message_id=event.source_message_id,
            ))
        if caption:
            result.append(TextInputPart(
                part_id=f"semantic-caption-{event.source_message_id}",
                text=caption,
                role="caption",
                source_message_id=event.source_message_id,
            ))
        return result


class _TelegramBinaryResolver:
    attribute: str
    part_type: type

    def supports(self, event: TelegramResolverContext) -> bool:
        return bool(getattr(event.message, self.attribute, None))

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        native = getattr(event.message, self.attribute, None)
        if self.attribute == "photo":
            values = list(native or [])
            native = values[-1] if values else None
        if native is None:
            return []
        slot = next(
            (
                item
                for item in event.attachment_slots
                if item.media_kind
                in {self.attribute, self.attribute.replace("_", "-")}
                or (
                    self.attribute == "document"
                    and item.media_kind == "document"
                )
                or (
                    self.attribute == "photo"
                    and item.media_kind == "photo"
                )
            ),
            None,
        )
        return [self.part_type(
            part_id=f"semantic-{self.attribute}-{event.source_message_id}",
            slot_id=getattr(slot, "slot_id", None),
            filename=getattr(slot, "original_filename", None),
            mime_type=getattr(native, "mime_type", None)
            or getattr(slot, "declared_mime_type", None),
            size_bytes=getattr(native, "file_size", None),
            duration_seconds=getattr(native, "duration", None),
            width=getattr(native, "width", None),
            height=getattr(native, "height", None),
            source_message_id=event.source_message_id,
            metadata={
                "telegram_file_unique_id": getattr(
                    native, "file_unique_id", None
                )
            },
        )]


class TelegramDocumentResolver(_TelegramBinaryResolver):
    attribute = "document"
    part_type = ArtifactInputPart


class TelegramPhotoResolver(_TelegramBinaryResolver):
    attribute = "photo"
    part_type = ImageInputPart


class TelegramAudioResolver(_TelegramBinaryResolver):
    attribute = "audio"
    part_type = AudioInputPart


class TelegramVoiceResolver(_TelegramBinaryResolver):
    attribute = "voice"
    part_type = VoiceInputPart


class TelegramVideoResolver(_TelegramBinaryResolver):
    attribute = "video"
    part_type = VideoInputPart


class TelegramVideoNoteResolver(_TelegramBinaryResolver):
    attribute = "video_note"
    part_type = VideoNoteInputPart


class TelegramAnimationResolver(_TelegramBinaryResolver):
    attribute = "animation"
    part_type = AnimationInputPart


class TelegramStickerResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return getattr(event.message, "sticker", None) is not None

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        item = event.message.sticker
        return [StickerInputPart(
            part_id=f"semantic-sticker-{event.source_message_id}",
            filename=f"sticker-{event.source_message_id}",
            mime_type=(
                "video/webm"
                if getattr(item, "is_video", False)
                else "application/x-tgsticker"
                if getattr(item, "is_animated", False)
                else "image/webp"
            ),
            size_bytes=getattr(item, "file_size", None),
            width=getattr(item, "width", None),
            height=getattr(item, "height", None),
            emoji=getattr(item, "emoji", None),
            set_name=getattr(item, "set_name", None),
            is_animated=bool(getattr(item, "is_animated", False)),
            is_video=bool(getattr(item, "is_video", False)),
            source_message_id=event.source_message_id,
        )]


class TelegramLocationResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return getattr(event.message, "location", None) is not None

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        item = event.message.location
        return [LocationInputPart(
            part_id=f"semantic-location-{event.source_message_id}",
            latitude=item.latitude,
            longitude=item.longitude,
            horizontal_accuracy_meters=getattr(
                item, "horizontal_accuracy", None
            ),
            live_period_seconds=getattr(item, "live_period", None),
            heading=getattr(item, "heading", None),
            source_message_id=event.source_message_id,
        )]


class TelegramContactResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return getattr(event.message, "contact", None) is not None

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        item = event.message.contact
        return [ContactInputPart(
            part_id=f"semantic-contact-{event.source_message_id}",
            phone_number=item.phone_number,
            first_name=item.first_name,
            last_name=getattr(item, "last_name", None),
            user_id=(
                str(item.user_id)
                if getattr(item, "user_id", None) is not None
                else None
            ),
            vcard=getattr(item, "vcard", None),
            source_message_id=event.source_message_id,
        )]


class TelegramPollResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return getattr(event.message, "poll", None) is not None

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        item = event.message.poll
        return [PollInputPart(
            part_id=f"semantic-poll-{event.source_message_id}",
            poll_id=str(getattr(item, "id", "") or "") or None,
            question=item.question,
            options=tuple(option.text for option in item.options),
            is_anonymous=bool(getattr(item, "is_anonymous", True)),
            allows_multiple_answers=bool(
                getattr(item, "allows_multiple_answers", False)
            ),
            source_message_id=event.source_message_id,
        )]


class TelegramForwardResolver:
    def supports(self, event: TelegramResolverContext) -> bool:
        return any(
            getattr(event.message, name, None) is not None
            for name in (
                "forward_origin",
                "forward_from",
                "forward_from_chat",
                "forward_sender_name",
            )
        )

    async def resolve(self, event: TelegramResolverContext) -> list[InputPart]:
        origin = (
            getattr(event.message, "forward_origin", None)
            or getattr(event.message, "forward_from", None)
            or getattr(event.message, "forward_from_chat", None)
        )
        return [ForwardedMessageInputPart(
            part_id=f"semantic-forward-{event.source_message_id}",
            origin_type=type(origin).__name__ if origin is not None else "hidden",
            origin_name=(
                getattr(origin, "full_name", None)
                or getattr(origin, "title", None)
                or getattr(event.message, "forward_sender_name", None)
            ),
            origin_id=(
                str(origin.id)
                if getattr(origin, "id", None) is not None
                else None
            ),
            origin_message_id=(
                str(event.message.forward_from_message_id)
                if getattr(event.message, "forward_from_message_id", None)
                is not None
                else None
            ),
            forwarded_at=(
                event.message.forward_date.isoformat()
                if getattr(event.message, "forward_date", None) is not None
                else None
            ),
            trusted=False,
            source_message_id=event.source_message_id,
        )]


class TelegramInputResolverRegistry:
    def __init__(self, resolvers: tuple[ClientInputResolver, ...] | None = None):
        self.resolvers = resolvers or (
            TelegramTextResolver(),
            TelegramDocumentResolver(),
            TelegramPhotoResolver(),
            TelegramAudioResolver(),
            TelegramVoiceResolver(),
            TelegramVideoResolver(),
            TelegramVideoNoteResolver(),
            TelegramAnimationResolver(),
            TelegramStickerResolver(),
            TelegramLocationResolver(),
            TelegramContactResolver(),
            TelegramPollResolver(),
            TelegramForwardResolver(),
        )

    async def resolve(
        self,
        message: Any,
        *,
        attachment_slots: tuple[Any, ...] = (),
    ) -> list[InputPart]:
        context = TelegramResolverContext(
            message=message,
            source_message_id=str(message.message_id),
            attachment_slots=attachment_slots,
        )
        result: list[InputPart] = []
        for resolver in self.resolvers:
            if resolver.supports(context):
                result.extend(await resolver.resolve(context))
        return result
