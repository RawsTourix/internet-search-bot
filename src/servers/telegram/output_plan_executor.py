"""Authoritative Telegram execution of a committed OutputDeliveryPlan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from tempfile import SpooledTemporaryFile
from typing import Any

from telegram import InputMediaDocument
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut

from ...interaction.output_models import (
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
        started_at = _utc_now()
        parts = {item.part_id: item for item in batch.parts}
        outcomes: dict[str, OutputPartReceipt] = {}
        reply_available = context.reply_to_message_id

        for group in sorted(plan.groups, key=lambda item: item.index):
            group_parts = [parts[part_id] for part_id in group.part_ids]
            receipts = await self._execute_group(
                group=group,
                parts=group_parts,
                context=context,
                reply_to_message_id=reply_available,
            )
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
        aggregate = self._aggregate(tuple(ordered), batch)
        return OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=aggregate,
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
        if kind in {
            TransportOperationKind.TEXT,
            TransportOperationKind.STATUS,
        }:
            return [
                await self._send_text(
                    part=parts[0],
                    text=group.rendered_text or "",
                    status=(kind == TransportOperationKind.STATUS),
                    context=context,
                    reply_to_message_id=reply_to_message_id,
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
    ) -> OutputPartReceipt:
        if not text:
            return self._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                error_category="empty_rendered_text",
            )
        limit = max(
            1,
            int(
                getattr(part, "metadata", {}).get(
                    "transport_text_max_chars",
                    4096,
                )
            ),
        )
        markdown = (
            isinstance(part, TextOutputPart)
            and (part.parse_mode or "").strip().lower() in {"markdown", "md"}
        )
        if markdown:
            safe_limit = min(limit, 3000)
            logical_chunks = split_markdown_for_telegram(
                text,
                limit=safe_limit,
            )
            chunks = tuple(
                subchunk
                for chunk in logical_chunks
                for subchunk in (
                    tuple(
                        chunk[offset : offset + safe_limit]
                        for offset in range(0, len(chunk), safe_limit)
                    )
                    or ("",)
                )
            )
        else:
            chunks = tuple(
                text[offset : offset + limit]
                for offset in range(0, len(text), limit)
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
                except BadRequest:
                    if not markdown:
                        raise
                    sent = await self._send_text_chunk(
                        context=context,
                        status=status,
                        chunk=markdown_to_plain_text(chunk),
                        parse_mode=None,
                        chunk_index=index,
                        reply_to_message_id=reply_to_message_id,
                    )
                message_id = (
                    getattr(sent, "message_id", None)
                    or (
                        context.status_message_id
                        if status and index == 0
                        else None
                    )
                )
                if message_id is None:
                    return self._receipt(
                        part,
                        state=OutputPartReceiptState.UNKNOWN,
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
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_bad_request:{type(error).__name__}",
            )
        except (TimedOut, NetworkError) as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.UNKNOWN,
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
                client_message_ids=tuple(message_ids),
                error_category=f"telegram_text_error:{type(error).__name__}",
            )
        return self._receipt(
            part,
            state=OutputPartReceiptState.DELIVERED,
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
            kwargs = self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                media=media,
            )
            send_started = True
            sent = list(await context.bot.send_media_group(**kwargs))
            if len(sent) != len(parts):
                return [
                    self._receipt(
                        part,
                        state=OutputPartReceiptState.UNKNOWN,
                        error_category="telegram_media_group_receipt_mismatch",
                    )
                    for part in parts
                ]
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.DELIVERED,
                    client_message_ids=(str(message.message_id),),
                )
                for part, message in zip(parts, sent, strict=True)
            ]
        except BadRequest as error:
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.FAILED,
                    error_category=f"telegram_bad_request:{type(error).__name__}",
                )
                for part in parts
            ]
        except (TimedOut, NetworkError) as error:
            state = (
                OutputPartReceiptState.UNKNOWN
                if send_started
                else OutputPartReceiptState.FAILED
            )
            return [
                self._receipt(
                    part,
                    state=state,
                    error_category=f"telegram_group_error:{type(error).__name__}",
                )
                for part in parts
            ]
        except Exception as error:
            state = (
                OutputPartReceiptState.UNKNOWN
                if send_started
                else OutputPartReceiptState.FAILED
            )
            return [
                self._receipt(
                    part,
                    state=state,
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
    ) -> OutputPartReceipt:
        if not isinstance(part, ArtifactOutputPart):
            return self._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                error_category="artifact_operation_without_artifact",
            )
        opened: tuple[SpooledTemporaryFile, str] | None = None
        send_started = False
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
                TransportOperationKind.VIDEO_NOTE: (
                    "send_video_note",
                    "video_note",
                ),
                TransportOperationKind.ANIMATION: (
                    "send_animation",
                    "animation",
                ),
                TransportOperationKind.STICKER: ("send_sticker", "sticker"),
            }[operation]
            kwargs = self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                **{argument: payload},
            )
            if isinstance(part, ImageOutputPart):
                kwargs["caption"] = part.caption
            elif isinstance(part, AudioOutputPart):
                kwargs["title"] = part.title
                kwargs["performer"] = part.performer
            elif hasattr(part, "caption"):
                kwargs["caption"] = getattr(part, "caption")
            send_started = True
            sent = await getattr(context.bot, method_name)(**kwargs)
            message_id = getattr(sent, "message_id", None)
            if message_id is None:
                return self._receipt(
                    part,
                    state=OutputPartReceiptState.UNKNOWN,
                    error_category="telegram_artifact_receipt_missing",
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
        except (TimedOut, NetworkError) as error:
            return self._receipt(
                part,
                state=(
                    OutputPartReceiptState.UNKNOWN
                    if send_started
                    else OutputPartReceiptState.FAILED
                ),
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
                error_category=f"telegram_artifact_error:{type(error).__name__}",
            )
        finally:
            if opened is not None:
                opened[0].close()

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
        except (TimedOut, NetworkError) as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.UNKNOWN,
                error_category=f"telegram_structured_error:{type(error).__name__}",
            )
        except Exception as error:
            return self._receipt(
                part,
                state=OutputPartReceiptState.UNKNOWN,
                error_category=f"telegram_structured_error:{type(error).__name__}",
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
        return {
            key: value
            for key, value in result.items()
            if value is not None
        }

    @staticmethod
    def _type_failure(part: OutputPart) -> OutputPartReceipt:
        return TelegramOutputPlanExecutor._receipt(
            part,
            state=OutputPartReceiptState.FAILED,
            error_category="delivery_plan_part_type_mismatch",
        )

    @staticmethod
    def _receipt(
        part: OutputPart,
        *,
        state: OutputPartReceiptState,
        client_message_ids: tuple[str, ...] = (),
        error_category: str | None = None,
    ) -> OutputPartReceipt:
        return OutputPartReceipt(
            part_id=part.part_id,
            index=part.index,
            state=state,
            required=part.required,
            delivery_id=getattr(part, "delivery_id", None),
            client_message_ids=client_message_ids,
            error_category=error_category,
            delivered_at=(
                _utc_now()
                if state
                in {
                    OutputPartReceiptState.DELIVERED,
                    OutputPartReceiptState.PARTIALLY_DELIVERED,
                }
                else None
            ),
        )

    @staticmethod
    def _aggregate(
        receipts: tuple[OutputPartReceipt, ...],
        batch: OutputBatch,
    ) -> OutputDeliveryReceiptState:
        del batch
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
            item.state
            in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
            }
            for item in required
        ):
            return OutputDeliveryReceiptState.PARTIALLY_DELIVERED
        return OutputDeliveryReceiptState.FAILED
