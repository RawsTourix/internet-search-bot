"""Telegram plan executor with exact client-instance content authority."""

from __future__ import annotations

import logging
import re
from tempfile import SpooledTemporaryFile
from typing import Any, Mapping

from telegram import InputFile, InputMediaDocument
from telegram.error import BadRequest, NetworkError, TimedOut

from ...interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    OutputBatch,
    OutputDeliveryGroup,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
    OutputPart,
    OutputPartReceipt,
    OutputPartReceiptState,
    TransportOperationKind,
)
from .output_batch_gateway import TelegramClaimedOutputGateway
from .output_context import (
    build_preflight_failure_receipt,
    build_telegram_execution_context,
)
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


logger = logging.getLogger("TelegramServer.OutputExecutor")


class InstanceScopedTelegramOutputPlanExecutor(TelegramOutputPlanExecutor):
    """Execute one claimed immutable batch through exact durable authority."""

    async def execute(
        self,
        *,
        batch: OutputBatch,
        plan: OutputDeliveryPlan,
        attempt_id: str,
        context: TelegramExecutionContext,
    ) -> OutputDeliveryReceipt:
        gateway = context.gateway
        configured_instance = str(
            getattr(gateway, "client_instance_id", "") or ""
        ).strip()
        if (
            configured_instance
            and configured_instance
            != batch.capability_snapshot.client_instance_id
        ):
            raise ValueError(
                "Telegram executor gateway instance differs from OutputBatch"
            )
        if (
            not isinstance(gateway, TelegramClaimedOutputGateway)
            and all(
                hasattr(gateway, attribute)
                for attribute in ("gateway_url", "api_key")
            )
        ):
            gateway = TelegramClaimedOutputGateway.from_client(
                gateway,
                output_batch_id=batch.output_batch_id,
                client_instance_id=(
                    batch.capability_snapshot.client_instance_id
                ),
            )
        try:
            authoritative_context = build_telegram_execution_context(
                batch,
                bot=context.bot,
                gateway=gateway,
                status_message_id=context.status_message_id,
            )
        except (TypeError, ValueError) as error:
            receipt = build_preflight_failure_receipt(
                batch,
                attempt_id=attempt_id,
                error_category=(
                    "telegram_invalid_response_route:"
                    f"{type(error).__name__}"
                ),
            )
            self._log_receipt(batch, receipt)
            return receipt

        receipt = await super().execute(
            batch=batch,
            plan=plan,
            attempt_id=attempt_id,
            context=authoritative_context,
        )
        self._log_receipt(batch, receipt)
        return receipt

    async def _execute_group(
        self,
        *,
        group: OutputDeliveryGroup,
        parts: list[OutputPart],
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
        limits: Mapping[str, Any],
    ) -> list[OutputPartReceipt]:
        receipts = await super()._execute_group(
            group=group,
            parts=parts,
            context=context,
            reply_to_message_id=reply_to_message_id,
            limits=limits,
        )
        if not self._can_fallback_document_group(
            group=group,
            parts=parts,
            receipts=receipts,
        ):
            return receipts

        logger.warning(
            "telegram_document_group_fallback group_id=%s part_count=%s "
            "categories=%s",
            group.group_id,
            len(parts),
            [item.error_category for item in receipts],
        )
        fallback_receipts: list[OutputPartReceipt] = []
        reply_available = reply_to_message_id
        for part in parts:
            receipt = await self._send_artifact(
                part=part,
                operation=TransportOperationKind.DOCUMENT,
                context=context,
                reply_to_message_id=reply_available,
                limits=limits,
            )
            fallback_receipts.append(receipt)
            if (
                reply_available is not None
                and self._transport_attempted([receipt])
            ):
                reply_available = None

        logger.info(
            "telegram_document_group_fallback_finished group_id=%s "
            "part_states=%s",
            group.group_id,
            [item.state.value for item in fallback_receipts],
        )
        return fallback_receipts

    async def _send_document_group(
        self,
        *,
        parts: list[OutputPart],
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
    ) -> list[OutputPartReceipt]:
        """Try streaming handles, then one bounded eager representation."""

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
                    raise ValueError(
                        "document group contains a non-artifact part"
                    )
                opened.append(
                    await context.gateway.open_delivery_file(
                        part.delivery_id,
                        session_id=context.session_id,
                    )
                )

            media = [
                InputMediaDocument(
                    media=context.gateway.telegram_input_file(
                        spool,
                        filename,
                    )
                )
                for spool, filename in opened
            ]
            kwargs = self._message_kwargs(
                context,
                reply_to_message_id=reply_to_message_id,
                media=media,
            )
            try:
                send_started = True
                sent = list(await context.bot.send_media_group(**kwargs))
            except BadRequest as error:
                logger.warning(
                    "telegram_document_group_bad_request representation=stream "
                    "part_count=%s filenames=%s error=%s",
                    len(parts),
                    [filename for _, filename in opened],
                    self._safe_telegram_error(error),
                )
                if (
                    reply_to_message_id is not None
                    and self._is_reply_target_error(error)
                ):
                    retry_kwargs = dict(kwargs)
                    retry_kwargs.pop("reply_to_message_id", None)
                    retry_kwargs.pop("allow_sending_without_reply", None)
                    logger.info(
                        "telegram_document_group_retry_without_reply "
                        "representation=stream part_count=%s",
                        len(parts),
                    )
                    sent = list(
                        await context.bot.send_media_group(**retry_kwargs)
                    )
                elif self._can_eager_retry(parts, context):
                    eager_media = []
                    for spool, filename in opened:
                        spool.seek(0)
                        eager_media.append(
                            InputMediaDocument(
                                media=InputFile(
                                    spool.read(),
                                    filename=filename,
                                )
                            )
                        )
                    eager_kwargs = self._message_kwargs(
                        context,
                        reply_to_message_id=reply_to_message_id,
                        media=eager_media,
                    )
                    logger.info(
                        "telegram_document_group_retry_eager "
                        "part_count=%s total_bytes=%s",
                        len(parts),
                        sum(int(part.size_bytes or 0) for part in parts),
                    )
                    sent = list(
                        await context.bot.send_media_group(**eager_kwargs)
                    )
                else:
                    return self._group_failure_receipts(
                        parts,
                        error_category=(
                            "telegram_bad_request:"
                            f"{type(error).__name__}"
                        ),
                    )

            if len(sent) != len(parts):
                return [
                    self._receipt(
                        part,
                        state=OutputPartReceiptState.UNKNOWN,
                        artifact_content_state=(
                            ArtifactContentReceiptState.UNKNOWN
                        ),
                        client_message_ids=(
                            (str(sent[index].message_id),)
                            if index < len(sent)
                            and getattr(
                                sent[index], "message_id", None
                            ) is not None
                            else ()
                        ),
                        error_category=(
                            "telegram_media_group_receipt_mismatch"
                        ),
                    )
                    for index, part in enumerate(parts)
                ]
            return [
                self._receipt(
                    part,
                    state=OutputPartReceiptState.DELIVERED,
                    artifact_content_state=(
                        ArtifactContentReceiptState.DELIVERED
                    ),
                    client_message_ids=(str(message.message_id),),
                )
                for part, message in zip(parts, sent, strict=True)
            ]
        except BadRequest as error:
            logger.warning(
                "telegram_document_group_bad_request representation=retry "
                "part_count=%s filenames=%s error=%s",
                len(parts),
                [filename for _, filename in opened],
                self._safe_telegram_error(error),
            )
            return self._group_failure_receipts(
                parts,
                error_category=(
                    "telegram_bad_request:"
                    f"{type(error).__name__}"
                ),
            )
        except (TimedOut, NetworkError) as error:
            logger.warning(
                "telegram_document_group_transport_unknown part_count=%s "
                "error=%s",
                len(parts),
                self._safe_telegram_error(error),
            )
            return [
                self._receipt(
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
                    error_category=(
                        "telegram_group_transport:"
                        f"{type(error).__name__}"
                    ),
                )
                for part in parts
            ]
        except Exception as error:
            logger.exception(
                "telegram_document_group_error part_count=%s "
                "send_started=%s error=%s",
                len(parts),
                send_started,
                self._safe_telegram_error(error),
            )
            return [
                self._receipt(
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
                    error_category=(
                        "telegram_group_error:"
                        f"{type(error).__name__}"
                    ),
                )
                for part in parts
            ]
        finally:
            for spool, _ in opened:
                spool.close()

    @staticmethod
    def _can_eager_retry(
        parts: list[OutputPart],
        context: TelegramExecutionContext,
    ) -> bool:
        if not all(
            isinstance(part, ArtifactOutputPart)
            and part.size_bytes is not None
            for part in parts
        ):
            return False
        budget = int(
            getattr(
                context.gateway,
                "delivery_spool_memory_bytes",
                8 * 1024 * 1024,
            )
        )
        total = sum(int(part.size_bytes or 0) for part in parts)
        return budget > 0 and total <= budget

    @classmethod
    def _group_failure_receipts(
        cls,
        parts: list[OutputPart],
        *,
        error_category: str,
    ) -> list[OutputPartReceipt]:
        return [
            cls._receipt(
                part,
                state=OutputPartReceiptState.FAILED,
                artifact_content_state=(
                    ArtifactContentReceiptState.NOT_DELIVERED
                ),
                error_category=error_category,
            )
            for part in parts
        ]

    @staticmethod
    def _is_reply_target_error(error: BadRequest) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "message to be replied",
                "reply message",
                "replied message",
                "reply_to_message",
            )
        )

    @staticmethod
    def _safe_telegram_error(error: BaseException) -> str:
        message = re.sub(r"\s+", " ", str(error)).strip()
        value = type(error).__name__
        if message:
            value += f": {message}"
        return value[:1_000]

    @staticmethod
    def _can_fallback_document_group(
        *,
        group: OutputDeliveryGroup,
        parts: list[OutputPart],
        receipts: list[OutputPartReceipt],
    ) -> bool:
        """Retry individually only when the grouped attempt is known unsent.

        A confirmed Telegram ``BadRequest`` has no successful transport side
        effect, and any exception raised before ``send_media_group`` starts is
        represented by FAILED receipts. UNKNOWN/PARTIALLY_DELIVERED outcomes are
        never retried because they may already exist in the client.
        """

        if group.operation_kind != TransportOperationKind.DOCUMENT_GROUP:
            return False
        if not 2 <= len(parts) <= 10:
            return False
        if not all(isinstance(part, ArtifactOutputPart) for part in parts):
            return False
        if len(receipts) != len(parts) or not receipts:
            return False
        if any(
            receipt.state != OutputPartReceiptState.FAILED
            for receipt in receipts
        ):
            return False
        safe_prefixes = (
            "telegram_bad_request:",
            "telegram_group_error:",
        )
        return all(
            bool(receipt.error_category)
            and receipt.error_category.startswith(safe_prefixes)
            for receipt in receipts
        )

    @staticmethod
    def _log_receipt(
        batch: OutputBatch,
        receipt: OutputDeliveryReceipt,
    ) -> None:
        logger.info(
            "telegram_output_receipt output_batch_id=%s attempt_id=%s "
            "state=%s parts=%s",
            batch.output_batch_id,
            receipt.attempt_id,
            receipt.state.value,
            [
                {
                    "part_id": item.part_id,
                    "index": item.index,
                    "state": item.state.value,
                    "delivery_id": item.delivery_id,
                    "artifact_content_state": (
                        item.artifact_content_state.value
                        if item.artifact_content_state is not None
                        else None
                    ),
                    "error_category": item.error_category,
                    "client_message_ids": list(item.client_message_ids),
                }
                for item in receipt.part_receipts
            ],
        )
