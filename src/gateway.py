import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from .adapters.telegram_adapter import TelegramAdapter
from .adapters.web_adapter import WebAdapter
from .api.api import API
from .api.artifact_routes import create_artifact_router
from .api.artifact_transport import ArtifactTransportFacade
from .api.attachment_provider import StrictHttpAttachmentStreamProvider
from .api.domain_errors import register_domain_exception_handlers
from .api.legacy_delivery_guard import LegacyTelegramDeliveryGuardMiddleware
from .api.output_outbox_routes import create_output_outbox_router
from .core.message_processor import MessageProcessor
from .core.models import ClientType, MessageType, UnifiedMessage, WebMessage
from .interaction.output_startup_recovery import (
    reconcile_unclaimable_legacy_ready,
)


load_dotenv()

log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Gateway")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "gateway.log"),
        maxBytes=8 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")


def get_api_key_scopes() -> dict[str, frozenset[str]]:
    """Bind every configured internal credential to its transport authority."""

    mutable: dict[str, set[str]] = {}
    for environment_name, scope in (
        ("TELEGRAM_API_KEY", "telegram"),
        ("WEB_API_KEY", "web"),
        ("INTERNAL_API_KEY", "*"),
    ):
        value = os.getenv(environment_name, "").strip()
        if value:
            mutable.setdefault(value, set()).add(scope)
    if not mutable:
        raise RuntimeError("Отсутствуют API-ключи в переменных среды")
    return {
        key: frozenset(sorted(scopes))
        for key, scopes in mutable.items()
    }


def get_api_key_instance_scopes() -> dict[
    str,
    frozenset[tuple[str, str]],
]:
    """Bind transport credentials to exact client-instance authority."""

    telegram_instance = (
        os.getenv("TELEGRAM_BOT_INSTANCE_ID", "default").strip()
        or "default"
    )
    mutable: dict[str, set[tuple[str, str]]] = {}
    for environment_name, scope in (
        ("TELEGRAM_API_KEY", ("telegram", telegram_instance)),
        ("WEB_API_KEY", ("web", "*")),
        ("INTERNAL_API_KEY", ("*", "*")),
    ):
        value = os.getenv(environment_name, "").strip()
        if value:
            mutable.setdefault(value, set()).add(scope)
    if not mutable:
        raise RuntimeError("Отсутствуют API-ключи в переменных среды")
    return {
        key: frozenset(sorted(scopes))
        for key, scopes in mutable.items()
    }


API_KEY_SCOPES = get_api_key_scopes()
API_KEY_INSTANCE_SCOPES = get_api_key_instance_scopes()
VALID_API_KEYS = frozenset(API_KEY_SCOPES)
PROGRESS_CALLBACK_ALLOWED_PREFIXES = [
    value.strip()
    for value in os.getenv(
        "PROGRESS_CALLBACK_ALLOWED_PREFIXES",
        "http://127.0.0.1,http://localhost",
    ).split(",")
    if value.strip()
]


def is_allowed_progress_callback_url(url: str) -> bool:
    if not url:
        return False
    try:
        candidate = urlparse(url.strip())
        candidate_port = candidate.port
    except ValueError:
        return False
    if (
        candidate.scheme not in {"http", "https"}
        or not candidate.hostname
        or candidate.username is not None
        or candidate.password is not None
    ):
        return False
    for prefix in PROGRESS_CALLBACK_ALLOWED_PREFIXES:
        try:
            allowed = urlparse(prefix)
            allowed_port = allowed.port
        except ValueError:
            continue
        if (
            candidate.scheme == allowed.scheme
            and candidate.hostname == allowed.hostname
            and (allowed_port is None or candidate_port == allowed_port)
            and candidate.path.startswith(allowed.path or "/")
        ):
            return True
    return False


def _make_http_progress_callback(
    *,
    metadata: dict[str, Any],
    request_id: str,
    client_type: str,
):
    callback_url = metadata.get("progress_callback_url")
    if not callback_url:
        return None
    if not is_allowed_progress_callback_url(str(callback_url)):
        logger.warning(
            "Progress callback URL rejected by allowlist: %s",
            callback_url,
        )
        return None
    progress_target = metadata.get("progress_target") or {
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("status_message_id"),
    }
    callback_token = metadata.get("progress_callback_token")

    async def send_progress_event(event: dict[str, Any]):
        payload = {
            "type": "progress_event",
            "request_id": request_id,
            "client_type": client_type,
            "target": progress_target,
            "event": event,
        }
        headers = {}
        if callback_token:
            headers["X-Progress-Token"] = str(callback_token)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    str(callback_url),
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
        except Exception as error:
            logger.warning("Failed to send progress callback: %r", error)

    return send_progress_event


def make_http_progress_callback(message: UnifiedMessage):
    metadata = message.metadata or {}
    return _make_http_progress_callback(
        metadata=metadata,
        request_id=str(metadata.get("progress_request_id") or message.id),
        client_type=message.client_type.value,
    )


def make_input_batch_progress_callback(batch):
    route = batch.response_route
    metadata = dict(route.metadata or {})
    metadata.setdefault(
        "progress_target",
        {
            "conversation_id": route.conversation_id,
            "thread_id": route.thread_id,
            "message_id": route.reply_to_message_id,
        },
    )
    return _make_http_progress_callback(
        metadata=metadata,
        request_id=batch.input_batch_id,
        client_type=batch.client_type.value,
    )


async def api_key_auth(api_key: str = Depends(API_KEY_HEADER)):
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key",
        )
    if api_key not in VALID_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API Key",
        )
    return api_key


message_processor = MessageProcessor()
telegram_adapter = TelegramAdapter(message_processor)
web_adapter = WebAdapter(message_processor)

attachment_providers = {}
telegram_provider_url = os.getenv("TELEGRAM_FILE_PROVIDER_URL", "").strip()
telegram_provider_token = (
    os.getenv("TELEGRAM_FILE_PROVIDER_TOKEN", "").strip()
    or os.getenv("WEBHOOK_SECRET", "").strip()
)
if bool(telegram_provider_url) != bool(telegram_provider_token):
    raise RuntimeError(
        "TELEGRAM_FILE_PROVIDER_URL and its token must be configured together"
    )
if telegram_provider_url:
    attachment_providers["telegram"] = StrictHttpAttachmentStreamProvider(
        base_url=telegram_provider_url,
        token=telegram_provider_token,
        provider_name="telegram",
    )

artifact_transport = ArtifactTransportFacade(
    api=API,
    message_processor=message_processor,
    providers=attachment_providers,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own startup and shutdown resources in one cancellation-safe task.

    MCP streamable HTTP contexts contain AnyIO cancel scopes and must be exited
    by the same asyncio task that entered them. A forced Uvicorn shutdown throws
    ``CancelledError`` at the lifespan ``yield``; the ``finally`` block is
    therefore essential. Without it, Python later finalizes the async generator
    in another task and AnyIO reports ``Attempted to exit cancel scope in a
    different task``.
    """

    telegram_initialized = False
    web_initialized = False
    api_start_attempted = False
    shutdown_cancellation: asyncio.CancelledError | None = None

    try:
        logger.info("Запуск Multi-Protocol Gateway...")
        await telegram_adapter.initialize()
        telegram_initialized = True
        await web_adapter.initialize()
        web_initialized = True
        output_recovery = await reconcile_unclaimable_legacy_ready(
            API.output_store,
            API.artifact_services.delivery_store,
        )
        if output_recovery.cancelled_count:
            logger.warning(
                "Cancelled %s unclaimable legacy READY OutputBatch records: %s",
                output_recovery.cancelled_count,
                list(output_recovery.cancelled_legacy_output_batch_ids),
            )
        if output_recovery.repaired_count:
            logger.warning(
                "Repaired %s READY OutputBatch ownership bindings: %s",
                output_recovery.repaired_count,
                list(output_recovery.repaired_output_batch_ids),
            )
        if output_recovery.unrepaired_output_batch_ids:
            logger.error(
                "Unsafe READY OutputBatch ownership remains unrepaired: %s",
                list(output_recovery.unrepaired_output_batch_ids),
            )
        for item in output_recovery.remaining_ready:
            logger.warning(
                "Recoverable READY OutputBatch remains: "
                "output_batch_id=%s session_id=%s kind=%s "
                "client_type=%s client_instance_id=%s",
                item.output_batch_id,
                item.session_id,
                item.kind,
                item.client_type,
                item.client_instance_id,
            )
        api_start_attempted = True
        await API.start()
        logger.info("Gateway успешно запущен")
        yield
    finally:
        logger.info("Остановка Gateway...")

        if api_start_attempted:
            try:
                # Keep this await in the lifespan task. Moving it to a shielded
                # child task would violate AnyIO cancel-scope ownership.
                await API.stop()
            except asyncio.CancelledError as error:
                shutdown_cancellation = error
                logger.warning(
                    "Остановка API runtime была повторно отменена; "
                    "завершаю transport cleanup"
                )
            except Exception as error:
                logger.exception("Ошибка остановки API runtime: %r", error)

        if web_initialized:
            try:
                await web_adapter.shutdown()
            except asyncio.CancelledError as error:
                shutdown_cancellation = shutdown_cancellation or error
            except Exception as error:
                logger.exception("Ошибка остановки Web adapter: %r", error)

        if telegram_initialized:
            try:
                await telegram_adapter.shutdown()
            except asyncio.CancelledError as error:
                shutdown_cancellation = shutdown_cancellation or error
            except Exception as error:
                logger.exception("Ошибка остановки Telegram adapter: %r", error)

        logger.info("Gateway остановлен")
        if shutdown_cancellation is not None:
            raise shutdown_cancellation


origins = os.getenv("CORS_ORIGINS", "*").split(",")
app = FastAPI(
    title="Multi-Protocol Gateway",
    version="1.0.0",
    lifespan=lifespan,
)
register_domain_exception_handlers(app)
app.add_middleware(LegacyTelegramDeliveryGuardMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(
    create_artifact_router(
        facade=artifact_transport,
        auth_dependency=api_key_auth,
        progress_callback_factory=make_input_batch_progress_callback,
    )
)
app.include_router(
    create_output_outbox_router(
        facade=artifact_transport,
        auth_dependency=api_key_auth,
        api_key_scopes=API_KEY_SCOPES,
        api_key_instance_scopes=API_KEY_INSTANCE_SCOPES,
    )
)


@app.post("/message", dependencies=[Depends(api_key_auth)])
async def unified_message_handler(message: UnifiedMessage):
    try:
        processor = {
            ClientType.TELEGRAM: telegram_adapter.handle_unified_message,
            ClientType.WEB: web_adapter.handle_unified_message,
        }[message.client_type]
        progress_callback = make_http_progress_callback(message)
        response = await processor(
            message,
            progress_callback=progress_callback,
        )
        return {
            "status": "ok",
            "response": response.content,
            "metadata": response.metadata,
        }
    except KeyError:
        raise HTTPException(status_code=400, detail="Unsupported client type")
    except Exception as error:
        logger.exception("Ошибка обработки сообщения: %r", error)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/web/message", dependencies=[Depends(api_key_auth)])
async def web_message_handler(message: WebMessage):
    try:
        session_id = message.session_id or str(uuid.uuid4())
        unified_message = UnifiedMessage(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            client_type=ClientType.WEB,
            message_type=message.message_type or MessageType.TEXT,
            content=message.content,
            user_id=message.user_id,
            user_name=None,
            metadata={"session_id": session_id},
        )
        response = await web_adapter.handle_unified_message(unified_message)
        return JSONResponse(
            content={
                "status": "ok",
                "response": response.content,
                "session_id": session_id,
                "metadata": response.metadata,
            },
            media_type="application/json; charset=utf-8",
        )
    except Exception as error:
        logger.exception("Ошибка обработки web-сообщения: %r", error)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/health")
async def health_check():
    telegram_health = await telegram_adapter.health_check()
    web_health = await web_adapter.health_check()
    return {
        "status": "healthy",
        "adapters": {
            "telegram": telegram_health,
            "web": web_health,
        },
        "artifact_providers": sorted(attachment_providers),
    }
