import os
import re
import gc
import sys
import json
import logging
import shutil
import asyncio
import time
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import AsyncExitStack
from types import SimpleNamespace
from logging.handlers import RotatingFileHandler

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from ..core.models import AgentStatus, AgentResult
from ..core.errors import LLMError, LLMHTTPError, LLMTimeoutError, LLMTransportError

# Модели
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
    instructions: Optional[str] = None

class ServerConfigType(BaseModel):
    """Конфигурация для MCP сервера"""
    name: Optional[str] = None
    alias: Optional[str] = None
    connect_type: ServerConnectType = ServerConnectType.EXECUTABLE
    executable: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    host: Optional[str] = None
    port: Optional[int] = None
    enabled: bool = True

@dataclass
class SessionMemory:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)

@dataclass
class SessionState:
    status: AgentStatus = AgentStatus.IDLE
    last_seen: float = field(default_factory=time.time)
    iterations: int = 0
    tools_used: List[str] = field(default_factory=list)
    last_error: Optional[str] = None
    awaiting_user_input: bool = False

@dataclass
class MCPServerRuntime:
    name: str
    alias: str
    connect_type: ServerConnectType
    session: Any = None
    http_client: Any = None
    exit_stack: Optional[AsyncExitStack] = None
    tools: List[Any] = field(default_factory=list)

@dataclass
class MCPToolBinding:
    public_name: str
    server_name: str
    server_alias: str
    remote_name: str
    description: str
    input_schema: Dict[str, Any]

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("mcp_client")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "mcp_client.log"),
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

class MCPHttpClient:
    """
    Description:
    ---------------
        Класс для взаимодействия с MCP сервером по HTTP.
        
    Args:
    ---------------
        host: Хост сервера
        port: Порт сервера
    """
    def __init__(self, host: str, port: int):
        self.base_url = f"http://{host}:{port}"
        self.http_client = httpx.AsyncClient()
        
    async def initialize(self):
        """
        Description:
        ---------------
            Инициализация клиента.
        """
        # Проверка доступности сервера
        try:
            response = await self.http_client.get(f"{self.base_url}/status")
            if response.status_code != 200:
                raise Exception(f"Сервер недоступен: {response.status_code}")
        except Exception as e:
            raise Exception(f"Ошибка при подключении к серверу: {str(e)}")
    
    async def list_tools(self):
        """
        Description:
        ---------------
            Получение списка доступных инструментов.
            
        Returns:
        ---------------
            Список доступных инструментов
        """
        response = await self.http_client.get(f"{self.base_url}/tools")
        if response.status_code == 200:
            data = response.json()
            return SimpleNamespace(tools=data["tools"])
        else:
            raise Exception(f"Ошибка при получении списка инструментов: {response.status_code}")
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        """
        Description:
        ---------------
            Вызов инструмента.
            
        Args:
        ---------------
            tool_name: Имя инструмента
            arguments: Аргументы инструмента
            
        Returns:
        ---------------
            Результат вызова инструмента
        """
        payload = {
            "tool": tool_name,
            "arguments": arguments
        }
        response = await self.http_client.post(f"{self.base_url}/call", json=payload)
        if response.status_code == 200:
            data = response.json()
            # Преобразуем список текстовых ответов в объекты TextContent
            content = [TextContent(text=item) for item in data.get("content", [])]
            return SimpleNamespace(content=content)
        else:
            raise Exception(f"Ошибка при вызове инструмента: {response.status_code}")
    
    async def close(self):
        """
        Description:
        ---------------
            Закрытие клиента.
        """
        await self.http_client.aclose()


def find_python_executable() -> str:
    """
    Description:
    ---------------
        Находит доступный исполняемый файл Python в системе.
        
    Returns:
    ---------------
        str: Команда для запуска Python
        
    Examples:
    ---------------
        >>> find_python_executable()
        'python3'
    """
    # Проверяем возможные варианты
    python_variants = [
        "python3", "python", "python3.10", 
        "python3.11", "python3.12", "python3.13"
    ]
    
    for cmd in python_variants:
        if shutil.which(cmd):
            logger.info(f"Найден исполняемый файл Python: {shutil.which(cmd)}")
            return cmd
    
    # Если никакой вариант не найден, пробуем использовать sys.executable
    if sys.executable:
        logger.info(f"Используем текущий Python: {sys.executable}")
        return sys.executable
    
    # Последняя попытка - просто вернуть "python3"
    logger.warning(
        "Не удалось найти Python, используем 'python3' по умолчанию"
    )
    return "python3"


class MCPClient:
    """
    Description:
    ---------------
        Клиент для работы с через MCP и языковую модель.
        
    Args:
    ---------------
        llm_config: Конфигурация для языковой модели
        
    Examples:
    ---------------
        >>> llm_config = LLMConfig(
        ...     api_url="https://api.openai.com/v1/chat/completions",
        ...     api_key="sk-..."
        ... )
        >>> client = MCPClient(llm_config)
    """
    def __init__(self, llm_config: LLMConfigType):
        """
        Description:
        ---------------
            Инициализация клиента для работы с LLM и MCP сервером.
            
        Args:
        ---------------
            llm_config: Конфигурация для LLM
        """
        self.session = None
        self.exit_stack = AsyncExitStack()
        self.server_name = 'Unnamed'
        self.max_iterations = 50
        
        # Настройка для LLM
        self.llm_config = llm_config

        headers = dict(llm_config.headers or {})

        if llm_config.api_key:
            has_auth = any(k.lower() == "authorization" for k in headers)
            if not has_auth:
                headers["Authorization"] = f"Bearer {llm_config.api_key}"
        
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")

        self.http_client = httpx.AsyncClient(headers=headers)
        self.llm_config.headers = headers

        self.instructions = (
            llm_config.instructions
            or "Ты ассистент, задача которого — помогать пользователю решать его задачи."
        )
        self.server_runtimes: Dict[str, MCPServerRuntime] = {}
        self.tool_registry: Dict[str, MCPToolBinding] = {}
        self.available_tools: List[MCPToolBinding] = []
        
        # Настройки таймаутов
        self.tool_call_timeout = 120.0  # Таймаут для вызова инструментов
        self.llm_call_timeout = 60.0   # Таймаут для вызова LLM

        # Настройка повторных запросов
        self.llm_max_retries = 3
        self.llm_retry_base_delay = 5.0
        self.llm_retry_max_delay = 60.0

        self.llm_retryable_http_statuses = {429, 500, 502, 503, 504}

        # Память сессий
        self.sessions: Dict[str, SessionMemory] = {}
        self.session_states: Dict[str, SessionState] = {}
        self.max_history_messages = 24
    
    async def _connect_executable_server(
        self,
        server_config: ServerConfigType,
        server_name: str,
        server_alias: str
    ) -> MCPServerRuntime:
        executable = server_config.executable

        if not executable:
            executable = find_python_executable()

        executable_path = shutil.which(executable)
        if not executable_path:
            raise FileNotFoundError(f"Исполняемый файл не найден: {executable}")

        env = server_config.env or {}
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONLEGACYWINDOWSSTDIO": "0",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        })

        server_params = StdioServerParameters(
            command=executable_path,
            args=server_config.args or [],
            env=env
        )

        exit_stack = AsyncExitStack()

        stdio_transport = await exit_stack.enter_async_context(
            stdio_client(server_params)
        )

        stdio, write = stdio_transport

        session = await exit_stack.enter_async_context(
            ClientSession(stdio, write)
        )

        await session.initialize()

        response = await session.list_tools()
        tools = response.tools

        logger.info(
            f"MCP-сервер {server_name} подключён. "
            f"Инструменты: {[tool.name for tool in tools]}"
        )

        return MCPServerRuntime(
            name=server_name,
            alias=server_alias,
            connect_type=server_config.connect_type,
            session=session,
            exit_stack=exit_stack,
            tools=tools
        )

    async def _connect_single_server(self, server_config: ServerConfigType) -> MCPServerRuntime:
        server_name = server_config.name or "unnamed"
        server_alias = server_config.alias or server_name

        logger.info(f"Подключение к MCP-серверу: {server_name}")

        if server_config.connect_type == ServerConnectType.EXECUTABLE:
            return await self._connect_executable_server(server_config, server_name, server_alias)

        if server_config.connect_type == ServerConnectType.HTTP:
            return await self._connect_http_server(server_config, server_name, server_alias)

        if server_config.connect_type == ServerConnectType.MCP_LOOKUP:
            return await self._connect_lookup_server(server_config, server_name, server_alias)

        raise ValueError(f"Неизвестный тип подключения: {server_config.connect_type}")
    
    def _register_server_tools(self, runtime: MCPServerRuntime) -> None:
        for tool in runtime.tools:
            public_name = f"{runtime.alias}_{tool.name}"

            if public_name in self.tool_registry:
                raise ValueError(f"Конфликт имён инструментов: {public_name}")

            binding = MCPToolBinding(
                public_name=public_name,
                server_name=runtime.name,
                server_alias=runtime.alias,
                remote_name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {}
            )

            self.tool_registry[public_name] = binding
            self.available_tools.append(binding)

    async def connect_to_servers(self, server_configs: List[ServerConfigType]):
        """
        Description:
        ---------------
            Подключение к MCP серверам.
            
        Args:
        ---------------
            server_configs (List[ServerConfigType]): Конфигурации серверов
        """
        for server_config in server_configs:
            if not server_config.enabled:
                logger.info(f"Сервер {server_config.name} отключён, пропускаю")
                continue

            runtime = await self._connect_single_server(server_config)
            self.server_runtimes[runtime.name] = runtime
            self._register_server_tools(runtime)

        logger.info(
            f"Подключено MCP-серверов: {list(self.server_runtimes.keys())}"
        )
        logger.info(
            f"Доступные инструменты: {list(self.tool_registry.keys())}"
        )
    
    async def list_tools(self):
        """
        Description:
        ---------------
            Получение списка доступных инструментов.
            
        Returns:
        ---------------
            Список доступных инструментов
        """
        return self.available_tools
    
    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: Dict[str, Any]
    ):
        binding = self.tool_registry.get(public_tool_name)

        if binding is None:
            raise ValueError(f"Неизвестный инструмент: {public_tool_name}")

        runtime = self.server_runtimes.get(binding.server_name)

        if runtime is None:
            raise RuntimeError(
                f"Сервер для инструмента {public_tool_name} не подключён: "
                f"{binding.server_name}"
            )

        if runtime.session is not None:
            return await runtime.session.call_tool(
                binding.remote_name,
                arguments
            )

        if runtime.http_client is not None:
            return await runtime.http_client.call_tool(
                binding.remote_name,
                arguments
            )

        raise RuntimeError(
            f"У сервера {binding.server_name} нет активного клиента"
        )
        
    async def process_query(self, query: str, session_id: str = "default") -> AgentResult:
        """
        Description:
        ---------------
            Обработка запроса с использованием LLM и доступных инструментов.
            
        Args:
        ---------------
            query: Текст запроса от пользователя
            
        Returns:
        ---------------
            AgentResult: Результат обработки запроса
            
        Raises:
        ---------------
            Exception: При ошибке обработки запроса
        """
        logger.info(f"Начало обработки запроса: '{query}'")
        final_text = []
        
        try:
            # Создание состояния сессии
            state = self._get_or_create_state(session_id)
            state.status = AgentStatus.RUNNING
            state.iterations = 0
            state.tools_used = []
            state.last_error = None
            state.awaiting_user_input = False

            # Составляем системное сообщение с инструкциями
            system_message = self._create_system_message()

            # Инициализируем диалог
            session = self._get_or_create_session(session_id, system_message)
            messages = session.messages
            messages.append({"role": "user", "content": query})
            
            logger.debug(f"Сообщения для LLM: {messages}")
            
            # Преобразуем инструменты в формат для LLM
            tools = self._format_tools_for_llm()
            
            # Основной цикл обработки            
            for i in range(self.max_iterations):
                state.iterations = i + 1
                logger.info(f"Итерация {state.iterations}/{self.max_iterations}")
                
                try:
                    # Вызываем LLM с таймаутом
                    llm_response = await self._call_llm_with_retries(
                        messages,
                        tools,
                        context=f"Итерация {i + 1}"
                    )
                    logger.debug(f"Получен ответ от модели: {llm_response}")
                    
                    # Проверяем наличие вызовов инструментов
                    tool_calls = llm_response.get("tool_calls", [])
                    content = llm_response.get("content", "")
                    
                    # Добавляем текстовый ответ
                    if content:
                        logger.info(f"Получен текстовый ответ от модели: {content}")
                        final_text.append(content)
                    
                    if not tool_calls:
                        cleaned_content = self._strip_agent_markers(content) if content else ""
                        agent_status = self._extract_agent_status(content) if content else None

                        if agent_status is None:
                            logger.error("Отсутсвует маркер статуса! Пробуем повторно спросить его у LLM.")
                            marker_prompt = (
                                "Определи статус предыдущего ответа. "
                                "Верни только один маркер без пояснений: "
                                "[AGENT_STATUS=WAITING_USER] или [AGENT_STATUS=CONTINUE] или [AGENT_STATUS=DONE]."
                            )
                            messages.append({"role": "user", "content": marker_prompt})

                            logger.debug(f"Сообщения для LLM: {messages}")

                            llm_response = await self._call_llm_with_retries(
                                marker_prompt,
                                tools,
                                context=f"Marker repair на итерации {i + 1}"
                            )
                            logger.debug(f"Получен ответ от модели: {llm_response}")

                            marker = llm_response.get("content", "")
                            agent_status = self._extract_agent_status(marker) if marker else AgentStatus.ERROR

                            if agent_status is AgentStatus.ERROR:
                                state.status = AgentStatus.ERROR
                                state.last_error = "LLM response missing AGENT_STATUS marker"
                                cleaned_content = self._strip_agent_markers(content) if content else ""
                                if cleaned_content:
                                    messages.append({
                                        "role": "assistant",
                                        "content": cleaned_content
                                    })
                                final_text = [cleaned_content or "Ошибка на стороне LLM. Ответ модели не содержит обязательный маркер статуса."]
                                break
                        
                        if agent_status is None:
                            logger.warning(
                                "LLM не вернула маркер статуса даже после повторного запроса. "
                                "Использую fallback-статус."
                            )

                            if cleaned_content:
                                agent_status = AgentStatus.DONE
                            else:
                                self._finish_with_error(
                                    state,
                                    final_text,
                                    "LLM не вернула ни содержательный ответ, ни маркер статуса"
                                )
                                break

                        if cleaned_content:
                            messages.append({
                                "role": "assistant",
                                "content": cleaned_content
                            })

                        state.status = agent_status
                        state.awaiting_user_input = agent_status == AgentStatus.WAITING_USER

                        logger.info(f"Нет вызовов инструментов, завершаем обработку со статусом {agent_status}")
                        final_text = [cleaned_content] if cleaned_content else ["Пустой ответ."]
                        break
                    
                    # Обрабатываем вызовы инструментов
                    assistant_message = {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls
                    }
                    messages.append(assistant_message)
                    
                    tool_results = []
                    for tool_call in tool_calls:
                        function = tool_call.get("function", {})
                        tool_name = function.get("name", "")
                        tool_call_id = tool_call.get("id", "")
                        
                        if tool_name and tool_name not in state.tools_used:
                            state.tools_used.append(tool_name)

                        logger.info(f"Вызов инструмента: {tool_name}")
                        
                        try:
                            # Парсим аргументы
                            arguments = json.loads(function.get("arguments", "{}"))
                            logger.debug(f"Аргументы инструмента {tool_name}: {arguments}")
                            
                            # Вызываем инструмент через соответствующий клиент с таймаутом
                            result = await asyncio.wait_for(
                                self._call_registered_tool(tool_name, arguments),
                                timeout=self.tool_call_timeout
                            )
                            
                            # Преобразуем результат в текст
                            tool_result = self._format_tool_result(result.content)
                            logger.info(f"Результат инструмента {tool_name}: {tool_result}")
                            
                            tool_results.append(tool_result)
                            
                            # Добавляем результат в сообщения
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": tool_result
                            })
                            
                        except asyncio.TimeoutError:  # Обработка таймаута
                            error_message = f"Таймаут при вызове инструмента {tool_name}"
                            logger.error(error_message)
                            tool_results.append(error_message)
                            
                            # Добавляем сообщение об ошибке
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": error_message
                            })
                            
                        except Exception as e:
                            error_message = (
                                f"Ошибка при вызове инструмента {tool_name}: {str(e)}"
                            )
                            logger.error(error_message)
                            tool_results.append(error_message)
                            
                            # Добавляем сообщение об ошибке
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": error_message
                            })
                    
                    # Если последняя итерация и были вызовы, получаем финальный ответ
                    if i == self.max_iterations - 1 and tool_results:
                        try:
                            final_response = await self._call_llm_with_retries(
                                messages,
                                tools,
                                context="Финальный ответ после tool calls"
                            )
                            final_content = final_response.get("content", "")
                            if final_content:
                                final_text.clear()
                                final_text.append(final_content)

                            state.status = AgentStatus.DONE

                        except LLMTimeoutError as e:
                            error_message = f"Таймаут при получении финального ответа: {e}"
                            self._finish_with_error(state, final_text, error_message)
                        except LLMHTTPError as e:
                            if e.status_code == 429:
                                error_message = (
                                    f"LLM временно перегружена или достигнут лимит запросов. "
                                    f"Повторы исчерпаны на итерации {i + 1}: {e}"
                                )
                            else:
                                error_message = f"HTTP-ошибка LLM на итерации {i + 1}: {e}"

                            self._finish_with_error(state, final_text, error_message)
                            break
                        except LLMTransportError as e:
                            error_message = f"Сетевая ошибка при получении финального ответа: {e}"
                            self._finish_with_error(state, final_text, error_message)
                        except asyncio.TimeoutError:
                            error_message = "Общий таймаут при получении финального ответа"
                            self._finish_with_error(state, final_text, error_message)
                        except Exception as e:
                            error_message = f"Ошибка при получении финального ответа: {type(e).__name__}: {e!r}"
                            self._finish_with_error(state, final_text, error_message, log_exception=True)
                            
                except LLMTimeoutError as e:
                    error_message = f"Таймаут LLM на итерации {i+1}: {e}"
                    self._finish_with_error(state, final_text, error_message)
                    break

                except LLMHTTPError as e:
                    error_message = f"HTTP-ошибка LLM на итерации {i+1}: {e}"
                    self._finish_with_error(state, final_text, error_message)
                    break

                except LLMTransportError as e:
                    error_message = f"Сетевая ошибка LLM на итерации {i+1}: {e}"
                    self._finish_with_error(state, final_text, error_message)
                    break

                except asyncio.TimeoutError:
                    error_message = f"Общий таймаут обработки LLM на итерации {i+1}"
                    self._finish_with_error(state, final_text, error_message)
                    break

                except Exception as e:
                    error_message = f"Ошибка на итерации {i+1}: {type(e).__name__}: {e!r}"
                    self._finish_with_error(state, final_text, error_message, log_exception=True)
                    break
            
            if state.iterations >= self.max_iterations and state.status == AgentStatus.RUNNING:
                state.status = AgentStatus.ERROR
                state.last_error = f"Достигнут лимит итераций: {self.max_iterations}"
            
            result_text = final_text[-1] if final_text else "Пустой ответ."
            self._trim_session_messages(session)

            if state.status == AgentStatus.RUNNING:
                state.status = AgentStatus.DONE

            logger.info(f"Завершение обработки запроса. Результат: {result_text}")

            return AgentResult(
                content=result_text,
                status=state.status,
                session_id=session_id,
                iterations=state.iterations,
                tools_used=state.tools_used,
                error=state.last_error
            )
            
        except Exception as e:
            error_message = f"Критическая ошибка при обработке запроса: {str(e)}"
            logger.error(error_message)

            state = self._get_or_create_state(session_id)
            state.status = AgentStatus.ERROR
            state.last_error = error_message

            return AgentResult(
                content=error_message,
                status=AgentStatus.ERROR,
                session_id=session_id,
                iterations=state.iterations,
                tools_used=state.tools_used,
                error=error_message
            )
    
    def _format_tool_result(self, content_list: List[Any]) -> str:
        """
        Description:
        ---------------
            Форматирует результат вызова инструмента в текстовый формат.
            
        Args:
        ---------------
            content_list: Список объектов с текстовым содержимым
            
        Returns:
        ---------------
            str: Форматированный результат в виде текста
        """
        return "\n".join(
            [item.text for item in content_list if hasattr(item, 'text')]
        )
    
    def _create_system_message(self) -> str:
        """
        Description:
        ---------------
            Создает системное сообщение с описанием инструментов.
            
        Returns:
        ---------------
            str: Текст системного сообщения
        """
        
        return (
            f"{self.instructions}\n\n"
            "Правила агентного режима:\n"
            "1. Если тебе нужен ответ или действие пользователя, заканчивай ответ маркером [AGENT_STATUS=WAITING_USER]\n"
            "2. Если нужно продолжать работу через инструменты, используй [AGENT_STATUS=CONTINUE]\n"
            "3. Если задача завершена, заканчивай ответ маркером [AGENT_STATUS=DONE]\n"
            "4. Никогда не пропускай маркер статуса\n"
            "5. Если ты вызвал инструмент и получил результат, обязательно сформируй ответ пользователю "
            "на основе результата инструмента.\n\n"
            f"У тебя есть доступ к следующим инструментам:\n{self._tools_description()}\n\n"
            "Прежде чем ответить, оцени неопределённость своего ответа.\n"
            "Если она больше 0.1, задай мне уточняющие вопросы, пока она не станет 0.1 или ниже.\n\n"
            "Перед финальным ответом проверяй команды, имена инструментов, "
            "URL и названия пакетов по найденному источнику. Не изменяй символы в командах."
        )
    
    def _tools_description(self) -> List[Dict[str, Any]]:
        """
        Description:
        ---------------
            Составляет описание инструментов.
            
        Returns:
        ---------------
            List[Dict[str, Any]]: Список описания инструментов
        """
        tools_description = []

        for binding in self.available_tools:
            tools_description.append({
                "name": binding.public_name,
                "server": binding.server_alias,
                "description": re.sub(
                    r" {2,}",
                    " ",
                    re.sub(r"\n|\t|-{5,}", " ", binding.description)
                ).strip(),
                "inputSchema": binding.input_schema
            })

        return tools_description
    
    def _format_tools_for_llm(self) -> List[Dict[str, Any]]:
        """
        Description:
        ---------------
            Форматирует инструменты в формат, понятный LLM API.
            
        Returns:
        ---------------
            List[Dict[str, Any]]: Список инструментов в формате для LLM
        """
        llm_tools = []
        
        for binding in self.available_tools:
            function_spec = {
                "name": binding.public_name,
                "description": binding.description,
                "parameters": binding.input_schema
            }
            
            llm_tools.append({
                "type": "function",
                "function": function_spec
            })
            
        return llm_tools
    
    def _parse_retry_after(self, value: str | None) -> float | None:
        if not value:
            return None

        try:
            delay = float(value)
            if delay >= 0:
                return delay
        except ValueError:
            return None

        return None
    
    def _get_llm_retry_delay(
        self,
        error: LLMHTTPError | None,
        attempt: int
    ) -> float:
        if error is not None and error.retry_after is not None:
            return min(error.retry_after, self.llm_retry_max_delay)

        delay = self.llm_retry_base_delay * (2 ** (attempt - 1))
        return min(delay, self.llm_retry_max_delay)

    async def _call_llm(
        self, 
        messages: List[Dict[str, Any]], 
        tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Description:
        ---------------
            Вызывает LLM API с заданными сообщениями и инструментами.
            
        Args:
        ---------------
            messages: Список сообщений диалога
            tools: Список инструментов
            
        Returns:
        ---------------
            Dict[str, Any]: Ответ от LLM
            
        Raises:
        ---------------
            Exception: При ошибке вызова API
        """
        try:
            logger.debug("Отправка запроса к LLM")
            
            # Формируем запрос в зависимости от типа API
            if self.llm_config.is_openai_compatible:
                payload = {
                    "model": self.llm_config.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": self.llm_config.temperature,
                    "max_tokens": self.llm_config.max_tokens
                }
            else:
                # Для API, не совместимых с OpenAI
                payload = {
                    "model": self.llm_config.model,
                    "prompt": self._format_messages_for_custom_llm(messages),
                    "tools": tools,
                    "temperature": self.llm_config.temperature,
                    "max_tokens": self.llm_config.max_tokens
                }
            
            # Используем таймаут из конфигурации (изменено)
            response = await self.http_client.post(
                self.llm_config.api_url,
                json=payload,
                timeout=self.llm_call_timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.debug("Получен успешный ответ от LLM")
                
                # Обработка ответа в зависимости от типа API
                if self.llm_config.is_openai_compatible:
                    choices = result.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        return message
                    
                    return {"content": "Получен пустой ответ от LLM"}

                # Для API, не совместимых с OpenAI
                return self._parse_custom_llm_response(result)
            
            retry_after = self._parse_retry_after(
                response.headers.get("Retry-After")
            )

            raise LLMHTTPError(
                status_code=response.status_code,
                response_text=response.text,
                retry_after=retry_after
            )
                
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Таймаут LLM: {repr(e)}") from e
        
        except httpx.RequestError as e:
            raise LLMTransportError(f"Сетевая ошибка LLM: {repr(e)}") from e
        
        except LLMError:
            raise
        
        except Exception as e:
            raise LLMError(f"Ошибка при обращении к LLM: {repr(e)}")
        
    async def _call_llm_with_retries(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        context: str = "LLM call"
    ) -> Dict[str, Any]:
        max_attempts = self.llm_max_retries + 1

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._call_llm(messages, tools)

            except LLMHTTPError as e:
                can_retry = e.status_code in self.llm_retryable_http_statuses

                if not can_retry or attempt >= max_attempts:
                    logger.error(
                        f"{context}: LLM HTTP error без дальнейших повторов "
                        f"(attempt {attempt}/{max_attempts}): {e}"
                    )
                    raise

                delay = self._get_llm_retry_delay(e, attempt)

                logger.warning(
                    f"{context}: LLM HTTP {e.status_code}. "
                    f"Повтор через {delay:.1f} сек. "
                    f"Попытка {attempt}/{max_attempts}"
                )

                await asyncio.sleep(delay)

            except (LLMTimeoutError, LLMTransportError) as e:
                if attempt >= max_attempts:
                    logger.error(
                        f"{context}: LLM transport error без дальнейших повторов "
                        f"(attempt {attempt}/{max_attempts}): {e}"
                    )
                    raise

                delay = self._get_llm_retry_delay(None, attempt)

                logger.warning(
                    f"{context}: временная ошибка LLM: {e}. "
                    f"Повтор через {delay:.1f} сек. "
                    f"Попытка {attempt}/{max_attempts}"
                )

                await asyncio.sleep(delay)
    
    def _format_messages_for_custom_llm(
        self, 
        messages: List[Dict[str, Any]]
    ) -> str:
        """
        Description:
        ---------------
            Форматирует сообщения для пользовательской LLM.
            
        Args:
        ---------------
            messages: Список сообщений диалога
            
        Returns:
        ---------------
            str: Отформатированный текст промпта
        """
        formatted_messages = []
        
        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")
            
            if role == "system":
                formatted_messages.append(f"### Инструкции:\n{content}")
            elif role == "user":
                formatted_messages.append(f"### Пользователь:\n{content}")
            elif role == "assistant":
                formatted_messages.append(f"### Ассистент:\n{content}")
            elif role == "tool":
                tool_call_id = message.get("tool_call_id", "")
                formatted_messages.append(
                    f"### Результат инструмента ({tool_call_id}):\n{content}"
                )
        
        formatted_messages.append("### Ассистент:")
        return "\n\n".join(formatted_messages)
    
    def _parse_custom_llm_response(
            self, 
            response: Dict[str, Any]
        ) -> Dict[str, Any]:
            """
            Description:
            ---------------
                Обрабатывает ответ от пользовательской LLM.
                
            Args:
            ---------------
                response: Ответ от API
                
            Returns:
            ---------------
                Dict[str, Any]: Обработанный ответ
            """
            if "response" in response:
                content = response["response"]
                
                # Проверяем, есть ли вызовы инструментов в тексте
                tool_calls = []
                
                # Ищем паттерны вызова инструментов в тексте
                tool_call_pattern = (
                    r'Вызов инструмента (\w+)\s*с аргументами\s*\{([^}]*)\}'
                )
                matches = re.findall(tool_call_pattern, content)
                
                for i, (tool_name, args_str) in enumerate(matches):
                    try:
                        # Преобразуем строку аргументов в словарь JSON
                        args_dict = {}
                        for arg_pair in args_str.split(','):
                            if ':' in arg_pair:
                                key, value = arg_pair.split(':', 1)
                                key = key.strip().strip('"\'')
                                value = value.strip().strip('"\'')
                                args_dict[key] = value
                        
                        tool_calls.append({
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args_dict)
                            }
                        })
                    except Exception:
                        pass
                
                return {
                    "content": content,
                    "tool_calls": tool_calls
                }
            
            return {"content": "Не удалось обработать ответ от LLM"}
        
    async def chat_loop(self):
        """
        Description:
        ---------------
            Запускает интерактивный цикл чата с пользователем.
            
        Raises:
        ---------------
            Exception: При ошибке обработки запроса
            
        Examples:
        ---------------
            >>> await client.chat_loop()
            MCP Client запущен!
            Введите запрос или 'quit' для выхода.
        """
        print("\nMCP Client запущен!")
        print("Введите запрос или 'quit' для выхода.")

        while True:
            try:
                query = input("\nЗапрос: ").strip()

                if query.lower() in ('quit', 'exit', 'выход'):
                    break

                print("Обработка запроса...")
                response = await self.process_query(query)
                #print("\nФинальный ответ: " + response)

            except Exception as e:
                print(f"\nОшибка: {str(e)}")
                if "--debug" in sys.argv:
                    import traceback
                    traceback.print_exc()

    async def cleanup(self):
        """
        Description:
        ---------------
            Освобождает ресурсы клиента.
        """
        # Закрытие MCP-серверов в ОБРАТНОМ порядке
        for runtime in reversed(list(self.server_runtimes.values())):
            try:
                logger.info(f"Закрытие MCP-сервера {runtime.name}...")

                if runtime.http_client is not None:
                    await runtime.http_client.close()

                if runtime.exit_stack is not None:
                    await runtime.exit_stack.aclose()

                logger.info(f"MCP-сервер {runtime.name} отключён")

            except asyncio.CancelledError as e:
                # На shutdown anyio/mcp иногда пробрасывает CancelledError.
                # Для штатного завершения приложения лучше не валить весь shutdown.
                logger.warning(
                    f"Закрытие MCP-сервера {runtime.name} было отменено: {e!r}"
                )

            except BaseException as e:
                logger.exception(
                    f"Ошибка при закрытии MCP-сервера {runtime.name}: {type(e).__name__}: {e!r}"
                )

            finally:
                runtime.session = None
                runtime.http_client = None
                runtime.exit_stack = None

        # Очистка реестров
        self.server_runtimes.clear()
        self.tool_registry.clear()
        self.available_tools.clear()

        # Закрытие HTTP-клиента LLM
        try:
            await self.http_client.aclose()
        except Exception as e:
            logger.warning(f"Ошибка при закрытии HTTP-клиента LLM: {e!r}")

        # Очистка "мусора"
        await asyncio.sleep(0.5)
        gc.collect()
        await asyncio.sleep(0)

        logger.info("Все MCP-серверы отключены")

    def _get_or_create_session(
        self,
        session_id: str,
        system_message: str
    ) -> SessionMemory:
        """
        Description:
        ---------------
            Возвращает существующую сессию или создаёт новую.
            
        Args:
            ---------------
            session_id (str): Уникальный идентификатор сессии
            system_message (str): Системное сообщение для LLM
        """
        session = self.sessions.get(session_id)

        if session is None:
            session = SessionMemory(
                messages=[{"role": "system", "content": system_message}]
            )
            self.sessions[session_id] = session
        else:
            session.last_seen = time.time()

            # Системное сообщение держим актуальным:
            if not session.messages:
                session.messages = [{"role": "system", "content": system_message}]
            elif session.messages[0].get("role") == "system":
                session.messages[0] = {"role": "system", "content": system_message}
            else:
                session.messages.insert(0, {"role": "system", "content": system_message})

        return session


    def _trim_session_messages(self, session: SessionMemory) -> None:
        """
        Description:
            ---------------
            Обрезает историю, оставляя system + последние N сообщений.

        Args:
            session (SessionMemory): Текущая сессия
        """
        if not session.messages:
            return

        if session.messages[0].get("role") == "system":
            system_message = session.messages[0]
            body = session.messages[1:]
            if len(body) > self.max_history_messages:
                body = body[-self.max_history_messages:]
            session.messages = [system_message] + body
        else:
            if len(session.messages) > self.max_history_messages:
                session.messages = session.messages[-self.max_history_messages:]

    def _get_or_create_state(self, session_id: str) -> SessionState:
        """
        Description:
        ---------------
            Возвращает существующий статус сессии или создаёт новый.
            
        Args:
            ---------------
            session_id (str): Уникальный идентификатор сессии
        """
        state = self.session_states.get(session_id)

        if state is None:
            state = SessionState()
            self.session_states[session_id] = state

        state.last_seen = time.time()
        return state


    def get_session_state(self, session_id: str) -> Optional[SessionState]:
        """
        Description:
        ---------------
            Возвращает текущий статус сессии.
        
        Args:
            ---------------
            session_id (str): Уникальный идентификатор сессии
        """
        return self.session_states.get(session_id)

    def clear_session(self, session_id: str) -> None:
        """
        Description:
        ---------------
            Полностью очищает память сессии.

        Args:
        ---------------
            session_id (str): Уникальный идентификатор сессии
        """
        self.sessions.pop(session_id, None)
        self.session_states.pop(session_id, None)

    def _extract_agent_status(self, text: str) -> AgentStatus:
        """
        Description:
        ---------------
            Ищет маркеры состояний и возвращает статус сессии.

        Args:
        ---------------
            text (str): Текст ИИ-агента
        """
        if "[AGENT_STATUS=WAITING_USER]" in text:
            return AgentStatus.WAITING_USER
        if "[AGENT_STATUS=CONTINUE]" in text:
            return AgentStatus.RUNNING
        if "[AGENT_STATUS=DONE]" in text:
            return AgentStatus.DONE
        return None
    
    def _strip_agent_markers(self, text: str) -> str:
        """
        Description:
        ---------------
            Очищает маркеры состояний из текста.

        Args:
        ---------------
            text (str): Текст ИИ-агента
        """
        for marker in (
            "[AGENT_STATUS=WAITING_USER]",
            "[AGENT_STATUS=CONTINUE]",
            "[AGENT_STATUS=DONE]",
        ):
            text = text.replace(marker, "")
        return text.strip()
    
    def _finish_with_error(
        self,
        state: SessionState,
        final_text: list[str],
        message: str,
        *,
        log_exception: bool = False
    ) -> None:
        state.status = AgentStatus.ERROR
        state.last_error = message

        if log_exception:
            logger.exception(message)
        else:
            logger.error(message)

        final_text.clear()
        final_text.append(message)


def load_config(config_path: str) -> Tuple[List[ServerConfigType], LLMConfigType]:
    """
    Description:
    ---------------
        Загружает конфигурацию из файла JSON или YAML.
        
    Args:
    ---------------
        config_path (str): Путь к файлу конфигурации
        
    Returns:
    ---------------
        Tuple[List[ServerConfigType], LLMConfigType]: Конфигурации серверов и LLM
        
    Raises:
        ImportError: Если требуется YAML, но библиотека не установлена
        ValueError: Если формат файла не поддерживается
        Exception: При ошибке загрузки конфигурации
        
    Examples:
        >>> server_configs, llm_config = load_config("config.json")
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # === Загрузка MCP-серверов ===

        if "servers" in config:
            servers_data = config.get("servers") or []
        elif "server" in config:
            # Обратная совместимость со старым конфигом
            servers_data = [config.get("server") or {}]
        else:
            raise ValueError("В конфиге нет ни 'servers', ни 'server'")

        if not isinstance(servers_data, list):
            raise ValueError("Поле 'servers' должно быть списком")

        server_configs: List[ServerConfigType] = []

        for index, server_data in enumerate(servers_data):
            if not isinstance(server_data, dict):
                raise ValueError(
                    f"Описание MCP-сервера #{index + 1} должно быть объектом"
                )

            connect_type = ServerConnectType(
                server_data.get("connect_type", ServerConnectType.EXECUTABLE)
            )

            name = server_data.get("name") or f"server_{index + 1}"
            alias = server_data.get("alias") or name

            executable = server_data.get("executable")

            # Небольшой фикс для macOS, если когда-нибудь пригодится
            if executable == "python" and sys.platform == "darwin":
                logger.info("Обнаружена macOS, меняем 'python' на 'python3'")
                executable = "python3"

            server_config = ServerConfigType(
                name=name,
                alias=alias,
                connect_type=connect_type,
                executable=executable,
                args=server_data.get("args", []),
                env=server_data.get("env", {}),
                host=server_data.get("host"),
                port=server_data.get("port"),
                enabled=server_data.get("enabled", True)
            )

            server_configs.append(server_config)

        if not server_configs:
            raise ValueError("Список MCP-серверов пуст")

        enabled_servers = [server for server in server_configs if server.enabled]

        if not enabled_servers:
            raise ValueError("Нет включённых MCP-серверов: enabled=true")

        # === Загрузка LLM ===

        llm_data = config.get("llm", {})

        api_key = llm_data.get("api_key")

        if not api_key:
            api_key = os.environ.get("LLM_API_KEY", "")

            if api_key:
                logger.info("Использую API-ключ LLM из переменной LLM_API_KEY")
            else:
                logger.warning("Не указан api_key для LLM")

        llm_config = LLMConfigType(
            api_url=llm_data.get("api_url", ""),
            api_key=api_key,
            model=llm_data.get("model", "default"),
            headers=llm_data.get("headers"),
            is_openai_compatible=llm_data.get("is_openai_compatible", True),
            max_tokens=llm_data.get("max_tokens", 1000),
            temperature=llm_data.get("temperature", 0.7),
            instructions=llm_data.get("instructions")
        )

        if not llm_config.api_url:
            raise ValueError("В конфиге LLM не указан api_url")

        return server_configs, llm_config

    except Exception as e:
        logger.error(f"Ошибка при загрузке конфигурации: {type(e).__name__}: {e!r}")
        raise