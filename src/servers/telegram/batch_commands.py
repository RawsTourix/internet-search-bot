"""Thin Telegram commands for the shared explicit InputBatch control plane."""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

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


async def _retire_terminal_collection_status(
    *,
    update: Update,
    payload: dict[str, Any],
    status_message,
) -> None:
    retire = getattr(server, "retire_input_collection_status", None)
    if not callable(retire):
        return
    await retire(
        update=update,
        previous_message_id=payload.get(
            "_telegram_previous_status_message_id"
        ),
        processing_status_message=status_message,
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
            key = {
                "cancelled": "input_collection.cancelled",
                "not_found": "input_collection.not_found",
            }.get(str(payload.get("status")), "input_collection.failed")
            if str(payload.get("status")) == "cancelled":
                await _retire_terminal_collection_status(
                    update=update,
                    payload=payload,
                    status_message=status_message,
                )
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

            # The collection presentation is terminal after commit. Redirect
            # every immutable/stale progress target to the status created below
            # /send, then retire the older collection message before AgentCycle.
            await _retire_terminal_collection_status(
                update=update,
                payload=payload,
                status_message=status_message,
            )
            run_payload = await server.artifact_gateway.run_committed(
                batch_id,
                session_id=session_id,
                progress_locale=locale,
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
