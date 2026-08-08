"""High-priority Telegram transport handlers for IR-5 runtime controls.

These handlers own only Telegram identity/projection plumbing. Runtime semantics
remain in the Gateway application-layer InputRuntimeControlService.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


_RUNTIME_CONTROL_COMMANDS = ("stop", "continue")
_INSTALL_MARKER = "_input_runtime_ir5_control_handlers_installed"


def install_runtime_control_handlers(application: Any) -> None:
    """Register control commands before ordinary Telegram command handlers."""
    if getattr(application, _INSTALL_MARKER, False):
        return
    application.add_handler(
        CommandHandler(list(_RUNTIME_CONTROL_COMMANDS), runtime_control_handler),
        group=-10,
    )
    setattr(application, _INSTALL_MARKER, True)


async def runtime_control_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Forward one stable Telegram control delivery through the common API."""
    # Imported lazily to avoid a module cycle during Telegram server composition.
    from . import telegram_server as server

    message = update.effective_message
    full_text = message.text or ""
    words = full_text.split()
    command_token = words[0] if words else ""
    normalized = command_token.lower().split("@", 1)[0]
    if normalized not in {"/stop", "/continue"}:
        return

    session_id = server._session_for_update(update)
    thread_id = getattr(message, "message_thread_id", None)
    source_update_id = getattr(update, "update_id", None)
    locale = server.detect_progress_locale(update)
    status_message = await server.send_initial_status_message(
        update,
        server._localized("input.command_received", locale=locale),
    )
    payload = {
        "id": str(uuid4()),
        "timestamp": datetime.now().isoformat(),
        "client_type": "telegram",
        "message_type": "command",
        "content": normalized,
        "user_id": str(update.effective_user.id),
        "user_name": update.effective_user.full_name,
        "metadata": {
            "bot_instance_id": server.TELEGRAM_BOT_INSTANCE_ID,
            "chat_id": update.effective_chat.id,
            "conversation_id": str(update.effective_chat.id),
            "message_id": message.message_id,
            "message_thread_id": thread_id,
            "thread_id": thread_id,
            "update_id": source_update_id,
            "session_id": session_id,
            "progress_locale": locale,
        },
        "command": command_token,
        "arguments": words[1:],
    }
    server.attach_progress_metadata(
        payload=payload,
        update=update,
        status_message=status_message,
    )
    success, response, metadata = await server.send_to_gateway(payload)
    metadata = metadata or {}
    metadata.setdefault("progress_locale", locale)
    metadata["telegram_session_generation"] = server.session_generations.current(
        session_id
    )
    await server._deliver_agent_result(
        update=update,
        status_message=status_message,
        success=success,
        message=response,
        metadata=metadata,
        session_id=session_id,
    )
