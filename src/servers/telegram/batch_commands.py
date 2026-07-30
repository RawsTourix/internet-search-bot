"""Thin Telegram commands for the shared explicit InputBatch control plane."""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ...agent.progress_messages import progress_text
from . import telegram_server as server
from .config import TELEGRAM_BOT_INSTANCE_ID


_COLLECT_COMMAND = "/collect"
_SEND_COMMAND = "/send"
_CANCEL_COMMAND = "/cancel"


def _command(update: Update) -> str:
    text = str(getattr(update.effective_message, "text", "") or "")
    return text.split(maxsplit=1)[0].lower().split("@", 1)[0]


def _thread_id(update: Update):
    return getattr(update.effective_message, "message_thread_id", None)


def _idempotency_key(update: Update, action: str) -> str:
    update_id = getattr(update, "update_id", None)
    message_id = getattr(update.effective_message, "message_id", None)
    return (
        f"telegram:{TELEGRAM_BOT_INSTANCE_ID}:input-collection:"
        f"{action}:{update_id}:{message_id}"
    )


def _route(update: Update, status_message) -> dict[str, Any]:
    thread_id = _thread_id(update)
    return {
        "route_type": "telegram",
        "conversation_id": str(update.effective_chat.id),
        "thread_id": str(thread_id) if thread_id is not None else None,
        "reply_to_message_id": str(update.effective_message.message_id),
        "metadata": server._progress_metadata(update, status_message),
    }


def _counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "file_count": int(payload.get("file_count") or 0),
        "text_part_count": int(payload.get("text_part_count") or 0),
    }


async def _finalize_collection_snapshot(
    *,
    update: Update,
    payload: dict[str, Any],
    locale: str,
    message_key: str,
) -> None:
    """Terminalize the collection presentation without deleting its evidence."""

    value = payload.get("_telegram_previous_status_message_id")
    if value is None:
        return
    try:
        message_id = int(value)
    except (TypeError, ValueError):
        return

    await server.stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=message_id,
    )
    text = server._localized(
        message_key,
        locale=locale,
        **_counts(payload),
    )
    try:
        await server.edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=text,
            parse_mode=None,
        )
    except Exception as error:
        # The collection snapshot is historical presentation only. Failure to
        # terminalize it must not block the already-authoritative commit/cancel.
        server.logger.warning(
            "telegram_collection_snapshot_terminalize_failed "
            "message_id=%s error_type=%s",
            message_id,
            type(error).__name__,
        )
        return
    server.logger.info(
        "telegram_collection_snapshot_terminalized message_id=%s state=%s",
        message_id,
        message_key,
    )


async def _finalize_delivered_run_status(
    *,
    update: Update,
    status_message,
    metadata: dict[str, Any],
    locale: str,
) -> None:
    """Replace `result_ready` only after transport confirmed delivery."""

    output_batch = dict(metadata.get("output_batch") or {})
    output_batch_id = str(output_batch.get("output_batch_id") or "").strip()
    take_state = getattr(
        server.artifact_gateway,
        "take_completed_output_state",
        None,
    )
    if not output_batch_id or not callable(take_state):
        return
    terminal_state = await take_state(output_batch_id)
    if terminal_state != "delivered":
        return
    await server.finish_status_or_send_reply(
        update=update,
        status_message=status_message,
        text=progress_text("cycle_done", locale),
        delivery_mode="edit_status",
    )


async def _handle_input_collection_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    del context
    command = _command(update)
    locale = server.detect_progress_locale(update)
    session_id = server._session_for_update(update)
    status_message = await server.send_initial_status_message(
        update,
        server._localized("input.command_received", locale=locale),
    )
    common = {
        "session_id": session_id,
        "chat_id": update.effective_chat.id,
        "thread_id": _thread_id(update),
        "principal_id": update.effective_user.id,
    }

    try:
        if command == _COLLECT_COMMAND:
            payload = await server.artifact_gateway.start_collection(
                **common,
                idempotency_key=_idempotency_key(update, "start"),
                locale=locale,
                response_route=_route(update, status_message),
            )
            key = {
                "started": "input_collection.started",
                "promoted_auto_draft": "input_collection.promoted",
                "already_active": "input_collection.already_active",
            }.get(str(payload.get("status")), "input_collection.collecting")
            await server.finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=server._localized(key, locale=locale, **_counts(payload)),
                delivery_mode="edit_status",
            )
            return

        if command == _CANCEL_COMMAND:
            payload = await server.artifact_gateway.cancel_collection(
                **common,
                idempotency_key=_idempotency_key(update, "cancel"),
            )
            control_status = str(payload.get("status") or "")
            if control_status == "cancelled":
                await _finalize_collection_snapshot(
                    update=update,
                    payload=payload,
                    locale=locale,
                    message_key="input_collection.cancelled_summary",
                )
            key = {
                "cancelled": "input_collection.cancelled",
                "not_found": "input_collection.not_found",
            }.get(control_status, "input_collection.failed")
            await server.finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=server._localized(key, locale=locale, **_counts(payload)),
                delivery_mode="edit_status",
            )
            return

        if command == _SEND_COMMAND:
            payload = await server.artifact_gateway.send_collection(
                **common,
                idempotency_key=_idempotency_key(update, "send"),
            )
            control_status = str(payload.get("status") or "")
            if control_status != "committed":
                key = {
                    "empty": "input_collection.empty",
                    "commit_requested": "input_collection.commit_requested",
                    "not_found": "input_collection.not_found",
                    "conflict": "input_collection.conflict",
                    "failed": "input_collection.failed",
                }.get(control_status, "input_collection.failed")
                await server.finish_status_or_send_reply(
                    update=update,
                    status_message=status_message,
                    text=server._localized(key, locale=locale, **_counts(payload)),
                    delivery_mode="edit_status",
                )
                return

            batch_id = str(payload.get("input_batch_id") or "").strip()
            if not batch_id:
                raise RuntimeError("Committed collection returned no input batch ID")
            if payload.get("duplicate"):
                await server.finish_status_or_send_reply(
                    update=update,
                    status_message=status_message,
                    text=server._localized(
                        "input_collection.already_sent",
                        locale=locale,
                    ),
                    delivery_mode="edit_status",
                )
                return

            # Collection and execution have separate public presentations. The
            # former remains as a durable package summary; the latter was just
            # created below /send and is the only target for AgentCycle progress.
            await _finalize_collection_snapshot(
                update=update,
                payload=payload,
                locale=locale,
                message_key="input_collection.committed_summary",
            )
            run_payload = await server.artifact_gateway.run_committed(
                batch_id,
                session_id=session_id,
                progress_locale=locale,
                progress_metadata=server._progress_metadata(
                    update,
                    status_message,
                    request_id=batch_id,
                ),
            )
            metadata = dict(run_payload.get("metadata") or {})
            metadata.setdefault("progress_locale", locale)
            await server._deliver_agent_result(
                update=update,
                status_message=status_message,
                success=True,
                message=str(run_payload.get("response") or ""),
                metadata=metadata,
                session_id=session_id,
            )
            await _finalize_delivered_run_status(
                update=update,
                status_message=status_message,
                metadata=metadata,
                locale=locale,
            )
            return

        raise RuntimeError(f"Unsupported input collection command: {command}")
    except Exception as error:
        server.logger.exception(
            "Telegram input collection command failed command=%s error=%r",
            command,
            error,
        )
        await server._deliver_agent_result(
            update=update,
            status_message=status_message,
            success=False,
            message=server._safe_transport_error(error, locale=locale),
            metadata={"progress_locale": locale},
            session_id=session_id,
        )


async def input_collection_command_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Handle one collection command and stop lower-priority command groups."""

    await _handle_input_collection_command(update, context)
    raise ApplicationHandlerStop
