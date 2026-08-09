"""High-priority Telegram handlers for runtime controls and IR-9 status.

These handlers own only Telegram identity/projection plumbing. Runtime semantics
remain in the Gateway application-layer input-runtime services.
"""
from __future__ import annotations

import sys
from datetime import datetime
from typing import Any
from uuid import uuid4

from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
)

from .runtime_projection_edits import (
    install_runtime_projection_editing,
    install_runtime_projection_progress_middleware,
)


_RUNTIME_CONTROL_COMMANDS = ("stop", "continue")
_INSTALL_MARKER = "_input_runtime_ir9_command_handlers_installed"


def install_runtime_control_handlers(application: Any) -> None:
    """Register runtime commands before ordinary Telegram command handlers."""
    if getattr(application, _INSTALL_MARKER, False):
        return

    # Never force-import the Telegram host here: IR-5 characterization and
    # composition seams intentionally install handlers without a real bot token.
    # In production app.py has already imported telegram_server, so IR-9
    # presentation wrappers can be installed without changing host lifecycle.
    server = sys.modules.get(f"{__package__}.telegram_server")
    if server is not None and hasattr(server, "apply_input_ack_policy"):
        install_runtime_projection_editing(server)
        gateway = getattr(server, "artifact_gateway", None)
        if gateway is not None:
            install_runtime_projection_progress_middleware(server, gateway)

    application.add_handler(
        CommandHandler(list(_RUNTIME_CONTROL_COMMANDS), runtime_control_handler),
        group=-10,
    )
    application.add_handler(
        CommandHandler("status", runtime_status_handler),
        group=-10,
    )
    setattr(application, _INSTALL_MARKER, True)


def _command_payload(update: Update, *, normalized: str, locale: str) -> dict[str, Any]:
    from . import telegram_server as server

    message = update.effective_message
    full_text = message.text or ""
    words = full_text.split()
    command_token = words[0] if words else normalized
    session_id = server._session_for_update(update)
    thread_id = getattr(message, "message_thread_id", None)
    return {
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
            "update_id": getattr(update, "update_id", None),
            "session_id": session_id,
            "progress_locale": locale,
        },
        "command": command_token,
        "arguments": words[1:],
    }


async def runtime_status_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Read trusted-session diagnostics without entering input/control FIFO."""
    from . import telegram_server as server

    locale = server.detect_progress_locale(update)
    payload = _command_payload(update, normalized="/status", locale=locale)
    success, response, _metadata = await server.send_to_gateway(payload)
    if success:
        text = response
    else:
        text = server._localized(
            "input_runtime.status.unavailable",
            locale=locale,
            reason="diagnostics_unavailable",
        )
    try:
        await server.telegram_reply_with_retries(
            update,
            text,
            parse_mode=None,
            max_retries=3,
            base_delay=0.5,
        )
    finally:
        # Prevent the legacy group-0 command handler from appending process-local
        # transport diagnostics or issuing a second Gateway /status request.
        raise ApplicationHandlerStop


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
    locale = server.detect_progress_locale(update)
    status_message = await server.send_initial_status_message(
        update,
        server._localized("input.command_received", locale=locale),
    )
    payload = _command_payload(update, normalized=normalized, locale=locale)
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
