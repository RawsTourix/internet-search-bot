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

    text = server._presentation_text(submission)
    new_status = await server.send_initial_status_message(update, text)
    if new_status is None:
        ref = dict(submission.get("presentation_ref") or {})
        old_message_id = ref.get("previous_client_message_id") or ref.get(
            "client_message_id"
        )
        server.logger.warning(
            "telegram_presentation_relocation_create_failed presentation_id=%s "
            "generation=%s",
            ref.get("presentation_id"),
            int(ref.get("presentation_generation") or 0) + 1,
        )
        try:
            return SimpleNamespace(message_id=int(old_message_id))
        except (TypeError, ValueError):
            return None

    return await relocate_precreated_input_presentation(
        server=server,
        gateway=server.artifact_gateway,
        submission=submission,
        session_id=session_id,
        status_message=new_status,
        chat_id=update.effective_chat.id,
        cleanup_unbound=True,
    )


async def relocate_precreated_input_presentation(
    *,
    server,
    gateway,
    submission: dict[str, Any],
    session_id: str,
    status_message,
    chat_id: int | str | None = None,
    cleanup_unbound: bool = False,
    raise_on_bind_failure: bool = False,
):
    """Bind one already-created status as the next presentation generation."""

    if str(submission.get("ack_policy") or "silent") != "relocate":
        return status_message

    ref = dict(submission.get("presentation_ref") or {})
    old_message_id = ref.get("previous_client_message_id") or ref.get(
        "client_message_id"
    )
    new_message_id = getattr(status_message, "message_id", None)
    try:
        old_message_id_int = int(old_message_id)
        new_message_id_int = int(new_message_id)
        generation = int(ref.get("presentation_generation") or 0)
    except (TypeError, ValueError):
        server.logger.error(
            "telegram_presentation_relocation_invalid_reference presentation_id=%s",
            ref.get("presentation_id"),
        )
        return status_message

    if old_message_id_int == new_message_id_int:
        return status_message

    resolved_chat_id = _resolve_chat_id(status_message, fallback=chat_id)
    try:
        await gateway.relocate_input_presentation(
            ref,
            session_id=session_id,
            client_message_id=str(new_message_id_int),
        )
    except Exception as error:
        server.logger.exception(
            "telegram_presentation_relocation_bind_failed presentation_id=%s "
            "old_message_id=%s new_message_id=%s error_type=%s",
            ref.get("presentation_id"),
            old_message_id_int,
            new_message_id_int,
            type(error).__name__,
        )
        if cleanup_unbound and resolved_chat_id is not None:
            try:
                await server.application.bot.delete_message(
                    chat_id=resolved_chat_id,
                    message_id=new_message_id_int,
                )
            except Exception:
                server.logger.warning(
                    "telegram_unbound_relocation_message_cleanup_failed "
                    "message_id=%s",
                    new_message_id_int,
                )
            return SimpleNamespace(message_id=old_message_id_int)
        if raise_on_bind_failure:
            raise
        return status_message

    register_redirect = getattr(server, "register_progress_redirect", None)
    if callable(register_redirect) and resolved_chat_id is not None:
        try:
            numeric_chat_id = int(resolved_chat_id)
        except (TypeError, ValueError):
            numeric_chat_id = resolved_chat_id
        register_redirect(
            chat_id=numeric_chat_id,
            old_message_id=old_message_id_int,
            new_message_id=new_message_id_int,
        )

    if resolved_chat_id is not None:
        await server.stop_progress_edits(
            chat_id=resolved_chat_id,
            message_id=old_message_id_int,
        )

    deletion_state = "unknown"
    if resolved_chat_id is not None:
        deletion_state = "deleted"
        try:
            await server.application.bot.delete_message(
                chat_id=resolved_chat_id,
                message_id=old_message_id_int,
            )
        except BadRequest as error:
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
        await gateway.record_input_presentation_deletion(
            ref,
            session_id=session_id,
            generation=generation,
            deletion_state=deletion_state,
        )
    except Exception as error:
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
        new_message_id_int,
        deletion_state,
    )
    return status_message


def _resolve_chat_id(status_message, *, fallback: int | str | None):
    if fallback is not None:
        return fallback
    direct = getattr(status_message, "chat_id", None)
    if direct is not None:
        return direct
    chat = getattr(status_message, "chat", None)
    return getattr(chat, "id", None) if chat is not None else None
