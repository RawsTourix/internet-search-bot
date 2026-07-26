"""Authoritative Telegram execution of a committed OutputDeliveryPlan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from tempfile import SpooledTemporaryFile
from typing import Any, Mapping

from telegram import InputMediaDocument
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut

from ...interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    AudioOutputPart,
    ContactOutputPart,
    ImageOutputPart,
    LocationOutputPart,
    OutputBatch,
    OutputDeliveryGroup,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPart,
    OutputPartReceipt,
    OutputPartReceiptState,
    TextOutputPart,
    TransportOperationKind,
)
from ...utils.telegram_formatting import (
    markdown_to_plain_text,
    markdown_to_telegram_html,
    split_markdown_for_telegram,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TelegramExecutionContext:
    bot: Any
    gateway: Any
    session_id: str
    chat_id: int
    message_thread_id: int | None = None
    reply_to_message_id: int | None = None
    status_message_id: int | None = None


class TelegramOutputPlanExecutor:
    """Execute groups by plan index and emit an exact receipt for every part."""

    async def execute(
        self,
        *,
        batch: OutputBatch,
        plan: OutputDeliveryPlan,
        attempt_id: str,
        context: TelegramExecutionContext,
    ) -> OutputDeliveryReceipt:
        if plan.output_batch_id != batch.output_batch_id:
            raise ValueError("delivery plan belongs to another OutputBatch")
        parts = {item.part_id: item for item in batch.parts}
        planned_ids = tuple(
            part_id
            for group in sorted(plan.groups, key=lambda item: item.index)
            for part_id in group.part_ids
        )
        expected_ids = tuple(item.part_id for item in batch.parts)
        if planned_ids != expected_ids:
            raise ValueError(
                "delivery plan must cover committed output parts exactly in order"
            )
        for group in plan.groups:
            group_parts = [parts[part_id] for part_id in group.part_ids]
            if group.required != any(part.required for part in group_parts):
                raise ValueError(
                    "delivery group required flag does not match committed parts"
                )

        started_at = _utc_now()
        outcomes: dict[str, OutputPartReceipt] = {}
        reply_available = context.reply_to_message_id
        limits = batch.capability_snapshot.limits

        for group in sorted(plan.groups, key=lambda item: item.index):
            group_parts = [parts[part_id] for part_id in group.part_ids]
            receipts = await self._execute_group(
                group=group,
                parts=group_parts,
                context=context,
                reply_to_message_id=reply_available,
                limits=limits,
            )
            if reply_available is not None and self._transport_attempted(receipts):
                reply_available = None
            for receipt in receipts:
                if receipt.part_id in outcomes:
                    raise ValueError("duplicate transport outcome for output part")
                outcomes[receipt.part_id] = receipt

        ordered: list[OutputPartReceipt] = []
        for part in batch.parts:
            outcome = outcomes.get(part.part_id)
            if outcome is None:
                outcome = self._receipt(
                    part,
                    state=OutputPartReceiptState.FAILED,
                    error_category="missing_transport_outcome",
                )
            ordered.append(outcome)
        return OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=self._aggregate(tuple(ordered)),
            part_receipts=tuple(ordered),
            started_at=started_at,
            completed_at=_utc_now(),
        )

    async def _execute_group(
        self,
        *,
        group: OutputDeliveryGroup,
        parts: list[OutputPart],
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
        limits: Mapping[str, Any],
    ) -> list[OutputPartReceipt]:
        kind = group.operation_kind
        if kind == TransportOperationKind.UNSUPPORTED:
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.FAILED,
                    error_category="unsupported_transport_operation",
                )
                for part in parts
            ]
        if kind in {TransportOperationKind.TEXT, TransportOperationKind.STATUS}:
            return [
                await self._send_text(
                    part=parts[0],
                    text=group.rendered_text or "",
                    status=(kind == TransportOperationKind.STATUS),
                    context=context,
                    reply_to_message_id=reply_to_message_id,
                    limits=limits,
                )
            ]
        if kind == TransportOperationKind.DOCUMENT_GROUP and len(parts) == 1:
            kind = TransportOperationKind.DOCUMENT
        if kind == TransportOperationKind.DOCUMENT_GROUP:
            return await self._send_document_group(
                parts=parts,
                context=context,
                reply_to_message_id=reply_to_message_id,
            )
        if kind == TransportOperationKind.LOCATION:
            return [
                await self._send_location(
                    parts[0],
                    context=context,
                    reply_to_message_id=reply_to_message_id,
                )
            ]
        if kind == TransportOperationKind.CONTACT:
            return [
                await self._send_contact(
                    parts[0],
                    context=context,
                    reply_to_message_id=reply_to_message_id,
                )
            ]
        return [
            await self._send_artifact(
                part=parts[0],
                operation=kind,
                context=context,
                reply_to_message_id=reply_to_message_id,
                limits=limits,
            )
        ]

    async def _send_text(
        self,
        *,
        part: OutputPart,
        text: str,
        status: bool,
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
        limits: Mapping[str, Any],
    ) -> OutputPartReceipt:
        artifact_fallback = (
            ArtifactContentReceiptState.NOT_DELIVERED
            if isinstance(part, ArtifactOutputPart)
            else None
        )
        if not text:
            return self._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                artifact_content_state=artifact_fallback,
                error_category="empty_rendered_text",
            )
        limit_key = (
            "transport.telegram.presentation.edit.max_chars"
            if status
            else "transport.telegram.output.text.max_chars"
        )
        limit = self._limit(limits, limit_key, default=4096, ceiling=4096)
        markdown = (
            isinstance(part, TextOutputPart)
            and (part.parse_mode or "").strip().lower() in {"markdown", "md"}
        )
        chunks = (
            tuple(split_markdown_for_telegram(text, limit=min(limit, 3000)))
            if markdown
            else self._plain_chunks(text, limit)
        )

        message_ids: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                rendered = markdown_to_telegram_html(chunk) if markdown else chunk
                try:
                    sent = await self._send_text_chunk(
                        context=context,
                        status=status,
                        chunk=rendered,
                        parse_mode=ParseMode.HTML if markdown else None,
                        chunk_index=index,
                        reply_to_message_id=reply_to_message_id,
                    )
                except BadRequest as error:
                    if self._is_message_not_modified(error, status=status, context=context):
                        sent = None
                    elif not markdown:
                        raise
                    else:
                        try:
                            sent = await self._send_text_chunk(
                                context=context,
                                status=status,
                                chunk=markdown_to_plain_text(chunk),
                                parse_mode=None,
                                chunk_index=index,
                                reply_to_message_id=reply_to_message_id,
                            )
                        except BadRequest as fallback_error:
                            if self._is_message_not_modified(
                                fallback_error,
                                status=status,
                                context=context,
                            ):
                                sent = None
                            else:
                                raise
                message_id = getattr(sent, "message_id", None) or (
                    context.status_message_id if status and index == 0 else None
                )
                if message_id is None:
                    return self._receipt(
                        part,
                        state=OutputPartReceiptState.UNKNOWN,
                        artifact_content_state=artifact_fallback,
                        client_message_ids=tuple(message_ids),
                        error_category="telegram_text_receipt_missing",
                    )
                message_ids.append(str(message_id))
        except BadRequest as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.PARTIALLY_DELIVERED
                    if message_ids
                    else OutputPartReceiptState.FAILED
                ),
                artifact_content_state=artifact_fallback,
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_bad_request:{type(error).__name__}",
            )
        except (TimedOut, NetworkError) as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.UNKNOWN,
                artifact_content_state=artifact_fallback,
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_transport_unknown:{type(error).__name__}",
            )
        except Exception as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.UNKNOWN
                    if message_ids
                    else OutputPartReceiptState.FAILED
                ),
                artifact_content_state=artifact_fallback,
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_text_error:{type(error).__name__}",
            )
        return self._receipt(
            part,
            state=OutputPartReceiptState.DELIVERED,
            artifact_content_state=artifact_fallback,
            client_message_ids=tuple(message_ids),
        )

    async def _send_text_chunk(
        self,
        *,
        context: TelegramExecutionContext,
        status: bool,
        chunk: str,
        parse_mode: str | None,
        chunk_index: int,
        reply_to_message_id: int | None,
    ) -> Any:
        if status and context.status_message_id is not None and chunk_index == 0:
            return await context.bot.edit_message_text(
                chat_id=context.chat_id,
                message_id=context.status_message_id,
                text=chunk,
                parse_mode=parse_mode,
            )
        return await context.bot.send_message(
            **self._message_kwargs(
                context,
                reply_to_message_id=(
                    reply_to_message_id if chunk_index == 0 else None
                ),
                text=chunk,
                parse_mode=parse_mode,
            )
        )

    async def _send_document_group(
        self,
        *,
        parts: list[OutputPart],
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
    ) -> list[OutputPartReceipt]:
        if not 2 <= len(parts) <= 10:
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.FAILED,
                    error_category="invalid_document_group_size",
                )
                for part in parts
            ]
        opened: list[tuple[SpooledTemporaryFile, str]] = []
        send_started = False
        try:
            for part in parts:
                if not isinstance(part, ArtifactOutputPart):
                    raise ValueError("document group contains a non-artifact part")
                opened.append(
                    await context.gateway.open_delivery_file(
                        part.delivery_id,
                        session_id=context.session_id,
                    )
                )
            media = [
                InputMediaDocument(
                    media=context.gateway.telegram_input_file(spool, filename)
                )
                for spool, filename in opened
            ]
            send_started = True
            sent = list(await context.bot.send_media_group(**self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                media=media,
            )))
            if len(sent) != len(parts):
                return [
                    self._receipt(
                        part,
                        state=OutputPartReceiptState.UNKNOWN,
                        artifact_content_state=ArtifactContentReceiptState.UNKNOWN,
                        client_message_ids=(
                            (str(sent[index].message_id),)
                            if index < len(sent)
                            and getattr(sent[index], "message_id", None) is not None
                            else ()
                        ),
                        error_category="telegram_media_group_receipt_mismatch",
                    )
                    for index, part in enumerate(parts)
                ]
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.DELIVERED,
                    artifact_content_state=ArtifactContentReceiptState.DELIVERED,
                    client_message_ids=(str(message.message_id),),
                )
                for part, message in zip(parts, sent, strict=True)
            ]
        except BadRequest as error:
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.FAILED,
                    artifact_content_state=ArtifactContentReceiptState.NOT_DELIVERED,
                    error_category=f"telegram_bad_request:{type(error).__name__}",
                )
                for part in parts
            ]
        except Exception as error:
            state = (
                OutputPartReceiptState.UNKNOWN
                if send_started
                else OutputPartReceiptState.FAILED
            )
            content_state = (
                ArtifactContentReceiptState.UNKNOWN
                if send_started
                else ArtifactContentReceiptState.NOT_DELIVERED
            )
            return [
                self._receipt(
                    part,
                    state=state,
                    artifact_content_state=content_state,
                    error_category=f"telegram_group_error:{type(error).__name__}",
                )
                for part in parts
            ]
        finally:
            for spool, _ in opened:
                spool.close()

    async def _send_artifact(
        self,
        *,
        part: OutputPart,
        operation: TransportOperationKind,
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
        limits: Mapping[str, Any],
    ) -> OutputPartReceipt:
        if not isinstance(part, ArtifactOutputPart):
            return self._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                error_category="artifact_operation_without_artifact",
            )
        opened: tuple[SpooledTemporaryFile, str] | None = None
        send_started = False
        message_ids: list[str] = []
        try:
            opened = await context.gateway.open_delivery_file(
                part.delivery_id,
                session_id=context.session_id,
            )
            spool, filename = opened
            payload = context.gateway.telegram_input_file(spool, filename)
            method_name, argument = {
                TransportOperationKind.DOCUMENT: ("send_document", "document"),
                TransportOperationKind.IMAGE: ("send_photo", "photo"),
                TransportOperationKind.AUDIO: ("send_audio", "audio"),
                TransportOperationKind.VOICE: ("send_voice", "voice"),
                TransportOperationKind.VIDEO: ("send_video", "video"),
                TransportOperationKind.VIDEO_NOTE: ("send_video_note", "video_note"),
                TransportOperationKind.ANIMATION: ("send_animation", "animation"),
                TransportOperationKind.STICKER: ("send_sticker", "sticker"),
            }[operation]
            kwargs = self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                **{argument: payload},
            )
            caption = getattr(part, "caption", None)
            caption_limit = self._limit(
                limits,
                "transport.telegram.output.caption.max_chars",
                default=1024,
                ceiling=1024,
            )
            overflow_caption = (
                caption if caption and len(caption) > caption_limit else None
            )
            if caption and overflow_caption is None:
                kwargs["caption"] = caption
            if isinstance(part, AudioOutputPart):
                kwargs["title"] = part.title
                kwargs["performer"] = part.performer

            send_started = True
            sent = await getattr(context.bot, method_name)(**kwargs)
            message_id = getattr(sent, "message_id", None)
            if message_id is None:
                return self._receipt(
                    part,
                    state=OutputPartReceiptState.UNKNOWN,
                    artifact_content_state=ArtifactContentReceiptState.UNKNOWN,
                    error_category="telegram_artifact_receipt_missing",
                )
            message_ids.append(str(message_id))
            if overflow_caption:
                caption_outcome = await self._send_caption_overflow(
                    caption=overflow_caption,
                    media_message_id=int(message_id),
                    context=context,
                    limits=limits,
                )
                message_ids.extend(caption_outcome[1])
                if caption_outcome[0] is not None:
                    return self._receipt(
                        part,
                        state=caption_outcome[0],
                        artifact_content_state=ArtifactContentReceiptState.DELIVERED,
                        client_message_ids=tuple(message_ids),
                        error_category=caption_outcome[2],
                    )
            return self._receipt(
                part,
                state=OutputPartReceiptState.DELIVERED,
                artifact_content_state=ArtifactContentReceiptState.DELIVERED,
                client_message_ids=tuple(message_ids),
            )
        except BadRequest as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.PARTIALLY_DELIVERED
                    if message_ids
                    else OutputPartReceiptState.FAILED
                ),
                artifact_content_state=(
                    ArtifactContentReceiptState.DELIVERED
                    if message_ids
                    else ArtifactContentReceiptState.NOT_DELIVERED
                ),
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_bad_request:{type(error).__name__}",
            )
        except (TimedOut, NetworkError) as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.UNKNOWN
                    if send_started
                    else OutputPartReceiptState.FAILED
                ),
                artifact_content_state=(
                    ArtifactContentReceiptState.UNKNOWN
                    if send_started
                    else ArtifactContentReceiptState.NOT_DELIVERED
                ),
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_artifact_error:{type(error).__name__}",
            )
        except Exception as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.UNKNOWN
                    if send_started
                    else OutputPartReceiptState.FAILED
                ),
                artifact_content_state=(
                    ArtifactContentReceiptState.UNKNOWN
                    if send_started
                    else ArtifactContentReceiptState.NOT_DELIVERED
                ),
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_artifact_error:{type(error).__name__}",
            )
        finally:
            if opened is not None:
                opened[0].close()

    async def _send_caption_overflow(
        self,
        *,
        caption: str,
        media_message_id: int,
        context: TelegramExecutionContext,
        limits: Mapping[str, Any],
    ) -> tuple[OutputPartReceiptState | None, list[str], str | None]:
        limit = self._limit(
            limits,
            "transport.telegram.output.text.max_chars",
            default=4096,
            ceiling=4096,
        )
        message_ids: list[str] = []
        try:
            for index, chunk in enumerate(self._plain_chunks(caption, limit)):
                sent = await context.bot.send_message(**self._message_kwargs(
                    context,
                    reply_to_message_id=(media_message_id if index == 0 else None),
                    text=chunk,
                    parse_mode=None,
                ))
                message_id = getattr(sent, "message_id", None)
                if message_id is None:
                    return (
                        OutputPartReceiptState.UNKNOWN,
                        message_ids,
                        "telegram_caption_receipt_missing",
                    )
                message_ids.append(str(message_id))
        except BadRequest as error:
            return (
                OutputPartReceiptState.PARTIALLY_DELIVERED,
                message_ids,
                f"telegram_caption_bad_request:{type(error).__name__}",
            )
        except Exception as error:
            return (
                OutputPartReceiptState.UNKNOWN,
                message_ids,
                f"telegram_caption_unknown:{type(error).__name__}",
            )
        return None, message_ids, None

    async def _send_location(
        self,
        part: OutputPart,
        *,
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
    ) -> OutputPartReceipt:
        if not isinstance(part, LocationOutputPart):
            return self._type_failure(part)
        return await self._native_structured(
            part,
            method=context.bot.send_location,
            kwargs=self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                latitude=part.latitude,
                longitude=part.longitude,
            ),
        )

    async def _send_contact(
        self,
        part: OutputPart,
        *,
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
    ) -> OutputPartReceipt:
        if not isinstance(part, ContactOutputPart):
            return self._type_failure(part)
        return await self._native_structured(
            part,
            method=context.bot.send_contact,
            kwargs=self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                phone_number=part.phone_number,
                first_name=part.first_name,
                last_name=part.last_name,
                vcard=part.vcard,
            ),
        )

    async def _native_structured(
        self,
        part: OutputPart,
        *,
        method: Any,
        kwargs: dict[str, Any],
    ) -> OutputPartReceipt:
        try:
            sent = await method(**kwargs)
            message_id = getattr(sent, "message_id", None)
            if message_id is None:
                return self._receipt(
                    part,
                    state=OutputPartReceiptState.UNKNOWN,
                    error_category="telegram_structured_receipt_missing",
                )
            return self._receipt(
                part,
                state=OutputPartReceiptState.DELIVERED,
                client_message_ids=(str(message_id),),
            )
        except BadRequest as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                error_category=f"telegram_bad_request:{type(error).__name__}",
            )
        except Exception as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.UNKNOWN,
                error_category=f"telegram_structured_error:{type(error).__name__}",
            )

    @staticmethod
    def _limit(
        limits: Mapping[str, Any],
        key: str,
        *,
        default: int,
        ceiling: int,
    ) -> int:
        value = limits.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid client capability limit: {key}")
        return min(value, ceiling)

    @staticmethod
    def _plain_chunks(text: str, limit: int) -> tuple[str, ...]:
        return tuple(
            text[offset : offset + limit]
            for offset in range(0, len(text), limit)
        )

    @staticmethod
    def _message_kwargs(
        context: TelegramExecutionContext,
        *,
        reply_to_message_id: int | None,
        **values: Any,
    ) -> dict[str, Any]:
        result = {"chat_id": context.chat_id, **values}
        if context.message_thread_id is not None:
            result["message_thread_id"] = context.message_thread_id
        if reply_to_message_id is not None:
            result["reply_to_message_id"] = reply_to_message_id
            result["allow_sending_without_reply"] = True
        return {key: value for key, value in result.items() if value is not None}

    @staticmethod
    def _type_failure(part: OutputPart) -> OutputPartReceipt:
        return TelegramOutputPlanExecutor._receipt(
            part,
            state=OutputPartReceiptState.FAILED,
            error_category="delivery_plan_part_type_mismatch",
        )

    @staticmethod
    def _is_message_not_modified(
        error: BadRequest,
        *,
        status: bool,
        context: TelegramExecutionContext,
    ) -> bool:
        return (
            status
            and context.status_message_id is not None
            and "message is not modified" in str(error).lower()
        )

    @staticmethod
    def _transport_attempted(receipts: list[OutputPartReceipt]) -> bool:
        for receipt in receipts:
            if receipt.state in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
                OutputPartReceiptState.UNKNOWN,
            }:
                return True
            if (receipt.error_category or "").startswith("telegram_bad_request"):
                return True
        return False

    @staticmethod
    def _receipt(
        part: OutputPart,
        *,
        state: OutputPartReceiptState,
        artifact_content_state: ArtifactContentReceiptState | None = None,
        client_message_ids: tuple[str, ...] = (),
        error_category: str | None = None,
    ) -> OutputPartReceipt:
        if isinstance(part, ArtifactOutputPart) and artifact_content_state is None:
            if state == OutputPartReceiptState.DELIVERED:
                artifact_content_state = ArtifactContentReceiptState.DELIVERED
            elif state in {
                OutputPartReceiptState.FAILED,
                OutputPartReceiptState.SKIPPED,
            }:
                artifact_content_state = ArtifactContentReceiptState.NOT_DELIVERED
            else:
                artifact_content_state = ArtifactContentReceiptState.UNKNOWN
        return OutputPartReceipt(
            part_id=part.part_id,
            index=part.index,
            state=state,
            required=part.required,
            delivery_id=getattr(part, "delivery_id", None),
            artifact_content_state=artifact_content_state,
            client_message_ids=client_message_ids,
            error_category=error_category,
            delivered_at=(
                _utc_now()
                if state in {
                    OutputPartReceiptState.DELIVERED,
                    OutputPartReceiptState.PARTIALLY_DELIVERED,
                }
                else None
            ),
        )

    @staticmethod
    def _aggregate(
        receipts: tuple[OutputPartReceipt, ...],
    ) -> OutputDeliveryReceiptState:
        all_receipts = list(receipts)
        if any(
            item.state == OutputPartReceiptState.UNKNOWN
            for item in all_receipts
        ):
            return OutputDeliveryReceiptState.UNKNOWN
        required = [item for item in all_receipts if item.required] or all_receipts
        if required and all(
            item.state == OutputPartReceiptState.DELIVERED
            for item in required
        ):
            return OutputDeliveryReceiptState.DELIVERED
        if any(
            item.state in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
            }
            for item in required
        ):
            return OutputDeliveryReceiptState.PARTIALLY_DELIVERED
        return OutputDeliveryReceiptState.FAILED
