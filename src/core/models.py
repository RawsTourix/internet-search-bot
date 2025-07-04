from pydantic import BaseModel
from enum import Enum
from typing import Dict, Any, Optional, List
from datetime import datetime

class ClientType(str, Enum):
    """Типы клиентов"""
    TELEGRAM = "telegram"
    WEB = "web"
    CLI = "cli"

class MessageType(str, Enum):
    """Типы сообщений"""
    TEXT = "text"
    COMMAND = "command"
    FILE = "file"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"

class UnifiedMessage(BaseModel):
    """Унифицированная модель сообщения"""
    id: str
    client_type: ClientType
    message_type: MessageType
    content: str
    user_id: str
    user_name: Optional[str] = None
    timestamp: datetime
    metadata: Dict[str, Any] = {}

    # Поля специфичные для команд
    command: Optional[str] = None
    arguments: Optional[List[str]] = None
    
class UnifiedResponse(BaseModel):
    """Унифицированная модель ответа"""
    message_id: str
    client_type: ClientType
    content: str
    response_type: MessageType = MessageType.TEXT
    metadata: Dict[str, Any] = {}
    
class CommandRequest(BaseModel):
    """Модель CLI команды"""
    command: str
    args: List[str] = []
    user_id: str
    options: Dict[str, Any] = {}
    
class WebMessage(BaseModel):
    """Модель веб-сообщения"""
    content: str
    user_id: str
    session_id: Optional[str] = None
    message_type: MessageType = MessageType.TEXT

# === Adapters ===

class AdapterStatus(BaseModel):
    """Статус адаптера"""
    is_healthy: bool
    last_activity: Optional[datetime] = None
    error_count: int = 0
    message_count: int = 0

# === MCP Client ===

class ServerConnectType(str, Enum):
    """Перечисление типов подключения к серверу"""
    EXECUTABLE = "executable"  # Запуск сервера как процесса
    MCP_LOOKUP = "mcp_lookup"  # Использование имени из конфигурации MCP
    HTTP = "http"              # Подключение к серверу по HTTP

class LLMConfigType(BaseModel):
    """Конфигурации для языковой модели (LLM)"""
    api_url: str
    api_key: Optional[str] = None
    model: str = "default"
    headers: Optional[Dict[str, str]] = None
    is_openai_compatible: bool = True
    max_tokens: int = 1000
    temperature: float = 0.7

class ServerConfigType(BaseModel):
    """Конфигурация для MCP сервера"""
    connect_type: ServerConnectType
    name: Optional[str] = None
    executable: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    host: Optional[str] = None
    port: Optional[int] = None
    instructions: Optional[str] = None