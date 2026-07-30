"""Safe Telegram execution of a server-reserved presentation relocation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from telegram.error import BadRequest, NetworkError, TimedOut


async def apply_relocating_input_ack_policy(
    *,
    base_apply: Callable[..., Awaitable[Any]],
    server,
    update,
    submission: dict[str, Any],
    session_id: str,
):
    """Create, bind, supersede and only then best-effort delete the old status."""

    if str(submission.get("ack_policy") or "silent") != "relocate":
        return await base_apply(
            update=update,
            submission=submission,
            session_id=session_id,
        )

    ref = dict(submission.get("presentation_ref") or {})
    old_message_id = ref.get("previous_client_message_id") or ref.get(
        "client_message_id"
    )
    try:
        old_message_id_int = int(old_message_id)
        generation = int(ref.get("presentation_generation") or 0)
    except (TypeError, ValueError):
        server.logger.error(
            "telegram_presentation_relocation_invalid_reference presentation_id=%s",
            ref.get("presentation_id"),
        )
        return await base_apply(
            update=update,
            submission=submission,
            session_id=session_id,
        )

    text = server._presentation_text(submission)
    new_status = await server.send_initial_status_message(update, text)
    if new_status is None:
        server.logger.warning(
            "telegram_presentation_relocation_create_failed presentation_id=%s "
            "generation=%s",
            ref.get("presentation_id"),
            generation + 1,
        )
        return SimpleNamespace(message_id=old_message_id_int)

    new_message_id = int(new_status.message_id)
    try:
        await server.artifact_gateway.relocate_input_presentation(
            ref,
            session_id=session_id,
            client_message_id=str(new_message_id),
        )
    except Exception as error:
        server.logger.exception(
            "telegram_presentation_relocation_bind_failed presentation_id=%s "
            "old_message_id=%s new_message_id=%s error_type=%s",
            ref.get("presentation_id"),
            old_message_id_int,
            new_message_id,
            type(error).__name__,
        )
        # The new message is not authoritative without the durable bind. Remove
        # it when possible, but never touch the still-active old handle.
        try:
            await server.application.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=new_message_id,
            )
        except Exception:
            server.logger.warning(
                "telegram_unbound_relocation_message_cleanup_failed "
                "message_id=%s",
                new_message_id,
            )
        return SimpleNamespace(message_id=old_message_id_int)

    # Immutable committed batches may still contain an older progress target.
    # Redirect it before deleting the superseded handle, so every future event
    # reaches the new writable generation instead of raising "message not found".
    register_redirect = getattr(server, "register_progress_redirect", None)
    if callable(register_redirect):
        register_redirect(
            chat_id=int(update.effective_chat.id),
            old_message_id=old_message_id_int,
            new_message_id=new_message_id,
        )

    await server.stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=old_message_id_int,
    )

    deletion_state = "deleted"
    try:
        await server.application.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=old_message_id_int,
        )
    except BadRequest as error:
        # BadRequest is a NetworkError subclass in python-telegram-bot, so it
        # must be classified before the broader transport exception.
        deletion_state = "failed"
        server.logger.warning(
            "telegram_presentation_old_handle_delete_failed "
            "presentation_id=%s message_id=%s error=%s",
            ref.get("presentation_id"),
            old_message_id_int,
            error,
        )
    except (TimedOut, NetworkError) as error:
        deletion_state = "unknown"
        server.logger.warning(
            "telegram_presentation_old_handle_delete_unknown "
            "presentation_id=%s message_id=%s error_type=%s",
            ref.get("presentation_id"),
            old_message_id_int,
            type(error).__name__,
        )
    except Exception as error:
        deletion_state = "failed"
        server.logger.warning(
            "telegram_presentation_old_handle_delete_failed "
            "presentation_id=%s message_id=%s error_type=%s",
            ref.get("presentation_id"),
            old_message_id_int,
            type(error).__name__,
        )

    try:
        await server.artifact_gateway.record_input_presentation_deletion(
            ref,
            session_id=session_id,
            generation=generation,
            deletion_state=deletion_state,
        )
    except Exception as error:
        # The new durable bind is already authoritative. A missing deletion
        # receipt must never roll presentation ownership back to the old handle.
        server.logger.warning(
            "telegram_presentation_deletion_receipt_failed "
            "presentation_id=%s generation=%s state=%s error_type=%s",
            ref.get("presentation_id"),
            generation,
            deletion_state,
            type(error).__name__,
        )

    server.logger.info(
        "telegram_presentation_relocated presentation_id=%s "
        "generation=%s old_message_id=%s new_message_id=%s deletion_state=%s",
        ref.get("presentation_id"),
        generation + 1,
        old_message_id_int,
        new_message_id,
        deletion_state,
    )
    return new_status
