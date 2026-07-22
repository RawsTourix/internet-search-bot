import os
import uuid
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .adapters.telegram_adapter import TelegramAdapter
from .adapters.web_adapter import WebAdapter
from .api.api import API
from .api.artifact_routes import create_artifact_router
from .api.artifact_transport import (
    ArtifactTransportFacade,
    HttpAttachmentStreamProvider,
)
from .core.message_processor import MessageProcessor
from .core.models import UnifiedMessage, WebMessage, MessageType, ClientType

from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("Gateway")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "gateway.log"),
    maxBytes=8*1024*1024,  # 8 MB
    encoding='utf-8'
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# API Key Authentication
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")


def get_api_keys():
    """Возвращает список валидных client/internal API-ключей."""
    keys = []
    for environment_name in (
        "TELEGRAM_API_KEY",
        "WEB_API_KEY",
        "INTERNAL_API_KEY",
    ):
        value = os.getenv(environment_name, "").strip()
        if value and value not in keys:
            keys.append(value)
    if not keys:
        raise RuntimeError("Отсутствуют API-ключи в переменных среды")
    return keys


VALID_API_KEYS = get_api_keys()

PROGRESS_CALLBACK_ALLOWED_PREFIXES = [
    value.strip()
    for value in os.getenv(
        "PROGRESS_CALLBACK_ALLOWED_PREFIXES",
        "http://127.0.0.1,http://localhost",
    ).split(",")
    if value.strip()
]


def is_allowed_progress_callback_url(url: str) -> bool:
    """Проверяет callback URL по явно разрешённым origin/path-prefix."""
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
        logger.warning("Progress callback URL rejected by allowlist: %s", callback_url)
        return None

    progress_target = metadata.get("progress_target") or {
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("status_message_id"),
    }
    callback_token = metadata.get("progress_callback_token")

    logger.debug(
        "Progress callback enabled: request_id=%s target=%s",
        request_id,
        progress_target,
    )

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
                logger.debug(
                    "Progress callback delivered: request_id=%s event_type=%s",
                    request_id,
                    event.get("type"),
                )
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
            detail="Missing API Key"
        )
    if api_key not in VALID_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API Key"
        )
    return api_key


# Инициализация компонентов
message_processor = MessageProcessor()
telegram_adapter = TelegramAdapter(message_processor)
web_adapter = WebAdapter(message_processor)

attachment_providers = {}
telegram_provider_url = os.getenv("TELEGRAM_FILE_PROVIDER_URL", "").strip()
telegram_provider_token = os.getenv("TELEGRAM_FILE_PROVIDER_TOKEN", "").strip()
if telegram_provider_url and telegram_provider_token:
    attachment_providers["telegram"] = HttpAttachmentStreamProvider(
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
    """Асинхронное управление жизненным циклом"""
    logger.info("Запуск Multi-Protocol Gateway...")

    await telegram_adapter.initialize()
    await web_adapter.initialize()
    await API.start()

    logger.info("Gateway успешно запущен")
    yield

    logger.info("Остановка Gateway...")
    await API.stop()
    await web_adapter.shutdown()
    await telegram_adapter.shutdown()


origins = os.getenv("CORS_ORIGINS", "*").split(",")

app = FastAPI(
    title="Multi-Protocol Gateway",
    version="1.0.0",
    lifespan=lifespan
)

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


##############################
## UNIFIED MESSAGE ENDPOINT ##
##############################

@app.post("/message", dependencies=[Depends(api_key_auth)])
async def unified_message_handler(message: UnifiedMessage):
    """Совместимый text/command endpoint."""
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


##########################
## WEB MESSAGE ENDPOINT ##
##########################

@app.post("/web/message", dependencies=[Depends(api_key_auth)])
async def web_message_handler(message: WebMessage):
    """Совместимый text-only endpoint веб-интерфейса."""
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
            metadata={"session_id": session_id}
        )

        response = await web_adapter.handle_unified_message(unified_message)

        return JSONResponse(
            content={
                "status": "ok",
                "response": response.content,
                "session_id": session_id,
                "metadata": response.metadata
            },
            media_type="application/json; charset=utf-8"
        )

    except Exception as error:
        logger.exception("Ошибка обработки web-сообщения: %r", error)
        raise HTTPException(status_code=500, detail="Internal server error")


#################################
## HEALTH AND STATUS ENDPOINTS ##
#################################

@app.get("/health")
async def health_check():
    """Проверка здоровья Gateway"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "adapters": {
            "telegram": await telegram_adapter.health_check(),
            "web": await web_adapter.health_check(),
        },
        "artifacts": {
            "enabled": API.artifact_config.enabled,
            "ingress_enabled": API.ingress_config.enabled,
            "attachment_providers": sorted(attachment_providers),
        },
    }


@app.get("/stats")
async def get_stats():
    """Статистика Gateway"""
    return await message_processor.get_stats()


@app.get("/")
async def root():
    return {"service": "Multi-Protocol Gateway", "status": "running"}
