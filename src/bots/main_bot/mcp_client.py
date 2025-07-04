import os
import re
import sys
import json
import logging
import shutil
import asyncio
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from pathlib import Path
from contextlib import AsyncExitStack
from types import SimpleNamespace
from logging.handlers import RotatingFileHandler

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

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
        
        # Настройка для LLM
        self.llm_config = llm_config
        self.http_client = httpx.AsyncClient(headers=llm_config.headers)
        self.instructions = "Ты ассистент, задача которого помогать пользователю в решении его задач."
        self.available_tools = []
        
        # Настройки таймаутов
        self.tool_call_timeout = 300.0  # Таймаут для вызова инструментов
        self.llm_call_timeout = 300.0   # Таймаут для вызова LLM
        
    async def connect_to_server(self, server_config: ServerConfigType):
        """
        Description:
        ---------------
            Подключение к MCP серверу.
            
        Args:
        ---------------
            server_config: Конфигурация сервера
            
        Raises:
        ---------------
            FileNotFoundError: Если исполняемый файл не найден
            ValueError: Если неверный тип подключения или отсутствует 
                        обязательный параметр
        """
        self.server_name = server_config.name
        self.instructions = server_config.instructions
        logger.info(
            f"Подключение к серверу: {self.server_name}"
        )
        
        if server_config.connect_type == ServerConnectType.HTTP:
            if not server_config.host or not server_config.port:
                raise ValueError(
                    "Для типа подключения HTTP необходимо указать хост и порт сервера"
                )
            
            logger.info(f"Подключение к HTTP серверу: {server_config.host}:{server_config.port}")
            
            # Создаем HTTP-клиент
            self.mcp_client = MCPHttpClient(server_config.host, server_config.port)
            await self.mcp_client.initialize()
            
            # Получаем список доступных инструментов
            response = await self.mcp_client.list_tools()
            self.available_tools = response.tools
            
        elif server_config.connect_type == ServerConnectType.EXECUTABLE:
            if not server_config.executable:
                # Автоматически определяем Python
                logger.info(
                    "Исполняемый файл не указан, пытаемся определить "
                    "Python автоматически"
                )
                server_config.executable = find_python_executable()
                
            # Проверяем, существует ли исполняемый файл
            executable_path = shutil.which(server_config.executable)
            if not executable_path:
                raise FileNotFoundError(
                    f"Исполняемый файл не найден: {server_config.executable}"
                )
            
            logger.info(f"Исполняемый файл найден: {executable_path}")
            
            # Настройка переменных окружения для корректной работы с Unicode
            env = server_config.env or {}
            env.update({
                'PYTHONIOENCODING': 'utf-8',
                'PYTHONUTF8': '1',
                'PYTHONLEGACYWINDOWSSTDIO': '0',
                'LC_ALL': 'C.UTF-8',
                'LANG': 'C.UTF-8'
            })
                
            server_params = StdioServerParameters(
                command=executable_path,
                args=server_config.args,
                env=env  # Используем обновленные переменные окружения
            )
            
            logger.info(
                f"Запуск сервера: {executable_path} "
                f"{' '.join(server_config.args)}"
            )
            try:
                stdio_transport = await self.exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f"Ошибка при запуске сервера: {str(e)}\n"
                    f"Проверьте путь к исполняемому файлу и аргументы."
                )
            
            # Инициализация сессии для stdio
            self.stdio, self.write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(self.stdio, self.write)
            )
            await self.session.initialize()

            # Получаем список доступных инструментов
            response = await self.session.list_tools()
            self.available_tools = response.tools
                
        elif server_config.connect_type == ServerConnectType.MCP_LOOKUP:
            if not server_config.name:
                raise ValueError(
                    "Для типа подключения MCP_LOOKUP необходимо "
                    "указать имя сервера"
                )
                
            # Поиск сервера в конфигурации Claude Desktop или MCP-клиента
            config_paths = [
                Path.home() / ".config" / "mcp" / "config.json"
            ]
            
            server_found = False
            for config_path in config_paths:
                if config_path.exists():
                    logger.info(f"Найдена конфигурация MCP: {config_path}")
                    try:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                            
                        if ("mcpServers" in config and 
                                server_config.name in config["mcpServers"]):
                            server_info = config["mcpServers"][
                                server_config.name
                            ]
                            command = server_info.get("command")
                            
                            # Проверяем наличие команды
                            command_path = shutil.which(command)
                            if not command_path:
                                logger.warning(
                                    f"Команда '{command}' не найдена, "
                                    f"пытаемся определить Python автоматически"
                                )
                                command = find_python_executable()
                            
                            server_params = StdioServerParameters(
                                command=command,
                                args=server_info.get("args", []),
                                env=server_info.get("env", {})
                            )
                            
                            logger.info(
                                f"Используется сервер из конфигурации: "
                                f"{server_config.name}"
                            )
                            try:
                                stdio_transport = (
                                    await self.exit_stack.enter_async_context(
                                        stdio_client(server_params)
                                    )
                                )
                                # Инициализация сессии для stdio
                                self.stdio, self.write = stdio_transport
                                self.session = await self.exit_stack.enter_async_context(
                                    ClientSession(self.stdio, self.write)
                                )
                                await self.session.initialize()

                                # Получаем список доступных инструментов
                                response = await self.session.list_tools()
                                self.available_tools = response.tools
                                server_found = True
                                break
                            except FileNotFoundError as e:
                                logger.error(
                                    f"Ошибка при запуске сервера из "
                                    f"конфигурации: {str(e)}"
                                )
                    except Exception as e:
                        logger.error(
                            f"Ошибка при чтении конфигурации {config_path}: {e}"
                        )
                        
            if not server_found:
                raise ValueError(
                    f"Сервер с именем '{server_config.name}' не найден "
                    f"в конфигурации MCP или не удалось запустить"
                )
                
        else:
            raise ValueError(
                f"Неизвестный тип подключения: {server_config.connect_type}"
            )
        
        logger.info(
            f"Подключено к серверу. Доступные инструменты: "
            f"{[tool.name for tool in self.available_tools]}"
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
        
    async def process_query(self, query: str) -> str:
        """
        Description:
        ---------------
            Обработка запроса с использованием LLM и доступных инструментов.
            
        Args:
        ---------------
            query: Текст запроса от пользователя
            
        Returns:
        ---------------
            str: Результат обработки запроса
            
        Raises:
        ---------------
            Exception: При ошибке обработки запроса
        """
        logger.info(f"Начало обработки запроса: '{query}'")
        final_text = []
        
        try:
            # Составляем системное сообщение с инструкциями
            system_message = self._create_system_message()
            
            # Инициализируем диалог
            messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": query}
            ]
            logger.debug(f"Сообщения для LLM: {messages}")
            
            # Преобразуем инструменты в формат для LLM
            tools = self._format_tools_for_llm()
            
            # Основной цикл обработки
            max_iterations = 10  # Ограничиваем количество итераций
            
            for i in range(max_iterations):
                logger.info(f"Итерация {i+1}/{max_iterations}")
                
                try:
                    # Вызываем LLM с таймаутом
                    
                    llm_response = await asyncio.wait_for(
                        self._call_llm(messages, tools),
                        timeout=self.llm_call_timeout
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
                        # Если нет вызовов инструментов, завершаем обработку
                        logger.info("Нет вызовов инструментов, завершаем обработку")
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
                        
                        logger.info(f"Вызов инструмента: {tool_name}")
                        
                        try:
                            # Парсим аргументы
                            arguments = json.loads(function.get("arguments", "{}"))
                            logger.debug(f"Аргументы инструмента {tool_name}: {arguments}")
                            
                            # Вызываем инструмент через соответствующий клиент с таймаутом
                            if hasattr(self, "mcp_client"):
                                # Для HTTP-клиента
                                result = await asyncio.wait_for(
                                    self.mcp_client.call_tool(tool_name, arguments),
                                    timeout=self.tool_call_timeout
                                )
                            else:
                                # Для stdio-клиента
                                result = await asyncio.wait_for(
                                    self.session.call_tool(tool_name, arguments),
                                    timeout=self.tool_call_timeout
                                )
                                logger.debug(f"Ответ от stdio-клиента: {result}")
                            
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
                    if i == max_iterations - 1 and tool_results:
                        try:
                            final_response = await asyncio.wait_for(
                                self._call_llm(messages, tools),
                                timeout=self.llm_call_timeout
                            )
                            final_content = final_response.get("content", "")
                            if final_content:
                                final_text.append(f"\nИтоговый ответ: {final_content}")
                        except asyncio.TimeoutError:
                            logger.error("Таймаут при получении финального ответа")
                            final_text.append("\nТаймаут при получении финального ответа")
                        except Exception as e:
                            logger.error(f"Ошибка при получении финального ответа: {e}")
                            
                except asyncio.TimeoutError:  # Обработка таймаута LLM
                    error_message = f"Таймаут LLM на итерации {i+1}"
                    logger.error(error_message)
                    final_text.append(f"\n{error_message}")
                    break
                    
                except Exception as e:
                    error_message = f"Ошибка на итерации {i+1}: {str(e)}"
                    logger.error(error_message)
                    final_text.append(f"\n{error_message}")
                    break
            
            result = final_text[len(final_text) - 1] if len(final_text) > 0 else "Пустой ответ."#"\n".join([text for text in final_text if text])
            logger.info(f"Завершение обработки запроса. Результат: {result}")
            return result
            
        except Exception as e:
            error_message = f"Критическая ошибка при обработке запроса: {str(e)}"
            logger.error(error_message)
            return error_message
    
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
        
        return f"{self.instructions}\n\nУ тебя есть доступ к следующим инструментам:\n{self._tools_description()}"
    
    def _tools_description(self) -> List[Dict[str, Any]]:
        """
        Description:
        ---------------
            Составляет описание инструментов.
            
        Returns:
        ---------------
            List[Dict[str, Any]]: Список описания инструментов
        """        
        tools_description = [
            {
                'name': tool.name,
                'description': re.sub(r' {2,}', ' ', re.sub(r'\n|\t|-{5,}', ' ', tool.description)).strip(),
                'inputSchema': tool.inputSchema
            }
            for tool in self.available_tools
        ]

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
        
        for tool in self.available_tools:
            # Преобразуем схему инструмента в формат, понятный LLM
            input_schema = tool.inputSchema or {}
            
            function_spec = {
                "name": tool.name,
                "description": tool.description,
                "parameters": input_schema
            }
            
            llm_tools.append({
                "type": "function",
                "function": function_spec
            })
            
        return llm_tools
    
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
                else:
                    # Для API, не совместимых с OpenAI
                    return self._parse_custom_llm_response(result)
            else:
                error_message = (
                    f"Ошибка при вызове LLM: "
                    f"{response.status_code} - {response.text}"
                )
                logger.error(error_message)
                return {"content": error_message}
                
        except Exception as e:
            error_message = f"Ошибка при обращении к LLM: {str(e)}"
            logger.error(error_message)
            return {"content": error_message}
    
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
        try:
            await self.http_client.aclose()
        except Exception:
            pass  # Игнорируем ошибки закрытия HTTP-клиента
        
        if hasattr(self, "mcp_client"):
            try:
                await self.mcp_client.close()
            except Exception:
                pass  # Игнорируем ошибки закрытия MCP-клиента
        
        if hasattr(self, "exit_stack"):
            try:
                await self.exit_stack.aclose()
            except Exception as e:
                logger.error(f"Ошибка при закрытии соединения: {e}")
            finally:
                # Гарантируем очистку ресурсов
                self.exit_stack = None

        logger.info(f"Соединение с {self.server_name} закрыто")


def load_config(config_path: str) -> Tuple[ServerConfigType, LLMConfigType]:
    """
    Description:
    ---------------
        Загружает конфигурацию из файла JSON или YAML.
        
    Args:
    ---------------
        config_path: Путь к файлу конфигурации
        
    Returns:
    ---------------
        Tuple[ServerConfig, LLMConfig]: Конфигурации для сервера и LLM
        
    Raises:
        ImportError: Если требуется YAML, но библиотека не установлена
        ValueError: Если формат файла не поддерживается
        Exception: При ошибке загрузки конфигурации
        
    Examples:
        >>> server_config, llm_config = load_config("config.json")
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Загрузка конфигурации сервера
        server_config_data = config.get('server', {})
        server_connect_type = ServerConnectType(
            server_config_data.get('connect_type', 'executable')
        )
        
        if server_connect_type == ServerConnectType.HTTP:
            server_config = ServerConfigType(
                connect_type=server_connect_type,
                host=server_config_data.get('host', '127.0.0.1'),
                port=server_config_data.get('port', 8080)
            )
        else:
            # Обработка пути к исполняемому файлу
            executable = server_config_data.get('executable')
            if executable == "python" and sys.platform == "darwin":
                # На macOS автоматически используем python3
                logger.info("Обнаружена macOS, меняем 'python' на 'python3'")
                executable = "python3"
            
            server_config = ServerConfigType(
                connect_type=server_connect_type,
                name=server_config_data.get('name'),
                executable=executable,
                args=server_config_data.get('args', []),
                env=server_config_data.get('env', {}),
                instructions=server_config_data.get('instructions')
            )
        
        # Загрузка конфигурации LLM
        llm_config_data = config.get('llm', {})
        
        # Проверяем наличие API ключа в переменных окружения
        api_key = llm_config_data.get('api_key')
        if not api_key:
            """
            api_key = os.environ.get("LLM_API_KEY", "")
            if api_key:
                logger.info("Использую API ключ из переменной окружения LLM_API_KEY")
            """
            logger.warning("Не указан api key в конфигурации LLM")
        
        llm_config = LLMConfigType(
            api_url=llm_config_data.get('api_url', ''),
            api_key=api_key,
            model=llm_config_data.get('model', 'default'),
            is_openai_compatible=llm_config_data.get(
                'is_openai_compatible', True
            ),
            max_tokens=llm_config_data.get('max_tokens', 1000),
            temperature=llm_config_data.get('temperature', 0.7)
        )
        
        return server_config, llm_config
    
    except Exception as e:
        logger.error(f"Ошибка при загрузке конфигурации: {e}")
        raise