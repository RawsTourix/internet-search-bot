# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.6

## 0. Назначение документа

Этот документ фиксирует развитие архитектуры памяти, рабочего пространства и runtime ИИ-агента после перехода на JSON-протокол, динамические MCP-инструменты и разделение контекста.

Главная цель:

```text
Агент должен уметь выполнять длинные задачи,
не терять рабочий контекст при WAITING_USER,
не засорять LLM-контекст завершёнными tool results,
и постепенно перейти к долговременной памяти через PostgreSQL/RAG.
```

Документ описывает:

- текущую логику v0.3;
- промежуточные обновления ветки `feature` перед v0.4:
  - `v0.3-agent-protocol-foundation`;
  - `v0.3-agent-memory-runtime`;
  - `v0.3-cycle-memory`;
  - `v0.3-progress-events`;
  - `v0.3-progress-events refinements`;
  - `v0.3-mcp-server-manager`;
  - `v0.3-prompt-optimization`;
  - `v0.3-final-processing-pipeline`;
  - `v0.3-final-processing-progress`;
- итог v0.3 и границу feature freeze перед v0.4;
- v0.4: agent workspace, storage foundation, LLM-compaction, file artifacts, DAG planning и input runtime;
- v0.5: PostgreSQL, lazy indexing, pgvector и RAG для памяти и workspace;
- v0.6: микросервисную архитектуру, Redis/arq, workers и distributed runtime;
- принципы result/cycle compaction;
- работу с файлами и версиями артефактов;
- `InputBatch` и `CycleInbox`;
- будущие RAG-инструменты и автоматизируемое DAG-исполнение.

---

# Часть I. Базовая модель памяти

## 1. Почему старая модель была проблемной

Старая модель, где `session.messages` одновременно был:

- историей общения;
- рабочим контекстом агента;
- логом инструментов;
- хранилищем tool results;
- трассировкой выполнения;

приводила к загрязнению контекста.

Проблемы:

```text
1. Старые tool results попадали в следующие запросы.
2. Огромные ответы инструментов раздували контекст.
3. LLM могла путаться между текущей задачей и прошлой выдачей.
4. final audit мог видеть лишние данные и галлюцинировать.
5. Невозможно было гибко управлять памятью.
```

Поэтому память разделяется на несколько слоёв.

---

## 2. Основные слои памяти

### 2.1. `messages_for_llm`

Локальный рабочий список сообщений, который отправляется в LLM в текущей итерации.

Это **видимый контекст агента**.

В него могут входить:

- system prompt;
- краткая session memory;
- текущий user request;
- текущие tool calls/tool results;
- текущий `pending_cycle`, если задача не завершена.

Важно:

```text
messages_for_llm не является долговременным хранилищем.
```

---

### 2.2. `session_dialog_memory` / `dialog_turns`

Краткая история завершённых обращений.

Формат:

```text
user_request → final_answer → status → tools_used
```

Туда не должны попадать:

- role=tool;
- assistant tool_calls;
- большие tool results;
- running/continue AgentAction;
- browser snapshots;
- HTML;
- внутренние repair/audit-сообщения.

---

### 2.3. `cycle_trace`

Подробная трассировка текущего агентного цикла. Ранее в коде использовалось имя `task_trace`; после refactor используется `cycle_trace`.

Туда попадают:

- LLM responses;
- tool calls;
- tool results;
- tool errors;
- progress events;
- context warnings;
- compaction events;
- infrastructure errors.

Trace нужен для debug, архивов и будущей памяти, но **не должен автоматически попадать в LLM-контекст**.

---

### 2.4. `archival_logs`

Полное хранилище циклов в JSON-файлах.

Это переходный формат перед PostgreSQL.

Архив нужен для:

- отладки;
- последующего анализа;
- будущей миграции в БД;
- восстановления старых agent cycles;
- будущих RAG-инструментов.

---

### 2.5. `pending_cycle`

Снимок незавершённого агентного цикла.

Используется, когда агент дошёл до `WAITING_USER`.

`WAITING_USER` не завершает цикл. Он только ставит его на паузу.

---

# Часть II. v0.3 — pending cycle memory

## 3. Главная идея v0.3

v0.3 не вводит полноценный task manager.

Вместо этого вводится:

```text
AgentCycleSnapshot / pending_cycle
```

`pending_cycle` — один сохранённый незавершённый агентный цикл.

Агентный цикл начинается с пользовательского запроса и завершается только одним из терминальных статусов:

```text
DONE
ERROR
CANCELLED
```

`WAITING_USER` — не терминальный статус.

---

## 4. Что такое agent cycle

Один agent cycle может включать:

- исходный запрос пользователя;
- много LLM-итераций;
- много вызовов инструментов;
- много результатов инструментов;
- несколько `WAITING_USER`-пауз;
- несколько ответов пользователя;
- финальный ответ или ошибку.

Пример:

```text
User request
→ RUNNING
→ tool calls
→ WAITING_USER
→ user reply
→ RUNNING
→ tool calls
→ WAITING_USER
→ user reply
→ RUNNING
→ DONE
```

Всё это один `cycle_id`.

---

## 5. `AgentCycleSnapshot`

Рекомендуемая структура:

```python
@dataclass
class AgentCycleSnapshot:
    cycle_id: str
    original_user_request: str
    messages_for_llm: List[Dict[str, Any]]
    task_trace: List[Dict[str, Any]]

    status: str = "waiting_user"
    waiting_question: str | None = None

    working_summary: str = ""
    working_state: Dict[str, Any] = field(default_factory=dict)

    tools_used: List[str] = field(default_factory=list)
    progress_events: List[Dict[str, Any]] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
```

---

## 6. `working_summary`

`working_summary` — человекочитаемое краткое описание текущего незавершённого цикла.

Нужно для будущей context compaction.

Пример:

```text
Пользователь попросил перевести память агента на v0.3. Уже реализованы dialog_turns, messages_for_llm, task_trace и архивирование. Сейчас обсуждается pending_cycle для WAITING_USER и будущая обработка больших tool results.
```

В v0.3 поле можно добавить с TODO, но полноценно использовать позже.

---

## 7. `working_state`

`working_state` — машинно-читаемое состояние текущей работы.

Пример:

```json
{
  "current_goal": "перевести память агента на v0.3-v0.4",
  "completed_actions": [
    "добавлен pending_cycle",
    "добавлен context budget",
    "добавлен архив agent_cycle_archive"
  ],
  "confirmed_actions": [],
  "rejected_actions": [],
  "modified_files": [],
  "archived_trace_refs": [],
  "large_result_refs": [],
  "pending_confirmation": null
}
```

`working_state` нужен, чтобы после compaction агент не потерял:

- подтверждённые действия;
- отклонённые действия;
- изменённые файлы;
- ID больших результатов;
- ссылки на архивированные tool outputs;
- текущую цель;
- pending confirmation.

---

## 8. Правила `WAITING_USER`

При `WAITING_USER`:

```text
1. Сохранить messages_for_llm в pending_cycle.
2. Сохранить task_trace.
3. Сохранить waiting_question.
4. Сохранить tools_used/progress_events.
5. Не создавать dialog_turn.
6. Не очищать рабочий контекст.
```

При следующем сообщении пользователя:

```text
1. Если pending_cycle есть — продолжить его.
2. Добавить user_reply_during_waiting_user.
3. Использовать тот же cycle_id.
4. Не строить новый messages_for_llm.
```

Если пользователь сменил тему во время `WAITING_USER`, runtime не должен пытаться семантически угадывать смену темы. Новое сообщение всё равно добавляется в текущий `pending_cycle`, а LLM сама решает, что делать.

---

## 9. DONE / ERROR

При `DONE`:

```text
1. Создать compact dialog_turn.
2. В dialog_turn.user_request записать original_user_request.
3. Очистить pending_cycle.
4. Архивировать полный цикл.
```

При `ERROR`:

```text
1. Сохранить last_error_cycle.
2. Очистить pending_cycle.
3. Архивировать полный цикл.
```

При инфраструктурной ошибке можно сохранить цикл как interrupted/resumable, если такая логика уже реализована.

---

# Часть III. v0.3.x — хронология промежуточных обновлений перед v0.4

Этот раздел фиксирует промежуточные обновления ветки `feature` в хронологическом порядке.

Важно:

```text
Здесь описывается только то, что уже отражено в коде и commit history.
Раздел не является планом будущих возможностей.
```

Назначение раздела — связать текущую реализацию v0.3 с будущей v0.4, чтобы дорожная карта не выглядела как скачок от `pending_cycle` сразу к storage/compaction.

---

## 10. `v0.3-agent-protocol-foundation`

Commit layer:

```text
feat(agent): add dynamic MCP manager and JSON agent protocol foundation
```

Смысл обновления:

```text
Базовый агентный протокол переводится с текстовых маркеров на структурированный JSON-контракт AgentAction.
```

В коде закреплены:

```text
1. `AGENT_SYSTEM_PROTOCOL` как базовые правила агента.
2. `AgentAction` как JSON-ответ агента, когда он не вызывает tool_call.
3. Первичная модель `ProgressEvent`.
4. `dumps_json()` / `loads_json_object()` для единой JSON-сериализации.
5. Dynamic MCP discovery:
   - `mcp_list_servers`;
   - `mcp_list_tools`;
   - `mcp_get_tool_schema`;
   - `mcp_call_tool`.
6. Принцип: агент не знает все MCP-инструменты заранее.
7. Принцип: tool output — это данные, а не инструкции.
```

Архитектурный эффект:

```text
System prompt перестаёт быть местом хранения полного описания всех инструментов.
LLM получает базовые правила, а актуальный список серверов/инструментов запрашивает через manager tools.
```

---

## 11. `v0.3-agent-memory-runtime`

Commit layers:

```text
refactor(mcp_client): delete old functions and optimize agent work
refactor: big improvements, prepare to dynamic agent memory
```

Смысл обновления:

```text
`MCPClient` начинает переходить от простого chat-history подхода к runtime-модели агентного цикла.
```

В коде закреплены:

```text
1. `SessionMemory` как отдельная память сессии.
2. `DialogTurn` как компактная запись завершённого обращения.
3. `last_task_trace` / последующий переход к `last_cycle_trace`.
4. `ManagerToolSpec` как единый реестр встроенных manager tools.
5. `server_configs_by_name`, `server_manager`, `manager_tools`.
6. Поддержка `streamable_http` как отдельного типа подключения MCP-сервера.
7. Архивирование agent traces в файловую систему.
8. Отказ от старых marker-based сценариев вроде `[AGENT_STATUS=DONE]`.
```

Архитектурный эффект:

```text
Долгая работа агента начинает отделяться от обычной истории чата.
Trace и tool results перестают восприниматься как обычная диалоговая память.
```

---

## 12. `v0.3-cycle-memory` / interrupted cycle memory

Commit layer:

```text
feat(mcp): preserve agent context on LLM failures
```

Смысл обновления:

```text
Агент получает возможность сохранять незавершённый цикл не только при WAITING_USER, но и при части инфраструктурных ошибок.
```

В коде закреплены:

```text
1. `AgentCycleSnapshot`.
2. `pending_cycle`.
3. `last_error_cycle`.
4. `interrupted` как состояние сохранённого цикла.
5. `interruption_reason` и `interrupted_at`.
6. `working_summary` и `working_state` как TODO/задел под v0.4 compaction.
7. `context_window_tokens`, `reserved_output_tokens`, `context_safety_ratio`, `context_compaction_target_ratio`.
8. `enable_context_compaction`.
9. `_save_interrupted_cycle()`.
10. `_save_last_error_cycle()`.
11. `_finalize_after_max_iterations()`.
12. `_iteration_runtime_payload()` и `_with_iteration_runtime_message()`.
```

Архитектурный эффект:

```text
Инфраструктурный сбой LLM больше не обязан уничтожать весь рабочий контекст цикла.
Runtime может сохранить состояние задачи и дать пользователю возможность продолжить позже.
```

---

## 13. Terminology cleanup: `task_*` → `cycle_*`

Commit layer:

```text
refactor: task_id → cycle_id, task_trace → cycle_trace, _archive_task_trace → _archive_agent_cycle
```

Смысл обновления:

```text
Терминология приводится к модели agent cycle.
```

В коде закреплены переименования:

```text
1. `task_id` → `cycle_id`.
2. `task_trace` → `cycle_trace`.
3. `last_task_trace` → `last_cycle_trace`.
4. `_archive_task_trace()` → `_archive_agent_cycle()`.
5. `AgentCycleSnapshot.task_trace` → `AgentCycleSnapshot.cycle_trace`.
```

Архитектурный эффект:

```text
Текущая единица выполнения называется agent cycle, а не task.
Это важно для будущего DAG/task planner: один user task может быть представлен как DAG внутри одного cycle.
```

---

## 14. `v0.3-progress-events`

Commit layer:

```text
base: v0.3-progress-events
```

Смысл обновления:

```text
Появляется live progress tracking без PostgreSQL, Redis и event bus.
```

В коде закреплены:

```text
1. Расширенный `ProgressEvent`:
   - `event_id`;
   - `created_at`;
   - `session_id`;
   - `cycle_id`;
   - `iteration`;
   - `target_tool_name`;
   - `severity`;
   - `visibility`;
   - `data`.
2. `progress_callback` в цепочке:
   - Telegram/Web adapter;
   - MessageProcessor;
   - API.call_agent;
   - MCPClient.process_query.
3. Gateway HTTP progress callback:
   - `progress_callback_url`;
   - allowlist `PROGRESS_CALLBACK_ALLOWED_PREFIXES`;
   - `make_http_progress_callback()`.
4. Telegram progress endpoint и редактирование status-message.
5. `progress_locale` через metadata сообщения.
6. Throttling и ограничение длины progress-текста для Telegram.
```

Архитектурный эффект:

```text
Пользователь видит ход выполнения текущего agent cycle почти в реальном времени,
а progress events сохраняются как часть состояния и trace.
```

Подробная логика этого слоя описана в следующей части документа.

---

## 15. `v0.3-progress-events` refinements

Этот слой объединяет несколько последовательных дополнений к progress tracking.

Commit layers:

```text
v0.3-progress-events: extend progress events with LLM retries and Telegram final delivery
v0.3-progress-events: preserve final runtime status in Telegram
v0.3-progress-events: classify non-retryable LLM HTTP errors
v0.3-progress-events: move progress messages out of mcp client
v0.3-progress-events: centralize localized progress fallbacks
v0.3-progress-events: unify progress event emission
```

В коде закреплены:

```text
1. LLM retry progress events:
   - `llm_retry`;
   - `llm_error`;
   - `infrastructure_error`.
2. `error_kind` и `can_resume` в `AgentResult`.
3. Telegram final delivery mode:
   - финальный ответ может отправляться новым сообщением;
   - status-message остаётся за progress callbacks.
4. Удаление захардкоженных финальных Telegram-статусов:
   - `TELEGRAM_FINAL_SUCCESS_STATUS_TEXT`;
   - `TELEGRAM_FINAL_ERROR_STATUS_TEXT`;
   - `final_prefix`.
5. Классификация LLM HTTP errors:
   - retryable HTTP errors → `infrastructure_interruption`, `can_resume=True`;
   - non-retryable HTTP errors → `llm_configuration_error`, `can_resume=False`.
6. `llm_http_non_retryable` вместо некорректного `Повторы исчерпаны. Попытка 1/5`.
7. Вынос progress-шаблонов в `src/agent/progress_messages.py`.
8. `PROGRESS_MESSAGE_DEFAULT_KWARGS` для локализованных fallback-значений.
9. `progress_key` и `progress_arg_map` в `ManagerToolSpec`.
10. Унифицированный helper `_emit_progress_event()`.
```

Архитектурный эффект:

```text
Progress layer становится отдельным локализованным слоем,
а Telegram server больше не должен сам придумывать терминальные статусы поверх runtime-status.
```

---

## 16. `v0.3-mcp-server-manager`

Commit layers:

```text
v0.3-mcp-server-manager: add lifecycle-aware MCP recovery and retry
v0.3-mcp-server-manager: handle MCP transport shutdowns without gateway failure
```

Смысл обновления:

```text
`MCPServerManager` становится lifecycle coordinator для MCP runtime.
```

В коде закреплены:

```text
1. Lifecycle-поля `MCPServerRuntime`:
   - `healthy`;
   - `reconnecting`;
   - `last_error`;
   - `connected_at`;
   - `generation`.
2. Настройки MCP lifecycle:
   - `mcp_startup_timeout`;
   - `mcp_transport_call_timeout`;
   - `mcp_reconnect_timeout`;
   - `mcp_runtime_close_timeout`;
   - `mcp_call_retries_after_recovery`;
   - `server_reconnect_locks`.
3. Ошибки manager-layer:
   - `MCPServerManagerError`;
   - `MCPToolNotFoundError`;
   - `MCPServerNotConnectedError`;
   - `MCPTransportLifecycleError`;
   - `MCPServerRecoveryError`;
   - `MCPToolCallFailedError`.
4. Lifecycle methods:
   - `resolve_tool_binding()`;
   - `get_runtime()`;
   - `mark_unhealthy()`;
   - `is_transport_lifecycle_error()`;
   - `supports_recovery()`;
   - `replace_runtime()`;
   - `recover_runtime()`;
   - `call_tool_once()`;
   - `call_tool_with_recovery()`.
5. `_call_registered_tool()` в `MCPClient` становится thin wrapper для real tools.
6. `MCPServerManager.call_tool()` выполняет lifecycle-aware вызов.
7. `list_servers()` показывает runtime-состояние:
   - `healthy`;
   - `reconnecting`;
   - `generation`;
   - `last_error`.
8. Обработка transport shutdown cases:
   - `Session terminated`;
   - `session closed`;
   - `connection attempts failed`;
   - internal `asyncio.CancelledError` от MCP transport.
9. MCP transport failure не должен пробивать Gateway как HTTP 500.
```

Архитектурный эффект:

```text
Внешний MCP-сервер может выключиться, перезапуститься или потерять session.
Agent Runtime должен получить управляемый tool error / recovery failure,
а не падать на уровне Gateway.
```

Подробная логика этого слоя описана в отдельной части `v0.3-mcp-server-manager`.

---

## 17. `v0.3-prompt-optimization`

Commit layers:

```text
v0.3-prompt-optimization: move client formatting to final audit
v0.3-prompt-optimization: apply delivery constraints to fallback answers
```

Смысл обновления:

```text
Surface-specific formatting выводится из system prompt и переносится в финальную обработку ответа.
```

В коде закреплены:

```text
1. `_delivery_constraints(client_type)` вместо `_client_instructions(client_type)` в system prompt.
2. `delivery_constraints` описывают поверхность вывода:
   - Telegram;
   - Web;
   - unknown.
3. `delivery_constraints.purpose = format_final_answer_only`.
4. Delivery constraints применяются в `_audit_final_answer()`.
5. Delivery constraints применяются к forced/fallback final answers через `_force_final_answer()`.
6. `_create_system_message()` больше не получает `client_type` и не добавляет client formatting rules в system prompt.
```

Архитектурный эффект:

```text
System prompt хранит базовые правила агента,
а ограничения Telegram/Web влияют только на форму финального ответа, не на факты и решения.
```

---

## 18. `v0.3-final-processing-pipeline`

Commit layer:

```text
v0.3-final-processing-pipeline: split final answer processing
```

Смысл обновления:

```text
Финальная обработка ответа разделяется на выбор режима, grounding и форматирование.
```

В коде закреплены:

```text
1. `FinalProcessingMode`:
   - `SKIP`;
   - `FORMAT_ONLY`;
   - `GROUNDED`;
   - `STRICT_GROUNDED`.
2. `FinalProcessingDecision`.
3. `_select_final_processing_mode()`.
4. `_trace_has_tool_errors()`.
5. `_trace_has_empty_tool_results()`.
6. `_build_final_evidence_pack()`.
7. `_format_final_answer()`.
8. `_ground_final_answer()`.
9. `_process_final_answer()`.
10. `_force_final_answer_from_evidence()`.
11. `final_processing_decision` trace event.
12. `final_processing_done` trace event.
```

Архитектурный эффект:

```text
Финальная стадия больше не является одним общим audit/polish-запросом.
Агент выбирает режим обработки по состоянию цикла, tool errors, пустым результатам, числу итераций и forced/fallback условиям.
```

Важно:

```text
FORMAT_ONLY отвечает за форму.
GROUNDED / STRICT_GROUNDED отвечают за сверку с собранными данными.
Факты берутся из evidence pack / cycle_trace, а не из собственных знаний модели.
```

---

## 19. `v0.3-final-processing-progress`

Commit layer:

```text
v0.3-progress-events: show final processing progress
```

Смысл обновления:

```text
Final processing pipeline связывается с progress layer.
```

В коде закреплены:

```text
1. Новый progress event type: `final_processing_started`.
2. User-friendly progress messages:
   - `final_processing_started`;
   - `final_processing_format_only`;
   - `final_processing_grounded`;
   - `final_processing_strict_grounded`.
3. `_final_processing_progress_key()`.
4. Progress event после `final_processing_decision` и до `_process_final_answer()`.
5. Технические детали режима сохраняются в `event.data`, но не показываются пользователю в тексте.
```

Архитектурный эффект:

```text
После последнего tool call пользователь видит, что агент не завис,
а готовит или проверяет финальный ответ.
```

Пример user-visible статуса:

```text
🔎 Проверяю финальный ответ по собранным данным…
```

---

# Часть IV. v0.3-progress-events — live progress tracking

## 20. Назначение v0.3-progress-events

`v0.3-progress-events` — промежуточная версия между текущей v0.3-логикой памяти и будущей v0.4.

Цель:

```text
Добавить простое рабочее отслеживание выполнения агентной задачи
без PostgreSQL, Redis, очередей и полноценной event bus архитектуры.
```

Эта версия должна дать пользователю видимый прогресс уже на MVP-этапе, но не усложнять архитектуру раньше времени.

Главная идея:

```text
Progress events генерирует Agent Runtime / MCPClient,
а не отдельные MCP-инструменты.
```

Это важно, потому что MCP-серверы могут быть сторонними, динамически подключаемыми и неизвестными заранее.
Нельзя требовать, чтобы каждый внешний MCP-инструмент умел отдавать человекочитаемые progress-подсказки.

---

## 21. Почему v0.3-progress-events вставляется до v0.4

Проект уже находится в состоянии v0.3-логики:

- есть JSON-протокол `AgentAction`;
- есть dynamic MCP manager tools;
- есть `pending_cycle` для `WAITING_USER`;
- есть `cycle_trace` и архив agent cycles;
- есть начальная логика context budget;
- есть `ProgressEvent` как базовая модель события.

Поэтому progress tracking логично добавить сейчас, пока архитектура ещё достаточно простая.

Не нужно ждать v0.5/v0.6, потому что базовое live-отслеживание можно сделать через:

```text
MCPClient progress_callback
→ API.call_agent
→ MessageProcessor
→ Gateway
→ Telegram server
→ editMessageText
```

Полноценная БД/event bus архитектура остаётся будущим развитием.

---

## 22. Текущая MVP-схема progress tracking

Базовая схема выполнения:

```text
Telegram server
→ отправляет пользователю: "Сообщение принято. Обрабатываю..."
→ получает status_message_id
→ отправляет UnifiedMessage в Gateway
→ Gateway прокидывает progress_callback
→ MCPClient генерирует ProgressEvent
→ progress_callback отправляет событие обратно в Telegram server
→ Telegram server редактирует status message
→ после завершения status message фиксируется как "✅ Готово. Ответ ниже." или "⚠️ Ошибка. Подробности ниже."
→ финальный ответ/ошибка отправляется новым Telegram-сообщением, если нужен notification-center сигнал
```

В MVP progress events существуют в трёх местах:

```text
1. SessionState.progress_events
2. AgentResult.progress_events
3. agent_cycle_archive.progress_events / cycle_trace
```

`SessionState.progress_events` нужен для текущего выполнения.

`AgentResult.progress_events` нужен для финального metadata-ответа.

`agent_cycle_archive.progress_events` нужен для debug и будущей миграции в БД.

---

## 23. Почему не PostgreSQL watcher на v0.3-progress-events

Вариант с PostgreSQL watcher или polling таблицы событий лучше оставить на v0.5+.

Причины:

```text
1. Сейчас проект ещё MVP.
2. PostgreSQL для agent memory появится только в v0.5.
3. Для live progress через БД нужны таблицы, polling/LISTEN-NOTIFY, task_id и cleanup.
4. Это усложнит архитектуру до того, как стабилизируется Agent Runtime.
5. Callback быстрее, проще и лучше подходит для текущей синхронной схемы Gateway.
```

Поэтому в v0.3-progress-events основной механизм:

```text
in-process / HTTP callback
```

А БД будет только будущим persistent event log.

---

## 24. Почему не LLM-generated progress как основной механизм

LLM-generated progress выглядит привлекательно, потому что модель может писать более естественные статусы:

```text
"Сейчас проверю свежие источники"
"Сначала найду методику анализа, потом соберу данные"
"Перехожу к сравнению результатов"
```

Но в v0.3-progress-events это не должно быть основой.

Причины:

```text
1. Дополнительный LLM-вызов на каждое событие увеличивает задержку.
2. Увеличивается стоимость при платных API.
3. Модель может галлюцинировать прогресс.
4. Progress event должен отражать реальное runtime-действие.
5. Для tool calls content может быть null, и это нормальное поведение.
```

Поэтому основной принцип:

```text
Детерминированные runtime events — основа.
LLM-generated agent_request — дополнительный человекочитаемый слой.
```

LLM может писать `agent_request` в `AgentAction`, когда она не вызывает инструмент.
Но если LLM вызывает инструмент через native tool calling, `content: null` остаётся нормальным и ожидаемым.

---

## 25. Базовая модель `ProgressEvent`

Минимальная модель уже существует, но для полноценного отслеживания её нужно расширить.

Рекомендуемая структура:

```python
class ProgressEvent(BaseModel):
    type: Literal[
        "cycle_started",
        "cycle_resumed",
        "iteration_started",
        "agent_message",
        "tool_start",
        "tool_done",
        "tool_error",
        "llm_retry",
        "llm_error",
        "infrastructure_error",
        "context_warning",
        "context_compaction_started",
        "context_compaction_done",
        "large_result_saved",
        "waiting_user",
        "cycle_done",
        "cycle_error",
    ]

    message: str

    event_id: str
    created_at: float

    session_id: str | None = None
    cycle_id: str | None = None
    iteration: int | None = None

    tool_name: str | None = None
    target_tool_name: str | None = None
    server_name: str | None = None

    severity: Literal["info", "success", "warning", "error"] = "info"
    visibility: Literal["user", "debug", "internal"] = "user"

    data: dict[str, Any] | None = None
```

Смысл полей:

```text
event_id       уникальный ID события
created_at     время создания события
session_id     сессия пользователя/клиента
cycle_id       текущий agent cycle
iteration      номер LLM-итерации
tool_name      имя manager tool или прямого tool call
target_tool_name реальный MCP-инструмент внутри mcp_call_tool
server_name    имя MCP-сервера, если известно
severity       уровень события для UI
visibility     можно ли показывать событие пользователю
data           отладочные/служебные данные, очищенные от секретов
```

---

## 26. Типы progress events

### `cycle_started`

Создаётся при старте нового agent cycle.

Пример сообщения:

```text
🧭 Начинаю обработку задачи…
```

### `cycle_resumed`

Создаётся при продолжении `pending_cycle` после `WAITING_USER` или infrastructure interruption.

Пример:

```text
▶️ Продолжаю задачу с учётом ответа…
```

### `iteration_started`

Debug-событие начала LLM-итерации.

Обычно:

```text
visibility = "debug"
```

Пользователю в Telegram его лучше не показывать.

### `agent_message`

Человекочитаемое сообщение, которое пришло из `AgentAction.agent_request`.

Используется, когда LLM не вызывает инструмент, а сообщает промежуточное намерение.

### `tool_start`

Создаётся перед вызовом инструмента.

Пример:

```text
🔧 Запускаю web_search_internet…
```

### `tool_done`

Создаётся после успешного завершения инструмента.

Пример:

```text
✅ Инструмент web_search_internet завершил работу.
```

### `tool_error`

Создаётся при ошибке или таймауте инструмента.

Пример:

```text
⚠️ Инструмент web_search_internet завершился с ошибкой.
```

### `llm_retry`

Создаётся при retryable-инфраструктурной ошибке LLM, когда runtime ещё будет делать повтор.

Примеры:

```text
⚠️ LLM HTTP 429. Повтор через 60 сек. Попытка 4/5…
⚠️ LLM transport error. Повтор через 60 сек. Попытка 4/5…
⚠️ LLM timeout. Повтор через 60 сек. Попытка 4/5…
```

Важно: сообщение должно сохранять технический смысл — HTTP-код, тип ошибки, номер попытки и задержку.
Нельзя заменять его абстрактным текстом вроде «модель временно недоступна», потому что такие формулировки хуже для отладки.

### `llm_error`

Создаётся, когда retryable-инфраструктурная ошибка LLM не была восстановлена и повторы исчерпаны.

Примеры:

```text
⚠️ LLM HTTP 429. Повторы исчерпаны. Попытка 5/5.
⚠️ LLM transport error. Повторы исчерпаны. Попытка 5/5.
```

### `infrastructure_error`

Создаётся, когда agent cycle прерывается из-за инфраструктурной ошибки, а состояние цикла можно сохранить для продолжения позже.

Пример финального summary для пользователя:

```text
⚠️ Задача прервана из-за инфраструктурной ошибки.

Тип: LLMTransportError / ConnectError
Итерация: 16
Состояние задачи сохранено, её можно продолжить позже.
```

### `context_warning`

Создаётся при приближении к лимиту контекста.

В v0.3-progress-events можно сделать debug-only.

### `context_compaction_started` / `context_compaction_done`

Зарезервированы для v0.4, когда появится реальная context compaction.

### `large_result_saved`

Зарезервирован для v0.4, когда большие результаты начнут сохраняться в `LargeResultStore`.

### `waiting_user`

Создаётся перед остановкой цикла на `WAITING_USER`.

Пример:

```text
❓ Нужны дополнительные данные от пользователя.
```

### `cycle_done`

Создаётся при успешном завершении цикла.

Пример:

```text
✅ Задача завершена.
```

### `cycle_error`

Создаётся при логической или инфраструктурной ошибке цикла.

---

## 27. Нормализация `mcp_call_tool`

Так как LLM видит только manager tools, реальный внешний инструмент часто вызывается через:

```text
mcp_call_tool
```

Фактическое имя инструмента лежит внутри аргументов:

```json
{
  "tool_name": "web_search_internet",
  "arguments": {
    "query": "..."
  }
}
```

Поэтому progress event должен различать:

```text
tool_name        = "mcp_call_tool"
target_tool_name = "web_search_internet"
```

Это нужно, чтобы:

- runtime понимал, что был вызван manager tool;
- UI показывал пользователю реальный инструмент;
- trace сохранял обе сущности;
- будущая аналитика могла считать и manager tools, и реальные tools.

Рекомендуемый helper:

```python
def _resolve_progress_tool_names(
    self,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, str | None]:
    if tool_name == "mcp_call_tool":
        target_tool_name = arguments.get("tool_name")
        return tool_name, str(target_tool_name) if target_tool_name else None

    return tool_name, None
```

---

## 28. Progress localization

В v0.3-progress-events нужно добавить простую локализацию progress-сообщений.

Источник локали:

```python
progress_locale = message.metadata.get("progress_locale", "ru")
```

Для Telegram можно передавать `progress_locale` из Telegram server в Gateway.

Базовая логика:

```text
1. Если metadata.progress_locale задан — использовать его.
2. Если не задан — использовать "ru".
3. В будущем можно определять язык по user locale или языку сообщения.
```

Пример словаря:

```python
PROGRESS_MESSAGES = {
    "ru": {
        "accepted": "Сообщение принято. Обрабатываю…",
        "cycle_started": "🧭 Начинаю обработку задачи…",
        "cycle_resumed": "▶️ Продолжаю задачу с учётом ответа…",
        "tool_start": "🔧 Запускаю {tool_name}…",
        "tool_done": "✅ Инструмент завершил работу.",
        "tool_error": "⚠️ Инструмент завершился с ошибкой.",
        "cycle_done": "✅ Задача завершена.",
    },
    "en": {
        "accepted": "Message received. Processing…",
        "cycle_started": "🧭 Starting the task…",
        "cycle_resumed": "▶️ Continuing the task…",
        "tool_start": "🔧 Running {tool_name}…",
        "tool_done": "✅ Tool finished.",
        "tool_error": "⚠️ Tool failed.",
        "cycle_done": "✅ Task completed.",
    },
}
```

Для LLM retry/error progress нужны отдельные локализованные ключи.
Они должны быть короткими, но технически точными:

```python
PROGRESS_MESSAGES = {
    "ru": {
        "llm_http_retry": "⚠️ LLM HTTP {status_code}. Повтор через {delay:.0f} сек. Попытка {attempt}/{max_attempts}…",
        "llm_http_exhausted": "⚠️ LLM HTTP {status_code}. Повторы исчерпаны. Попытка {attempt}/{max_attempts}.",
        "llm_transport_retry": "⚠️ LLM transport error. Повтор через {delay:.0f} сек. Попытка {attempt}/{max_attempts}…",
        "llm_transport_exhausted": "⚠️ LLM transport error. Повторы исчерпаны. Попытка {attempt}/{max_attempts}.",
        "llm_timeout_retry": "⚠️ LLM timeout. Повтор через {delay:.0f} сек. Попытка {attempt}/{max_attempts}…",
        "llm_timeout_exhausted": "⚠️ LLM timeout. Повторы исчерпаны. Попытка {attempt}/{max_attempts}.",
    },
    "en": {
        "llm_http_retry": "⚠️ LLM HTTP {status_code}. Retrying in {delay:.0f}s. Attempt {attempt}/{max_attempts}…",
        "llm_http_exhausted": "⚠️ LLM HTTP {status_code}. Retries exhausted. Attempt {attempt}/{max_attempts}.",
        "llm_transport_retry": "⚠️ LLM transport error. Retrying in {delay:.0f}s. Attempt {attempt}/{max_attempts}…",
        "llm_transport_exhausted": "⚠️ LLM transport error. Retries exhausted. Attempt {attempt}/{max_attempts}.",
        "llm_timeout_retry": "⚠️ LLM timeout. Retrying in {delay:.0f}s. Attempt {attempt}/{max_attempts}…",
        "llm_timeout_exhausted": "⚠️ LLM timeout. Retries exhausted. Attempt {attempt}/{max_attempts}.",
    },
}
```

Финальные error-сообщения тоже должны иметь локализацию, но они не являются live-progress текстом.
Они отправляются пользователю как итоговое новое сообщение, если agent cycle завершился ошибкой:

```python
FINAL_ERROR_MESSAGES = {
    "ru": {
        "infrastructure_interruption": (
            "⚠️ Задача прервана из-за инфраструктурной ошибки.\n\n"
            "Тип: {error_type}\n"
            "Итерация: {iteration}\n"
            "Состояние задачи сохранено, её можно продолжить позже."
        ),
        "agent_error": (
            "⚠️ Агент завершил задачу с ошибкой.\n\n"
            "Тип: {error_type}\n"
            "Итерация: {iteration}\n"
            "Подробности: {error_message}"
        ),
    },
    "en": {
        "infrastructure_interruption": (
            "⚠️ The task was interrupted by an infrastructure error.\n\n"
            "Type: {error_type}\n"
            "Iteration: {iteration}\n"
            "The task state has been saved and can be resumed later."
        ),
        "agent_error": (
            "⚠️ The agent finished with an error.\n\n"
            "Type: {error_type}\n"
            "Iteration: {iteration}\n"
            "Details: {error_message}"
        ),
    },
}
```

Локализация не должна требовать LLM-вызова.

LLM-generated progress можно добавить позже как optional polish layer.

---

## 29. Telegram final delivery и notification center

Live-progress в Telegram делается через редактирование одного status-сообщения:

```text
Сообщение принято. Обрабатываю…
→ 🔧 Запускаю инструмент…
→ ⚠️ LLM HTTP 429. Повтор через 60 сек. Попытка 4/5…
→ ✅ Задача завершена.
```

Но `editMessageText` не является надёжным способом вызвать уведомление в notification center телефона/ноутбука.
Telegram push-уведомления обычно появляются на новое сообщение, а не на редактирование старого.

Поэтому для Telegram нужно разделить две сущности:

```text
status message = live progress timeline через editMessageText
final answer / final error = новое Telegram-сообщение
```

Рекомендуемый режим для Telegram:

```text
TELEGRAM_FINAL_DELIVERY_MODE = "send_new"
```

Возможные режимы:

```text
edit_status
  Финальный ответ заменяет status-сообщение.
  Удобно для коротких локальных тестов, но не даёт надёжного notification-center уведомления.

send_new
  Status-сообщение редактируется в "✅ Готово. Ответ ниже."
  Финальный ответ отправляется отдельным новым сообщением.
  Это предпочтительный режим для Telegram.

auto
  Короткие ответы могут редактировать status-сообщение, длинные отправляются отдельно.
  Удобно для MVP, но хуже для гарантии уведомления об окончании.
```

Рекомендуемый UX:

```text
1. Пользователь отправляет запрос.
2. Telegram server отправляет: "Сообщение принято. Обрабатываю…"
3. Status-сообщение редактируется по progress events.
4. При успешном завершении status-сообщение становится: "✅ Готово. Ответ ниже."
5. Финальный ответ отправляется новым сообщением.
6. При ошибке status-сообщение становится: "⚠️ Ошибка. Подробности ниже."
7. Error summary отправляется новым сообщением.
```

Полностью гарантировать уведомление нельзя: пользователь может отключить уведомления, открыть чат, включить Do Not Disturb или Telegram может сгруппировать уведомления.
Но технически правильный способ повысить шанс notification-center уведомления — отправить новое сообщение, а не только редактировать старое.

---

## 30. Infrastructure errors: progress, logs и final notification

Инфраструктурные ошибки нужно показывать в трёх разных слоях:

```text
1. Logs / trace
   Полный технический формат: классы ошибок, HTTP-коды, attempt/max_attempts, retry_after, context, repr/response_text.

2. Progress event
   Короткий live-status для пользователя, но с сохранением технического смысла: HTTP 429, transport error, timeout, attempt/max_attempts.

3. Final notification
   Новое Telegram-сообщение с компактным summary: тип ошибки, итерация, можно ли продолжить задачу.
```

Логи нельзя заменять абстрактными «человекочитаемыми» сообщениями.
Для разработки полезнее видеть точные коды и классы ошибок:

```text
LLMHTTPError(status_code=429)
LLMTransportError / ConnectError
LLMTimeoutError
attempt=5/5
context=Итерация 16
retry_after=60
```

Progress-сообщение тоже должно сохранять код или тип ошибки:

```text
⚠️ LLM HTTP 429. Повтор через 60 сек. Попытка 4/5…
⚠️ LLM transport error. Повтор через 60 сек. Попытка 4/5…
⚠️ LLM timeout. Повтор через 60 сек. Попытка 4/5…
```

Если попытки исчерпаны, не нужно писать «Повтор через...».
Финальный progress event должен быть терминальным:

```text
⚠️ LLM HTTP 429. Повторы исчерпаны. Попытка 5/5.
⚠️ LLM transport error. Повторы исчерпаны. Попытка 5/5.
```

Финальное сообщение пользователю при infrastructure interruption:

```text
⚠️ Задача прервана из-за инфраструктурной ошибки.

Тип: LLMTransportError / ConnectError
Итерация: 16
Состояние задачи сохранено, её можно продолжить позже.
```

Для английской локали:

```text
⚠️ The task was interrupted by an infrastructure error.

Type: LLMTransportError / ConnectError
Iteration: 16
The task state has been saved and can be resumed later.
```

Raw error остаётся в логах, `event.data`, `metadata.error`, `cycle_trace` и agent cycle archive.
Telegram final notification показывает компактный technical summary, а не полный traceback/raw payload.

---

## 31. Throttling и deduplication для Telegram

Telegram status-message нельзя редактировать слишком часто.

Нужны правила:

```text
1. Не редактировать сообщение чаще одного раза в 1–2 секунды.
2. Не редактировать, если текст не изменился.
3. Не показывать visibility="debug" и visibility="internal".
4. Короткие tool_done можно пропускать, если сразу идёт следующий tool_start.
5. Финальный ответ должен заменить статусное сообщение или быть отправлен отдельным сообщением.
```

Рекомендуемый MVP:

```text
- Telegram server хранит last_progress_text.
- Telegram server хранит last_progress_edit_ts.
- Если новое событие пришло слишком быстро — можно отложить или пропустить.
- Ошибки editMessageText не должны ломать agent cycle.
```

---

## 32. Безопасность progress events

Progress events нельзя превращать в утечку данных.

В `data` могут случайно попасть:

- API-ключи;
- токены;
- cookies;
- пароли;
- приватные URL;
- пользовательские файлы;
- большие tool payloads;
- raw tool results.

Поэтому нужен sanitizing.

Правило:

```text
Пользователю показывается только event.message.
Event.data — debug/service payload и должен быть очищен от секретов.
```

Минимальная политика:

```text
1. Редактировать чувствительные ключи: api_key, token, password, secret, authorization, cookie.
2. Обрезать длинные строки.
3. Ограничивать длину списков.
4. Не класть raw tool_result в progress event.
5. Для больших результатов использовать result_id/preview/metadata.
```

---

## 33. Связь progress events с `pending_cycle`

Если агент остановился на `WAITING_USER`, progress events текущего цикла нужно сохранить в `pending_cycle.progress_events`.

При продолжении цикла:

```text
1. previous_cycle_progress_events берутся из pending_cycle.
2. Новый RUNNING-этап начинает новый state.progress_events.
3. При архивировании цикла progress events объединяются.
```

Это важно, потому что один agent cycle может иметь несколько пауз:

```text
RUNNING → WAITING_USER → RUNNING → WAITING_USER → RUNNING → DONE
```

Все progress events должны остаться связанными с одним `cycle_id`.

---

## 34. Будущее развитие progress events

### v0.4

Progress events начинают отражать large result handling:

```text
large_result_saved
context_warning
context_compaction_started
context_compaction_done
```

События должны содержать только ссылки и metadata:

```json
{
  "result_id": "res_123",
  "size_chars": 2140000,
  "preview_chars": 2000
}
```

Raw content не должен попадать в progress events.

### v0.5

Progress events сохраняются в PostgreSQL.

Возможная таблица:

```text
cycle_trace_events
  id
  cycle_id
  event_type
  payload_json
  created_at
```

`ProgressEvent` должен быть совместим с этой будущей таблицей.

### v0.6

Progress events могут стать частью полноценной event bus архитектуры:

```text
Agent Runtime
→ Redis Stream / PubSub
→ Telegram server / WebSocket / SSE / CLI
```

Это позволит:

- стримить прогресс в Web UI;
- восстанавливать состояние после перезапуска;
- запускать долгие background tasks;
- подписываться на task_id;
- объединять progress, trace, artifacts и memory events.

LLM-generated progress можно добавить как дополнительный слой:

```text
runtime event → optional LLM short status polish → localized UI message
```

Но даже в v0.6 runtime event остаётся источником истины.

---

# Часть V. v0.3-mcp-server-manager — lifecycle-aware MCP Server Manager

## 35. Назначение v0.3-mcp-server-manager

`v0.3-mcp-server-manager` — промежуточная версия между `v0.3-progress-events` и `v0.4`.

Её задача — превратить `MCPServerManager` из тонкой обёртки над `MCPClient` в слой, который отвечает за жизненный цикл MCP-серверов и вызовов инструментов.

Проблема текущего поведения:

```text
MCP-сервер подключился один раз
→ runtime.session сохранилась в памяти агента
→ tool call всегда использует старую session
→ внешний MCP-сервер перезапустился / transport умер / pipe закрылась
→ старая session стала stale
→ tool call может висеть до общего timeout
```

Нужная модель:

```text
MCPServerManager.call_tool()
→ resolve tool binding
→ получить runtime
→ проверить lifecycle state
→ вызвать инструмент через transport-specific timeout
→ при transport/lifecycle error пометить runtime unhealthy
→ восстановить runtime по правилам транспорта
→ повторить tool call один раз
→ вернуть result или корректный tool_error
```

Это обновление не требует PostgreSQL, Redis, workers или большой перестройки API.
Оно усиливает текущий runtime-слой и готовит формат данных к будущему хранению в PostgreSQL.

---

## 36. Почему это отдельная версия, а не часть v0.4

`v0.4` отвечает за large results, storage interfaces и подготовку context/large data architecture.

`v0.3-mcp-server-manager` решает другую проблему:

```text
надёжность MCP runtime
lifecycle MCP-серверов
reconnect/restart
повтор tool call после восстановления
fail-fast вместо зависания
```

Это нужно сделать раньше v0.4, потому что большие результаты, browser tools, HTTP MCP-серверы и будущие MCP-инструменты будут опираться на стабильный lifecycle manager.

---

## 37. Текущая проблема: MCP runtime считается бессмертным

Сейчас у агента есть `server_runtimes`, `tool_registry`, `available_tools` и `MCPServerManager`, но фактический вызов инструмента всё ещё завязан на прямой вызов сохранённого runtime:

```text
runtime.session.call_tool(...)
```

Если MCP-сервер перезапускается, например через `uvicorn --reload`, старая streamable HTTP session становится недействительной.
Но агент может продолжать считать её активной, пока внешний `tool_call_timeout` не завершит ожидание.

Это проявляется так:

```text
tool_start: 🔧 Запускаю reference…
сервер ушёл в reload/shutdown
старый ClientSession завис
через несколько минут: tool_error timeout
```

Такое поведение считается недоработкой lifecycle-слоя.

---

## 38. Разделение ответственности

После `v0.3-mcp-server-manager` ответственность должна быть такой:

```text
MCPClient:
  - agent loop;
  - LLM-вызовы;
  - AgentAction JSON;
  - progress events;
  - pending_cycle / interrupted cycle;
  - context budget / compaction hooks;
  - low-level connect/close/register helpers пока могут оставаться здесь.

MCPServerManager:
  - список серверов и инструментов;
  - resolve tool binding;
  - получение runtime;
  - lifecycle-aware call_tool;
  - mark runtime unhealthy;
  - recover runtime;
  - reconnect/restart;
  - replace runtime;
  - retry once;
  - подготовка формата для будущего PostgreSQL-хранения MCP-серверов/tools.
```

`MCPClient` не должен бесконечно разрастаться логикой transport recovery.
`MCPServerManager` должен стать единым координатором MCP lifecycle.

---

## 39. Единый сценарий lifecycle-aware tool call

Общий алгоритм:

```text
MCPServerManager.call_tool(tool_name, arguments)
  1. resolve_tool_binding(tool_name)
  2. get_runtime(binding.server_name)
  3. если runtime unhealthy → recover_runtime(server_name)
  4. call_tool_once(binding, arguments) с transport timeout
  5. если success → return result
  6. если tool/application error → вернуть ошибку без reconnect
  7. если transport/lifecycle error → mark_unhealthy(runtime, error)
  8. recover_runtime(server_name)
  9. retry call_tool_once один раз
  10. если retry failed → MCPToolCallFailedError / tool_error
```

Важно: retry должен быть ограниченным.
Бесконечные reconnect/retry запрещены.

На текущем этапе достаточно:

```text
1 первоначальная попытка
+ 1 попытка после recovery
```

---

## 40. Transport-specific recovery

Сценарий общий, но восстановление зависит от транспорта.

### `streamable_http` / `http`

Сервер запущен отдельно, агент не владеет процессом.

Recovery:

```text
закрыть old runtime / old exit_stack
→ создать новый streamable_http transport
→ создать новый ClientSession
→ initialize()
→ list_tools()
→ replace runtime
→ пересобрать tool_registry для сервера
→ retry tool call
```

### `executable` / `stdio`

Агент сам запускает MCP-сервер как процесс.

Recovery:

```text
закрыть old exit_stack / pipe / process resources
→ заново вызвать _connect_executable_server(config)
→ initialize()
→ list_tools()
→ replace runtime
→ retry tool call
```

### `mcp_lookup`

Пока не реализован.

Поведение:

```text
fail-fast
без recovery
```

### будущие транспорты

Для будущих транспортов нужно будет добавить transport policy, но agent loop не должен меняться.

---

## 41. Минимальные lifecycle-поля runtime

`MCPServerRuntime` должен хранить не только transport/session, но и состояние жизненного цикла:

```python
@dataclass
class MCPServerRuntime:
    name: str
    alias: str
    connect_type: ServerConnectType
    session: Any = None
    http_client: Any = None
    exit_stack: Optional[AsyncExitStack] = None
    tools: List[Any] = field(default_factory=list)

    healthy: bool = True
    reconnecting: bool = False
    last_error: str | None = None
    connected_at: float = field(default_factory=time.time)
    generation: int = 0
```

Смысл полей:

```text
healthy       можно ли использовать runtime для вызовов
reconnecting  идёт ли восстановление runtime
generation    версия подключения; увеличивается после reconnect/restart
last_error    последняя lifecycle-ошибка
connected_at  время создания текущего runtime
```

Эти поля PostgreSQL-friendly: в будущей версии их можно перенести в таблицу состояния MCP-серверов.

---

## 42. PostgreSQL-friendly модель MCP runtime

Хотя PostgreSQL подключается позже, формат уже нужно проектировать так, чтобы его можно было перенести в таблицы.

Будущие таблицы могут выглядеть так:

```text
mcp_servers
  id
  name
  alias
  connect_type
  enabled
  config_json
  created_at
  updated_at

mcp_server_runtime_state
  server_name
  healthy
  reconnecting
  generation
  connected_at
  last_error
  last_recovered_at
  failed_calls_count
  reconnect_count

mcp_tools
  id
  server_name
  public_name
  remote_name
  description
  input_schema_json
  enabled
  updated_at

mcp_tool_calls
  id
  cycle_id
  server_name
  tool_name
  target_tool_name
  status
  started_at
  finished_at
  error_kind
  error_text
  retry_count
  runtime_generation
```

В v0.3-mcp-server-manager эти таблицы не создаются.
Но runtime-события и dataclass-поля должны быть совместимы с будущим переносом в БД.

---

## 43. Ошибки lifecycle и tool/application errors

Нужно различать два класса ошибок.

### Transport/lifecycle errors

Такие ошибки означают, что runtime мог стать недействительным:

```text
connection reset
connection refused
read/write error
stream closed
pipe closed
broken pipe
closed resource
end of stream
httpx network error
asyncio timeout на transport call
stale session
```

Для них нужно:

```text
mark runtime unhealthy
recover runtime
retry once
```

### Tool/application errors

Такие ошибки не лечатся reconnect-ом:

```text
неверные аргументы
validation error
unknown tool
бизнес-ошибка инструмента
tool вернул нормальный error payload
```

Для них нужно:

```text
не делать reconnect
вернуть tool_error в LLM
```

---

## 44. Таймауты lifecycle-слоя

Нужны отдельные timeout-уровни:

```text
tool_call_timeout
  общий верхний предел на tool call внутри agent loop.

mcp_startup_timeout
  timeout подключения одного MCP runtime при запуске Gateway.

mcp_transport_call_timeout
  timeout одной попытки вызова runtime.

mcp_reconnect_timeout
  timeout восстановления runtime.

mcp_runtime_close_timeout
  timeout graceful close одного runtime при shutdown, recovery,
  reload или disable. После превышения runtime отсоединяется локально,
  чтобы устаревшая SSE-сессия не блокировала lifecycle.

mcp_call_retries_after_recovery
  количество повторов после восстановления.
```

Штатные значения runtime-конфига:

```text
mcp_startup_timeout = 30 sec
mcp_transport_call_timeout = 15 sec
mcp_reconnect_timeout = 10 sec
mcp_runtime_close_timeout = 10 sec
mcp_call_retries_after_recovery = 1
```

Эти значения задаются в отдельном верхнеуровневом блоке `runtime`,
не смешиваются с `llm`, `memory` или `storage` и валидируются при старте.
`asyncio.timeout()` сохраняет открытие и закрытие MCP transport в одном task.
Reconnect выполняется только в рабочем lifecycle; close никогда не создаёт
новое соединение. Смысл: stale runtime не должен молча висеть ни до общего
`tool_call_timeout`, ни до стандартного пятиминутного SSE read timeout SDK.

---

## 45. `MCPServerManager` как lifecycle coordinator

`MCPServerManager` должен получить методы:

```text
resolve_tool_binding(tool_name)
get_runtime(server_name)
call_tool(tool_name, arguments)
call_tool_once(binding, arguments)
call_tool_with_recovery(binding, arguments)
mark_unhealthy(runtime, error)
is_transport_lifecycle_error(error)
recover_runtime(server_name)
replace_runtime(server_name, new_runtime)
supports_recovery(runtime)
```

Текущий `server_manager.call_tool()` не должен больше прокидывать вызов обратно в `MCPClient._call_registered_tool`, иначе возникает архитектурная рекурсия.

Правильный поток:

```text
MCPClient._manager_call_tool()
→ MCPServerManager.call_tool(target_tool_name, target_arguments)
→ lifecycle-aware call
```

---

## 46. `enable_server`, `disable_server`, `reload_server`

Эти методы уже относятся к lifecycle-слою и должны остаться в `MCPServerManager`.

Они должны использовать общие helper-ы:

```text
enable_server:
  _connect_single_server(config)
  replace_runtime(runtime.name, runtime)

disable_server:
  pop runtime
  close runtime
  unregister server tools
  config.enabled = False

reload_server:
  disable_server(name)
  enable_server(name)
```

`replace_runtime()` должен:

```text
закрыть старый runtime
увеличить generation
заменить server_runtimes[server_name]
пересобрать tools для этого сервера
```

---

## 47. Progress events и MCP lifecycle

На v0.3-mcp-server-manager не обязательно прокидывать `progress_callback` внутрь `MCPServerManager`.

Минимальная модель:

```text
MCPClient emits:
  tool_start
  tool_done
  tool_error

MCPServerManager logs:
  lifecycle error
  mark unhealthy
  recovery attempt
  recovery success/failure
```

Позже можно добавить отдельные progress events:

```text
mcp_server_reconnect
mcp_server_reconnect_done
mcp_server_reconnect_failed
mcp_tool_retry
```

Но источник истины всё равно остаётся runtime lifecycle, а Telegram/Web только отображают события.

---

## 48. Связь с `v0.3-progress-events`

`v0.3-progress-events` отвечает за отображение работы агента.

`v0.3-mcp-server-manager` отвечает за надёжность выполнения MCP-инструментов.

Они должны работать вместе:

```text
tool_start
→ lifecycle-aware call
→ recovery/retry внутри MCPServerManager
→ success: tool_done
→ failure: tool_error
```

Если recovery не удался, `process_query()` уже должен превратить исключение в tool result:

```json
{
  "type": "tool_error",
  "trusted": false,
  "tool_name": "...",
  "error": "..."
}
```

LLM получает это как данные, а не инструкцию.

---

## 49. Подготовка к workers

В будущих версиях вызовы инструментов можно будет вынести в worker layer.

Тогда lifecycle model станет основой для:

```text
mcp_tool_call_queue
worker executes tool call
status: queued / running / retrying / recovered / failed / done
tool result saved by result_id
progress event emitted to subscribers
```

Но в v0.3-mcp-server-manager никаких workers не нужно.

Сейчас достаточно synchronous lifecycle-aware вызова внутри агента.

---

## 50. Acceptance criteria v0.3-mcp-server-manager

### Stable tool call

Если MCP-сервер работает штатно:

```text
вызовы инструментов работают как раньше
tool_start/tool_done приходят как раньше
```

### streamable_http reload

Если streamable HTTP MCP-сервер перезапускается во время tool call:

```text
tool call не висит до 240 секунд
runtime помечается unhealthy
MCPServerManager пытается восстановить runtime
если сервер поднялся — tool call повторяется и завершается
если сервер не поднялся — быстро возвращается tool_error
```

### stdio/executable died

Если executable MCP-сервер умер или pipe закрылась:

```text
runtime unhealthy
process/session закрывается
сервер запускается заново
call retry once
при неудаче tool_error
```

### logical/tool error

Если ошибка связана с аргументами или бизнес-логикой инструмента:

```text
reconnect не выполняется
ошибка возвращается как tool_error
```

### parallel recovery

Если два запроса одновременно обнаружили unhealthy runtime:

```text
reconnect выполняется под lock
не создаются два конкурирующих runtime одного сервера
```

---

## 51. Что не входит в v0.3-mcp-server-manager

Не входит:

```text
PostgreSQL tables
SQLAlchemy/Alembic
Redis/arq
background workers
event bus
TransportPolicy-классы
полный перенос connect-кода из MCPClient
progress_callback внутри MCPServerManager
```

Можно оставить TODO:

```text
v0.4/v0.5:
  transport policies as classes
  health check per server
  per-server timeout config
  PostgreSQL MCP server/tool tables
  MCP tool call history
  worker-based tool execution
  metrics: reconnect_count, failed_calls_count, last_recovered_at
```

---


# Часть VI. Итог v0.3 перед v0.4

## 52. Итог v0.3 перед v0.4

`v0.3` можно считать архитектурно завершённой фундаментальной версией agent runtime.

Эта версия закрепила не одну отдельную функцию, а базовый каркас дальнейшей архитектуры:

```text
LLM agent loop
+ AgentAction JSON
+ dynamic MCP manager tools
+ cycle memory
+ pending / interrupted cycle
+ progress events
+ MCP runtime lifecycle
+ delivery constraints
+ final processing pipeline
+ cycle_trace / archive
```

Главный итог:

```text
v0.3 превратила проект из простого MCP-клиента
в управляемый agent runtime с наблюдаемостью,
сохраняемым состоянием цикла и подготовкой к storage-архитектуре.
```

### Ключевые изменения v0.3

В рамках v0.3 закреплены следующие слои:

```text
1. JSON-протокол агента
   AgentAction заменяет старые текстовые маркеры и делает ответы агента валидируемыми.

2. Dynamic MCP discovery
   Агент больше не должен знать все инструменты из system prompt.
   Доступные серверы, инструменты и схемы раскрываются через manager tools.

3. Лёгкий system prompt
   System prompt отвечает за базовые правила, JSON-протокол и безопасность.
   Tool descriptions и surface-specific formatting вынесены из него.

4. Agent cycle memory
   Введены cycle_id, cycle_trace, pending_cycle, interrupted cycle и last_error_cycle.
   WAITING_USER больше не считается завершением задачи.

5. Resume после WAITING_USER и инфраструктурных ошибок
   Агент может сохранить контекст незавершённого цикла и продолжить работу после ответа пользователя
   или после временного сбоя LLM/transport.

6. Live progress events
   Runtime сообщает UI/Telegram о ключевых этапах выполнения задачи:
   cycle_started, cycle_resumed, tool_start, tool_done, tool_error, llm_retry,
   llm_error, waiting_user, final_processing_started, cycle_done, cycle_error.

7. Telegram progress UX
   Status-message принадлежит progress callbacks.
   Финальный ответ отправляется отдельным сообщением.
   Telegram server не должен затирать runtime-status собственным "готово".

8. LLM retry/error classification
   Retryable HTTP/transport ошибки отделены от configuration errors.
   429/5xx могут сохранять контекст для продолжения.
   400/401/403/404/422 считаются ошибками конфигурации и не получают retry.

9. Lifecycle-aware MCPServerManager
   MCPServerManager отвечает за health/recovery/retry runtime-а MCP-серверов.
   Сбой внешнего MCP-сервера не должен ронять Gateway request.

10. Delivery constraints
    Telegram/Web-ограничения применяются на финальной стадии и влияют только на форму ответа,
    а не на факты, выводы или выбор инструментов.

11. Final processing pipeline
    Финальная обработка разделена на выбор режима, форматирование и проверку по собранным данным.
    В коде закреплены FORMAT_ONLY, GROUNDED, STRICT_GROUNDED и SKIP.

12. Cycle trace / archive
    cycle_trace стал подробным журналом работы agent cycle.
    Это переходный слой перед PostgreSQL-хранением событий, результатов и артефактов.
```

### Почему v0.3 нужно остановить

К концу v0.3 в проекте уже есть основные runtime-механизмы:

```text
агентный цикл работает;
инструменты подключаются динамически;
прогресс виден пользователю;
ошибки LLM и MCP runtime обрабатываются управляемо;
контекст цикла можно сохранять;
финальный ответ проходит отдельную обработку.
```

Дальше добавлять крупные новые сущности прямо в v0.3 уже неразумно.

Причина:

```text
новые механизмы вроде DAG planner, workers, persistent tool-call tracking,
PostgreSQL memory и LargeResultStore должны жить уже на новой storage/runtime архитектуре.
Если добавить их в v0.3, их потом придётся переносить и переписывать при v0.4/v0.5.
```

### Feature freeze для v0.3

После завершения v0.3 допустимы только безопасные изменения:

```text
bugfixes;
tests;
documentation;
небольшие UX-правки;
чистка названий и структуры;
фиксация design_document.md;
подготовка к v0.4.
```

Не стоит добавлять в v0.3:

```text
DAG planner;
background workers;
PostgreSQL tables;
LargeResultStore;
persistent tool-call queue;
Redis/arq;
полноценный task manager;
новую систему long-term memory;
новые крупные runtime-artifacts.
```

### Граница перехода к v0.4

Правило перехода:

```text
v0.3 фиксирует текущий agent runtime.
v0.4 начинает storage/large-context архитектуру.
```

Практически это означает:

```text
1. Новые крупные возможности добавлять после v0.4/v0.5.
2. v0.3 больше не расширять архитектурно.
3. Перед v0.4 стабилизировать документацию, тесты и багфиксы.
4. Использовать v0.3 как baseline для миграции к storage interfaces, compaction и PostgreSQL.
```

Короткая формула:

```text
Не добавлять в старую архитектуру то,
что должно жить в новой.
```

---

# Часть VII. Context budget

## 53. Настройки LLM context budget

В `mcp.config`:

```json
{
  "llm": {
    "context_window_tokens": 262144,
    "max_tokens": 4096,
    "reserved_output_tokens": 8192,
    "context_safety_ratio": 0.75,
    "context_compaction_target_ratio": 0.55,
    "enable_context_compaction": true
  }
}
```

### `context_window_tokens`

Полное контекстное окно модели.

### `max_tokens`

Максимальное количество токенов, которое API разрешает модели сгенерировать.

### `reserved_output_tokens`

Запас под будущий ответ модели.

Если `reserved_output_tokens` не задан, можно использовать `max_tokens`.

Рекомендуемая логика:

```python
effective_reserved_output_tokens = max(
    reserved_output_tokens or 0,
    max_tokens,
)
```

### `context_safety_ratio`

Порог, когда нужно начинать беспокоиться о переполнении контекста.

### `context_compaction_target_ratio`

Цель, до которой желательно ужать visible context после compaction.

Важно:

```text
target ratio не означает “удалить ровно X токенов”.
Он означает “попытаться привести контекст к целевому бюджету,
сжимая смысловые блоки”.
```

---

# Часть VIII. v0.4 — agent workspace, planning & context management foundation

## 54. Главная идея v0.4

`v0.4` превращает текущий agent runtime в рабочее пространство, способное безопасно выполнять длинные, составные и файловые задачи.

Версия объединяет шесть связанных пакетов:

```text
v0.4-storage-foundation
v0.4-result-compaction
v0.4-cycle-compaction
v0.4-dag-planning
v0.4-file-artifacts
v0.4-input-runtime
```

Главная архитектурная формула:

```text
полные данные, файлы и история выполнения
→ внешнее storage/workspace

видимый LLM-контекст
→ только актуальная рабочая информация,
   компактные представления и устойчивые ссылки
```

`v0.4` работает без PostgreSQL, Redis и workers, но новые компоненты проектируются через интерфейсы, совместимые с последующей миграцией.

---

## 55. Граница v0.4

В `v0.4` входят:

- файловая storage foundation;
- LLM-компактизация больших результатов;
- LLM-компактизация старой части agent cycle;
- необязательный DAG-план;
- получение, чтение, изменение, версионирование и отправка файлов;
- `InputBatch`;
- `CycleInbox`, принимающий `InputBatch`;
- safe checkpoints и per-session lock;
- progress/trace events новых процессов.

В `v0.4` не входят:

- PostgreSQL и pgvector;
- embeddings и semantic RAG;
- постоянный chunk index;
- Redis/arq и background workers;
- automatic DAG scheduler;
- распределённый runtime;
- микросервисная архитектура.

Главный инвариант:

```text
Raw content не должен бесконтрольно жить
в messages_for_llm или дублироваться в cycle archive.
```

---

## 56. Разделение ответственности

Рекомендуемая структура:

```text
src/
  storage/
    models.py
    interfaces.py
    file_backend.py
    serializers.py

  memory/
    context_budget.py
    result_compaction.py
    cycle_compaction.py
    service.py

  artifacts/
    models.py
    service.py
    processors.py

  planning/
    models.py
    validation.py
    service.py
    tools.py

  runtime/
    cycle.py
    session_runtime.py
    input_batch.py
    cycle_inbox.py
```

Физически все модули не обязательно создавать одним патчем, но границы ответственности должны быть сохранены.

`MCPClient` остаётся orchestrator agent loop, но не должен знать физические пути хранения, реализовывать DAG-validation, напрямую отправлять Telegram-файлы или принимать параллельные изменения `messages_for_llm`.

---

# Часть VIII-A. v0.4-storage-foundation

## 57. Назначение storage foundation

Storage foundation хранит:

- большие результаты инструментов;
- полные cycle/trace events;
- пользовательские файлы;
- файлы, созданные и изменённые агентом;
- DAG-планы;
- working memory;
- sealed input batches.

Первая реализация — локальная файловая.

Главное правило:

```text
runtime зависит от storage interfaces,
а не от конкретной файловой структуры.
```

---

## 58. `ContentStore`

`ContentStore` хранит произвольное содержимое, которое может быть слишком большим для LLM-контекста.

Источники:

```text
tool_result
browser_snapshot
browser_html
webpage
document_text
user_file_content
generated_text
cycle_segment
retrieved_context
```

Базовый интерфейс:

```python
class ContentStore(Protocol):
    async def save_content(...) -> ContentRef: ...
    async def get_metadata(content_id: str) -> ContentMetadata: ...
    async def read_content(content_id: str) -> bytes: ...
    async def read_text(content_id: str) -> str: ...
    async def read_range(
        content_id: str,
        *,
        offset: int,
        length: int,
    ) -> ContentRange: ...
    async def search_text(
        content_id: str,
        *,
        query: str,
        limit: int,
    ) -> list[ContentMatch]: ...
```

`read_range` и простой линейный `search_text` ещё не являются полноценным RAG. Они нужны, чтобы ссылка на оригинал уже в `v0.4` не была тупиковой.

---

## 59. `ArtifactStore`

`ArtifactStore` хранит файлы как версионируемые объекты рабочего пространства.

```python
class ArtifactStore(Protocol):
    async def save_artifact(...) -> ArtifactRef: ...
    async def get_artifact(artifact_id: str) -> ArtifactRef: ...
    async def open_artifact(artifact_id: str) -> bytes: ...
    async def create_version(...) -> ArtifactRef: ...
    async def list_cycle_artifacts(cycle_id: str) -> list[ArtifactRef]: ...
    async def mark_for_delivery(
        artifact_id: str,
        *,
        client_type: str,
    ) -> None: ...
```

Разделение:

```text
ArtifactRef
→ оригинальный файл и его версии

ContentRef
→ текстовое/извлечённое содержимое,
   tool output или другой большой payload
```

---

## 60. Основные refs

### `ContentRef`

```json
{
  "content_id": "cnt_...",
  "source_type": "tool_result",
  "source_name": "web_search",
  "mime_type": "application/json",
  "size_bytes": 100000,
  "size_chars": 85000,
  "size_tokens_estimate": 42000,
  "content_hash": "sha256:...",
  "created_at": 0,
  "metadata": {}
}
```

### `StoredResultRef`

```json
{
  "type": "stored_result_ref",
  "result_id": "res_...",
  "content_id": "cnt_...",
  "cycle_id": "cycle_...",
  "tool_call_id": "call_...",
  "tool_name": "web_search",
  "summary_status": "inline | summarized | store_only | oversized | failed",
  "summary": null,
  "preview": null,
  "size_tokens_estimate": 42000,
  "needs_retrieval": false
}
```

### `ArtifactRef`

```json
{
  "artifact_id": "art_...",
  "cycle_id": "cycle_...",
  "filename": "report.md",
  "mime_type": "text/markdown",
  "size_bytes": 12345,
  "content_hash": "sha256:...",
  "version": 2,
  "parent_artifact_id": "art_previous",
  "source": "agent_generated",
  "created_at": 0,
  "metadata": {}
}
```

LLM не получает реальные локальные пути. Для неё используются непрозрачные ID.

---

## 61. Файловый backend

```text
storage/
  contents/
    <content_id>/
      metadata.json
      content.bin

  artifacts/
    <artifact_id>/
      metadata.json
      file.bin

  cycles/
    <session_id>/
      <cycle_id>/
        cycle.json
        events.jsonl
        working_memory.json

  plans/
    <plan_id>/
      plan.json

  input_batches/
    <session_id>/
      <batch_id>.json

  indexes/
```

Имена пользовательских файлов не используются как канонический storage path. Каноническим ключом является generated ID.

---

## 62. Атомарная запись и целостность

Запись:

```text
temporary file
→ flush
→ fsync при необходимости
→ atomic replace/rename
```

Для объекта сохраняются schema version, hash, timestamps, type и связи с cycle/session.

При несовпадении hash или повреждённом JSON storage возвращает управляемую ошибку, а не частично прочитанные данные.

Сериализация отделяется от domain models, чтобы PostgreSQL backend в `v0.5` не потребовал менять agent logic.

---

## 63. Конфигурация storage

```json
{
  "storage": {
    "backend": "filesystem",
    "root_dir": "storage",
    "atomic_writes": true,
    "verify_content_hash": true,
    "max_in_memory_content_bytes": 67108864
  }
}
```

`max_in_memory_content_bytes` — абсолютная техническая защита процесса. Она не заменяет относительную оценку результата по context window модели.

---

# Часть VIII-B. v0.4-result-compaction

## 64. Назначение result compaction

В `v0.3` большой tool result может остаться в `messages_for_llm` и занимать контекст на следующих итерациях.

В `v0.4`:

```text
tool call
→ raw result
→ оценка размера
→ сохранение оригинала
→ выбор представления
→ inline / LLM-summary / stored ref
→ безопасное представление в messages_for_llm
```

Сначала сохраняется оригинал, затем запускается суммаризация. Ошибка compact-запроса не должна приводить к потере raw result.

---

## 65. `result_handling`

В `mcp_call_tool` добавляется:

```json
{
  "tool_name": "some_tool",
  "arguments": {},
  "result_handling": "auto"
}
```

Значения:

```text
auto
prefer_inline
compact
store_only
```

- `auto` — runtime выбирает стратегию;
- `prefer_inline` — агент предпочитает полный результат;
- `compact` — агент заранее просит сохранить оригинал и создать summary;
- `store_only` — сохранить оригинал и вернуть metadata/ref без отдельного summary.

Ключевое правило:

```text
Agent controls efficiency.
Runtime controls safety.
```

`prefer_inline` является пожеланием. Runtime обязан переопределить его, если результат угрожает context budget или устойчивости процесса.

---

## 66. Относительные бюджеты

Используются существующие параметры:

```text
context_window_tokens
reserved_output_tokens
context_safety_ratio
context_compaction_target_ratio
enable_context_compaction
```

Расчёты:

```python
usable_input_tokens = (
    context_window_tokens
    - effective_reserved_output_tokens
)

context_trigger_tokens = int(
    usable_input_tokens * context_safety_ratio
)

context_target_tokens = int(
    usable_input_tokens * context_compaction_target_ratio
)

available_before_trigger = max(
    0,
    context_trigger_tokens - current_context_tokens,
)
```

Дополнительные параметры:

```json
{
  "memory": {
    "inline_result_max_input_ratio": 0.10,
    "single_pass_summary_max_input_ratio": 0.60,
    "result_summary_target_tokens": 256,
    "result_compaction_max_output_tokens": 2048,
    "enable_result_compaction": true
  }
}
```

Входные safety-пороги остаются относительными к context window.
Размер создаваемого compact-артефакта задаётся абсолютными параметрами:
`result_summary_target_tokens` относится только к полю `summary`, а
`result_compaction_max_output_tokens` ограничивает весь
`ResultCompactionSummary JSON`.

Результат можно оставить inline, только если он:

1. укладывается в относительный лимит одного результата;
2. не переводит весь контекст через trigger;
3. не имеет политики `compact` или `store_only`;
4. не превышает технический memory limit.

---

## 67. Выбор представления

Решение учитывает:

- размер результата;
- текущий размер `messages_for_llm`;
- доступный бюджет до trigger;
- `result_handling`;
- текущую `AgentActivity`;
- активный DAG и ожидаемую дальнейшую работу, если она отражена в плане.

Положительные сценарии:

```text
простая задача
→ один небольшой вызов
→ inline result
→ сразу финальный ответ
```

```text
длинная исследовательская задача
→ много вызовов
→ compact/store_only
→ результаты копятся в workspace
→ visible context остаётся чистым
```

Защитный сценарий:

```text
agent выбрал prefer_inline
→ tool вернул 2 млн символов
→ runtime принудительно сохраняет оригинал
→ inline запрещён
```

Control-plane manager results обрабатываются отдельно:

```text
mcp_list_servers
mcp_list_tools(include_schemas=false)
mcp_get_tool_schema
mcp_get_runtime_context
→ runtime-generated control-plane payload
→ inline до hard input limit
→ без ContentStore и без LLM-summary
```

Эти данные нужны агенту для выбора и корректного вызова следующего
инструмента. Замена списка или одиночной схемы на недоступный stored ref
ломает discovery loop и провоцирует повторные manager calls.

`mcp_list_tools(include_schemas=true)` запрещён: агрегат всех схем не имеет
надёжной верхней границы. Агент сначала получает краткий список, затем
запрашивает одну выбранную схему через `mcp_get_tool_schema`.

Если control-plane payload не помещается даже в hard input budget, runtime
возвращает явный `tool_result_processing_error` с
`retry_recommended=false`, не запускает LLM-summary и не предлагает повторять
тот же вызов. Обычные результаты `mcp_call_tool` по-прежнему проходят через
общую result-compaction policy.

DAG помогает агенту выбрать стратегию, но не является механизмом безопасности.

---

## 68. Single-pass LLM-summary результата

Если result не стоит оставлять inline, но он помещается в отдельный compact request:

```json
{
  "type": "result_compaction_request",
  "original_user_request": "...",
  "current_goal": "...",
  "agent_activity": "collecting",
  "active_plan_node": null,
  "tool": {
    "name": "...",
    "arguments": {}
  },
  "result_id": "res_...",
  "raw_result": "..."
}
```

Строгий ответ:

```json
{
  "type": "result_compaction",
  "summary": "Краткое содержание, релевантное текущей задаче.",
  "key_facts": [],
  "limitations": [],
  "suggested_follow_up": [],
  "needs_original_content": false
}
```

Summary должно сохранять ключевые факты, ID, ссылки, имена, ошибки и ограничения, не добавляя сведения из собственных знаний модели.

Summary не заменяет оригинал и всегда связано с `result_id/content_id`.

Если первый ответ не проходит `ResultCompactionSummary` validation, runtime
делает максимум один повторный structured-output запрос с тем же raw result,
более явной инструкцией и увеличенным, но ограниченным output budget.
Повторный отказ приводит к обычному `summary_status=failed`; оригинал остаётся
в `ContentStore`.

В логах internal LLM calls фиксируются только безопасные diagnostics:
`content_chars`, нормализованный `finish_reason` и числовой token usage.
Содержимое ответа и произвольные provider fields в diagnostics не попадают.

---

## 69. Oversized fallback

Если result не помещается даже в отдельный compact request, модель не должна делать вид, что прочитала его полностью.

```text
raw content → ContentStore
metadata + bounded preview → visible context
summary_status = oversized
needs_retrieval = true
```

```json
{
  "type": "stored_result_ref",
  "result_id": "res_...",
  "content_id": "cnt_...",
  "summary_status": "oversized",
  "size_tokens_estimate": 1050000,
  "preview": "Ограниченный начальный фрагмент...",
  "needs_retrieval": true,
  "note": "Полный результат превышает бюджет одного LLM-summary."
}
```

Иерархическая map-reduce-суммаризация переносится в `v0.5/v0.6`.

---

## 70. Original content и будущий chunking

В `v0.4` канонический оригинал хранится целиком. Постоянные chunks не обязательны.

```text
v0.4:
original content + metadata + content_id

v0.5, первый retrieval:
extract/read original
→ lazy chunking
→ chunks
→ embeddings
→ cache index
```

Chunks и embeddings являются перестраиваемыми производными данными.

---

## 71. Result ref в visible context

Summarized result:

```json
{
  "type": "stored_result_ref",
  "result_id": "res_...",
  "content_id": "cnt_...",
  "tool_name": "web_search",
  "summary_status": "summarized",
  "summary": "...",
  "key_facts": [],
  "limitations": [],
  "size_tokens_estimate": 42000,
  "needs_retrieval": false
}
```

Store-only/oversized result содержит bounded preview, metadata и `needs_retrieval=true`.

Raw path не передаётся LLM.

---

## 72. Result compaction и evidence

После `v0.4` raw results не должны автоматически собираться из `cycle_trace` в `final_evidence_pack`.

```text
small inline result
→ full evidence

summarized result
→ summary + result_id + key facts

retrieved range/chunk
→ фактически прочитанный fragment + source ref

непрочитанная часть stored result
→ не считается evidence
```

Final grounding не утверждает сведения из той части источника, которую агент фактически не прочитал.

---

## 73. Result progress и trace

Trace events:

```text
result_persist_started
result_persist_done
result_persist_failed
result_compaction_started
result_compaction_done
result_compaction_failed
oversized_result_stored
```

User-visible progress показывается для заметных операций:

```text
💾 Сохраняю большой результат…
🧩 Сжимаю большой результат…
```

Progress event не содержит raw result.

---

# Часть VIII-C. v0.4-cycle-compaction

## 74. Назначение cycle compaction

Даже после result compaction контекст растёт из-за большого числа итераций, пользовательских дополнений, плана, файлов, проверок и промежуточных решений.

```text
result compaction уже применена
→ context достиг trigger
→ выбирается старая закрытая часть cycle
→ LLM создаёт новое working memory
→ segment удаляется из visible context
```

---

## 75. Активный объект cycle

В `v0.4` активный объект создаётся с начала каждого цикла:

```python
class ActiveAgentCycle:
    cycle_id: str
    session_id: str
    original_user_request: str
    messages_for_llm: list[dict]
    cycle_trace: list[dict]
    working_memory: CycleWorkingMemory | None
    active_plan_id: str | None
    active_plan_revision: int | None
    active_plan_node_id: str | None
    artifact_refs: list[str]
    result_refs: list[str]
    status: str
```

`pending_cycle` остаётся сохраняемым состоянием приостановленного цикла, а не единственным объектом, где существует working state.

---

## 76. Атомарные сегменты

Cycle compactor не разрывает OpenAI-compatible последовательность.

Нельзя отделять `assistant tool_calls` от соответствующих `role=tool` results.

Сегмент выбирается только по закрытым логическим блокам:

```text
user/addendum
→ assistant action или tool_calls
→ все связанные tool results
→ завершённое последующее действие
```

Не компактизируются:

- system message;
- original user request;
- последнее пользовательское дополнение;
- последний вопрос агента;
- незакрытая tool-call цепочка;
- свежий хвост работы;
- активный plan node;
- ошибки, влияющие на продолжение;
- устойчивые result/artifact/plan refs.

---

## 77. `CycleWorkingMemory`

В visible context находится максимум одно актуальное working memory:

```json
{
  "type": "cycle_working_memory",
  "generation": 4,
  "summary": "Краткое состояние работы...",
  "working_state": {
    "current_goal": "...",
    "completed_actions": [],
    "confirmed_actions": [],
    "rejected_actions": [],
    "important_results": [],
    "important_decisions": [],
    "modified_files": [],
    "pending_confirmation": null,
    "errors_affecting_continuation": [],
    "active_plan_id": null,
    "active_plan_revision": null,
    "active_plan_node_id": null,
    "result_refs": [],
    "artifact_refs": []
  },
  "source_event_range": {
    "from": 1,
    "to": 138
  }
}
```

`working_summary` и `working_state` из v0.3 становятся частью этой модели.

---

## 78. Отсутствие summary tree

Нельзя создавать бесконечную цепочку:

```text
messages → summary A → summary B → summary C
```

Используются два слоя:

1. неизменяемый полный журнал исходных событий в storage;
2. одно заменяемое working memory в visible context.

При следующей compaction:

```text
previous working memory
+ новый закрытый segment
→ working memory generation N+1
```

Старые generations могут оставаться в audit storage, но не возвращаются в LLM-контекст и не образуют обязательное дерево ссылок.

---

## 79. LLM-компактизация cycle

Вход:

```json
{
  "type": "cycle_compaction_request",
  "original_user_request": "...",
  "previous_working_memory": null,
  "active_plan_state": null,
  "segment_to_compact": [],
  "preserve_rules": []
}
```

Строгий ответ:

```json
{
  "type": "cycle_compaction_result",
  "summary": "...",
  "working_state": {
    "current_goal": "...",
    "completed_actions": [],
    "confirmed_actions": [],
    "rejected_actions": [],
    "important_results": [],
    "important_decisions": [],
    "modified_files": [],
    "pending_confirmation": null,
    "errors_affecting_continuation": [],
    "active_plan_id": null,
    "active_plan_revision": null,
    "active_plan_node_id": null,
    "result_refs": [],
    "artifact_refs": []
  }
}
```

LLM не должна удалять refs, добавлять факты, менять пользовательские подтверждения или считать незавершённое действие завершённым.
Opaque `result_refs`, `artifact_refs` и активный plan принадлежат runtime:
cycle compactor не может создавать новые capability handles. Runtime строит
эти поля только из предыдущего working memory и состояния active cycle.
Архивируемый source segment является недоверенным и не может быть источником
capability handles, даже если содержит синтаксически корректные opaque IDs.

После невалидного structured output runtime выполняет ровно один repair-вызов
на тех же исходных данных. Второй невалидный ответ завершает compaction как
контролируемую ошибку; дополнительные structured-output retries запрещены.

Размер cycle compact-артефакта задаётся независимо от context window:

```json
{
  "memory": {
    "cycle_compaction_summary_target_tokens": 512,
    "cycle_compaction_max_output_tokens": 2048
  }
}
```

`cycle_compaction_summary_target_tokens` относится только к полю `summary`.
Полный `CycleCompactionResult JSON`, включая `working_state`, ограничивается
`cycle_compaction_max_output_tokens`. При выборе сегмента runtime использует
полный output budget как верхнюю оценку размера замены.

---

## 80. Trigger, target и recovery

Алгоритм:

```text
estimate полный main LLM request:
messages_for_llm + runtime_iteration_state + manager tool schemas
→ ниже trigger: продолжить

→ выше trigger:
   выбрать закрытый segment
   сохранить source segment
   вызвать cycle compactor
   заменить segment на CycleWorkingMemory
   повторно estimate

→ target не достигнут:
   применить следующий безопасный проход
```

Compaction не удаляет segment до успешного создания замены.

Если compact LLM-call завершился infrastructure error:

- source segment остаётся;
- generation не увеличивается;
- partial summary не принимается;
- cycle сохраняется как resumable при необходимости.

---

## 81. Cycle progress и trace

```text
cycle_compaction_started
cycle_compaction_done
cycle_compaction_failed
```

User-visible progress:

```text
🧠 Освобождаю рабочий контекст…
```

Trace хранит before/after tokens, generation и source event range, но не дублирует raw archived segment.

---

# Часть VIII-D. v0.4-dag-planning

## 82. Назначение и архитектурные границы DAG

DAG-план — это необязательный, runtime-owned, ревизионный рабочий artifact
активного agent cycle, описывающий существенные этапы задачи и зависимости
между ними.

Он не является:

- chain-of-thought;
- копией `messages_for_llm`;
- заменой `CycleWorkingMemory`;
- автоматическим scheduler;
- очередью tool calls;
- источником истины о результатах;
- обязательной церемонией для каждого запроса.

Как и в
[Apache Airflow](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html),
DAG отделяет структуру зависимостей от содержимого задач и сохраняет topology
относительно стабильной. Зависимости выполняют роль, сходную с `needs` в
GitHub Actions. При этом `v0.4` не переносит scheduler: узлы выбирает и
выполняет основная LLM через обычный agent loop.

Temporal и
[LangGraph](https://langchain-ai.github.io/langgraph/concepts/faq/)
служат ориентиром для будущего durable execution: plan и runtime state должны
переживать паузы и инфраструктурные сбои. Автоматическая оркестрация относится
к следующим версиям.

### 82.1. Разделение ответственности

| Компонент | Ответственность |
| --- | --- |
| `PlanStore` | Точное ревизионное хранение плана |
| `PlanningService` | Валидация DAG, переходы состояний и вычисление ready nodes |
| Manager tools | Команды и точечные запросы от LLM |
| Agent runtime | Активный plan, activity, guards и связь с cycle/tool trace |

`CycleWorkingMemory` не хранит полный plan. Она сохраняет только минимальное
runtime-состояние, необходимое для восстановления:

```json
{
  "active_plan_id": "plan_...",
  "active_plan_revision": 7,
  "active_plan_node_id": "pnode_..."
}
```

Полный актуальный plan всегда получается точным запросом в `PlanStore`, а не
из working-memory summary.

### 82.2. PlanStore и RAG решают разные задачи

Текущий authoritative plan нельзя получать через semantic search. RAG не
гарантирует актуальную revision, полный набор nodes, точные dependencies,
действующие statuses и active node.

```text
текущий plan
→ точный PlanStore / manager query

история, outcome и связанные знания
→ v0.5 RAG
```

RAG применяется к старым результатам, архивам cycles, outcome узлов, заметкам,
истории revisions и связанным документам. В `v0.5` файловый `PlanStore`
заменяется PostgreSQL-реализацией, но точный контракт чтения current plan
сохраняется.

### 82.3. Когда plan нужен

Plan создаётся, если задача содержит хотя бы один значимый признак сложности:

- несколько зависимых этапов;
- несколько файлов или artifacts;
- несколько внешних источников с последующей обработкой;
- side effects с обязательной проверкой;
- вероятный `WAITING_USER`;
- необходимость продолжить задачу после compaction/resume;
- развилки или существенная корректировка стратегии;
- длительный cycle, где легко повторить или забыть действие.

Plan не создаётся для:

- ответа без tools;
- одного понятного tool call;
- одного небольшого чтения;
- одного прямого изменения файла;
- простого уточнения;
- задачи, где plan добавляет больше действий, чем сама работа.

Практическое правило для system prompt:

> Создавай DAG-план не по формальному количеству шагов, а когда он снижает
> риск пропуска, повтора, неправильного порядка или потери состояния.

Runtime автоматически plan не создаёт. В `v0.4` решение принимает основная
LLM. Отдельный planner LLM-call не выполняется: это исключает дополнительную
стоимость, вложенный planning loop и второй источник решений.

Правила authoring выносятся в одну константу:

```python
PLAN_AUTHORING_RULES
```

Она используется в system prompt, description `agent_plan_create`, тестах и
позднее может быть переиспользована dedicated planner.

Основные authoring rules:

```text
DAG-план создаётся только для действительно многоэтапной задачи.

Nodes являются существенными единицами работы, а не внутренними
мыслительными действиями.

Запрещены nodes:
- «подумать»;
- «выбрать инструмент»;
- «создать plan»;
- «ответить пользователю»;
- отдельный node на каждый технический tool call.

Каждый node:
- имеет одну ясную цель;
- имеет проверяемые success criteria;
- зависит только от необходимых predecessors;
- не предполагает, что результат уже получен;
- не содержит выдуманных tool/result/artifact/plan IDs.

Для side effects добавляется отдельный validation node.
Независимые nodes не связываются искусственной линейной dependency.
Неизвестная условная ветвь добавляется новой revision после получения фактов.
```

Condition expressions, branch predicates, trigger rules, dynamic mapping и
subplans в `v0.4` не вводятся.

---

## 83. PlanStore, идентификаторы и domain models

### 83.1. Отдельный `PlanStore`

`ContentStore` хранит immutable content и не является authoritative storage
для изменяемого plan. Plan требует текущую revision, optimistic concurrency,
доменные переходы, точное чтение current state и историю изменений.

```python
@runtime_checkable
class PlanStore(Protocol):
    async def create_plan(
        self,
        plan: AgentPlan,
    ) -> AgentPlan:
        ...

    async def get_plan(
        self,
        plan_id: str,
        *,
        revision: int | None = None,
    ) -> AgentPlan:
        ...

    async def save_revision(
        self,
        plan: AgentPlan,
        *,
        expected_revision: int,
    ) -> AgentPlan:
        ...

    async def list_cycle_plans(
        self,
        cycle_id: str,
    ) -> list[PlanRef]:
        ...
```

Файловая структура `v0.4`:

```text
storage/
  plans/
    <plan_id>/
      metadata.json
      revisions/
        000001.json
        000002.json
        000003.json
```

`metadata.json` содержит только безопасный pointer:

```json
{
  "schema_version": 1,
  "plan_id": "plan_...",
  "cycle_id": "cycle-...",
  "current_revision": 3,
  "status": "active",
  "updated_at": "..."
}
```

Запись новой revision:

```text
проверить expected_revision
→ валидировать новый DAG
→ записать revisions/000004.json
→ атомарно заменить metadata.json
```

В одном процессе операции защищает per-plan lock. PostgreSQL backend `v0.5`
использует optimistic update:

```sql
UPDATE plans
SET revision = revision + 1, ...
WHERE plan_id = :plan_id
  AND revision = :expected_revision;
```

### 83.2. Runtime-owned IDs

Opaque capability IDs генерирует только runtime:

```text
plan_id = plan_<uuid4 hex>
node_id = pnode_<uuid4 hex>
```

При создании или batch-добавлении nodes LLM использует локальные
`client_key`, чтобы описать dependencies до генерации IDs:

```json
{
  "client_key": "collect_sources",
  "depends_on": ["inspect_input"]
}
```

Runtime возвращает mapping:

```json
{
  "node_id_map": {
    "inspect_input": "pnode_a1...",
    "collect_sources": "pnode_b2..."
  }
}
```

`client_key` может сохраняться как читаемый стабильный key, но capability
handle остаётся runtime-generated `node_id`.

### 83.3. `AgentPlan`

```python
class PlanStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AgentPlan(BaseModel):
    schema_version: Literal[1] = 1

    plan_id: str
    session_id: str
    cycle_id: str

    goal: str
    strategy: str | None = None

    status: PlanStatus
    revision: int
    nodes: list[PlanNode]

    created_at: datetime
    updated_at: datetime
```

`blocked` и `failed` не являются statuses всего plan. Они вычисляются из
состояния nodes. Если есть unresolved nodes, но нет ready и `in_progress`,
projection содержит `stalled=true`.

### 83.4. `PlanNode`

```python
class PlanNodeKind(str, Enum):
    COLLECT = "collect"
    PROCESS = "process"
    EXECUTE = "execute"
    VALIDATE = "validate"
    COORDINATE = "coordinate"
    OTHER = "other"


class PlanNodeStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanNode(BaseModel):
    node_id: str
    key: str

    title: str
    objective: str
    kind: PlanNodeKind

    depends_on: list[str]
    success_criteria: list[str]
    status: PlanNodeStatus

    outcome_summary: str | None = None
    status_reason: str | None = None

    result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

Произвольный `metadata: dict` в `v0.4` не добавляется: domain state должен
оставаться typed.

### 83.5. `ready` является projection

`ready` не хранится как status:

```python
node.status == PlanNodeStatus.PENDING
and all(
    dependency.status == PlanNodeStatus.DONE
    for dependency in dependencies
)
```

`SKIPPED` dependency не считается выполненной. Downstream node должен быть
изменён отдельной revision либо явно skipped/blocked. Вычисляемый ready-state
не устаревает и не требует массовой перезаписи downstream nodes.

---

## 84. PlanningService, DAG validation и lifecycle

### 84.1. Базовая валидация

`PlanningService` проверяет:

1. уникальность `client_key` и `node_id`;
2. существование всех dependencies;
3. отсутствие self-dependencies и cycles;
4. лимиты размера graph;
5. возможность start только для ready node;
6. максимум один `in_progress` node;
7. невозможность изменить dependencies начатого или terminal node;
8. безопасное удаление node с dependants;
9. runtime provenance `result_refs` и `artifact_refs`;
10. ожидаемую revision для каждой mutation.

### 84.2. Таблица переходов

| Текущее состояние | Разрешённые следующие состояния |
| --- | --- |
| `pending` | `in_progress`, `skipped` |
| `in_progress` | `done`, `blocked`, `failed`, `skipped` |
| `blocked` | `pending`, `skipped` |
| `failed` | `pending`, `skipped` |
| `done` | terminal |
| `skipped` | terminal |

Дополнительные invariants:

- `done` требует непустой `outcome_summary`;
- `blocked`, `failed` и `skipped` требуют `status_reason`;
- terminal node нельзя редактировать, удалять или повторно открывать;
- неполная работа исправляется новым corrective node;
- start разрешён только после `DONE` всех dependencies;
- refs принимаются только из runtime-owned списков active cycle.

### 84.3. Изменение topology

Plan остаётся адаптивным, но не переписывается после каждой iteration:

- новые факты требуют новой revision;
- новый обязательный этап добавляется новым node;
- потерявший смысл pending node удаляется либо становится skipped;
- выполненная работа не переписывается;
- terminal nodes не удаляются;
- ошибки исправляются corrective nodes.

Условная ветвь в `v0.4`:

```text
collect facts
→ condition resolved
→ add required branch nodes
→ skip or remove irrelevant pending nodes
```

### 84.4. Завершение и stalled projection

Plan автоматически становится `completed`, когда все nodes находятся в
`done` или `skipped`.

Поскольку skipped dependencies не удовлетворяют downstream requirements,
зависимые nodes до завершения plan должны быть изменены, skipped, удалены до
start или заменены.

Если нет ready/`in_progress`, но остаются unresolved nodes:

```json
{
  "stalled": true,
  "blocked_by": {
    "pnode_x": ["pnode_failed_dependency"]
  }
}
```

Agent должен retry failed node, изменить dependencies, добавить corrective
node, skip ветку либо отменить plan.

### 84.5. `WAITING_USER`

В `v0.4` используется полностью явная модель:

```text
transition current node → blocked
reason=waiting_user
→ следующая iteration возвращает ask_user
```

После resume:

```text
blocked → pending
pending → in_progress
```

Runtime не выполняет скрытую plan mutation при `WAITING_USER`. Это сохраняет
аудируемость, хотя требует дополнительной LLM iteration.

---

## 85. Manager tool context, команды и concurrency

### 85.1. `ManagerToolContext`

Plan manager tools требуют session/cycle context. Mutable global вроде
`self.current_cycle` запрещён, поскольку ломает параллельные sessions.

```python
@dataclass(slots=True)
class ManagerToolContext:
    session_id: str
    cycle_id: str

    active_cycle: ActiveAgentCycle
    session_state: SessionState


ManagerToolHandler = Callable[
    [dict[str, Any], ManagerToolContext],
    Awaitable[dict[str, Any]],
]
```

Существующие MCP manager tools принимают тот же context, но не обязаны
использовать все поля. Этот контракт позже переиспользуют artifact tools,
`InputBatch`, `CycleInbox` и session-aware memory tools.

### 85.2. Набор manager tools

`v0.4` использует семь command-oriented tools:

```text
agent_plan_create
agent_plan_get
agent_plan_add_nodes
agent_plan_update_node
agent_plan_transition_node
agent_plan_remove_node
agent_plan_cancel
```

Универсальный `agent_plan_patch` и JSON Patch не используются: они раскрывают
внутреннюю структуру документа, ухудшают status-specific validation и
позволяют случайно менять runtime-owned поля.

#### `agent_plan_create`

Создаёт и активирует новый plan текущего cycle.

```text
goal
strategy
nodes[]
```

Не принимает `plan_id`, `node_id`, `revision`, status, `result_refs` и
`artifact_refs`.

Пример:

```json
{
  "goal": "Проверить проект и безопасно обновить конфигурацию",
  "strategy": "Изучить входные данные, внести изменения и проверить результат",
  "nodes": [
    {
      "client_key": "inspect_inputs",
      "title": "Изучить входные файлы",
      "objective": "Определить структуру и необходимые изменения",
      "kind": "process",
      "depends_on": [],
      "success_criteria": [
        "Определены изменяемые файлы",
        "Зафиксированы ограничения"
      ]
    },
    {
      "client_key": "apply_changes",
      "title": "Внести изменения",
      "objective": "Создать новую версию файлов",
      "kind": "execute",
      "depends_on": ["inspect_inputs"],
      "success_criteria": [
        "Созданы новые версии изменяемых файлов"
      ]
    },
    {
      "client_key": "validate_changes",
      "title": "Проверить результат",
      "objective": "Убедиться, что изменения корректны",
      "kind": "validate",
      "depends_on": ["apply_changes"],
      "success_criteria": [
        "Проверки завершены без критических ошибок"
      ]
    }
  ]
}
```

Runtime проверяет `client_key`, генерирует IDs, разрешает local dependencies,
валидирует DAG, сохраняет revision 1, активирует plan и возвращает
`node_id_map` вместе с compact state.

#### `agent_plan_get`

Выполняет точное чтение active либо явно указанного plan.

```json
{
  "plan_id": null,
  "view": "summary",
  "node_id": null,
  "status_filter": [],
  "offset": 0,
  "limit": 10
}
```

Поддерживаемые views:

```text
summary — compact current state
nodes   — paginated compact node list
node    — один полный node
```

Отдельный `agent_plan_get_ready_nodes` не нужен: ready nodes входят в summary.

#### `agent_plan_add_nodes`

Добавляет batch nodes и требует `expected_revision`. Dependencies на
существующие nodes задаются через `node_id`, а внутри batch — через
`client_key`.

#### `agent_plan_update_node`

Меняет только незапущенный `pending` node:

```text
title
objective
kind
success_criteria
depends_on
```

Status и runtime-owned поля не изменяются.

#### `agent_plan_transition_node`

Выполняет lifecycle command:

```text
start
complete
block
fail
skip
retry
```

```json
{
  "plan_id": "plan_...",
  "expected_revision": 5,
  "node_id": "pnode_...",
  "transition": "complete",
  "outcome_summary": "...",
  "reason": null,
  "result_refs": ["res_..."],
  "artifact_refs": []
}
```

`PlanningService` проверяет обязательные поля и допустимость перехода.

#### `agent_plan_remove_node`

Удаляет только незапущенный `pending` node без dependants. Иначе возвращает:

```json
{
  "type": "plan_validation_error",
  "code": "node_has_dependants",
  "dependant_node_ids": ["pnode_..."]
}
```

#### `agent_plan_cancel`

Отменяет active plan с обязательными `expected_revision` и bounded `reason`.
Явная cancellation необходима final-answer guard, когда plan утратил
актуальность.

### 85.3. Optimistic concurrency

Все mutations, кроме create, требуют `expected_revision`. При конфликте:

```json
{
  "type": "plan_revision_conflict",
  "plan_id": "plan_...",
  "expected_revision": 5,
  "current_revision": 6,
  "retryable": true,
  "active_plan_state": {}
}
```

Runtime не повторяет mutation автоматически. LLM должна получить current
revision, сравнить изменения и сформировать новую осмысленную command.

---

## 86. Runtime integration и guards

### 86.1. Compact `ActivePlanState`

Полный plan автоматически в LLM context не помещается. На каждой iteration
runtime строит bounded projection и добавляет её в существующее временное
сообщение `runtime_iteration_state`:

```json
{
  "type": "active_plan_state",
  "plan_id": "plan_...",
  "revision": 6,
  "status": "active",
  "goal": "Проверить и обновить проект",
  "current_node": {
    "node_id": "pnode_...",
    "title": "Проверить изменения",
    "kind": "validate"
  },
  "ready_nodes": [
    {
      "node_id": "pnode_...",
      "title": "Собрать данные",
      "kind": "collect"
    }
  ],
  "counts": {
    "total": 5,
    "pending": 2,
    "in_progress": 1,
    "blocked": 0,
    "done": 2,
    "failed": 0,
    "skipped": 0
  },
  "stalled": false
}
```

Projection не сохраняется отдельным постоянным message. Она каждый раз
строится из `PlanStore`, поэтому содержит current revision, не попадает в
source segment compaction и не создаёт вторую authoritative копию.

`ActiveAgentCycle` получает `active_plan_revision` в дополнение к уже
существующему `active_plan_id`.

После каждого mutating plan tool ответ содержит обновлённый
`ActivePlanState`. Отдельный `agent_plan_get` обычно нужен только после
resume, revision conflict, изменения стратегии или для подробного чтения.

### 86.2. Связь plan и tool calls

После активации plan содержательный `mcp_call_tool` разрешён только при наличии
ровно одного `in_progress` node.

Исключения:

- `mcp_list_servers`;
- `mcp_list_tools`;
- `mcp_get_tool_schema`;
- `mcp_get_runtime_context`;
- plan manager tools.

Если active node отсутствует:

```json
{
  "type": "plan_node_required",
  "plan_id": "plan_...",
  "revision": 4,
  "ready_nodes": [],
  "message": "Перед содержательным tool call выбери и запусти ready node."
}
```

Tool trace и metadata большого результата получают:

```json
{
  "plan_id": "plan_...",
  "plan_revision": 5,
  "plan_node_id": "pnode_..."
}
```

Это создаёт точную provenance-связь result → plan node → cycle для `v0.5`
retrieval.

### 86.3. Result и artifact refs

При `complete`, `block` или `fail` LLM может предложить `result_refs` и
`artifact_refs`, но `PlanningService` принимает только IDs, уже
зарегистрированные в:

```text
active_cycle.result_refs
active_cycle.artifact_refs
```

Неизвестный ID отклоняется. Новая plan revision не создаётся автоматически
после каждого tool result: refs прикрепляются осмысленной lifecycle command,
а отдельные tool calls уже связаны с node через trace.

### 86.4. Final-answer guard

Если plan был активирован, runtime не принимает premature:

```json
{
  "status": "done",
  "action": "answer"
}
```

Финальный ответ разрешён только при:

```text
plan.status == completed
or plan.status == cancelled
```

Для active plan runtime возвращает:

```json
{
  "type": "plan_reconciliation_required",
  "plan_id": "plan_...",
  "revision": 8,
  "unfinished_node_ids": ["pnode_..."],
  "failed_node_ids": [],
  "message": "Перед финальным ответом заверши, измени или отмени активный план."
}
```

Message добавляется в следующий LLM request. Число reconciliation attempts
ограничивается конфигурацией; после повторного игнорирования возникает
controlled plan consistency error.

### 86.5. `AgentActivity`

Lifecycle status и activity остаются разными осями:

```text
status=RUNNING
activity=VALIDATING
```

```python
class AgentActivity(str, Enum):
    PLANNING = "planning"
    COLLECTING = "collecting"
    PROCESSING = "processing"
    EXECUTING = "executing"
    VALIDATING = "validating"
    FINALIZING = "finalizing"
```

| Node kind / runtime phase | Agent activity |
| --- | --- |
| `collect` | `collecting` |
| `process` | `processing` |
| `execute` | `executing` |
| `validate` | `validating` |
| `coordinate` | `planning` или `executing` |
| active node отсутствует | `planning` или обычный `running` |
| final pipeline | `finalizing` |

LLM не управляет activity отдельным tool. Runtime выводит её из active
operation и использует для trace, progress и диагностики.

---

## 87. Progress и trace

Plan events:

```text
plan_created
plan_revised
plan_node_started
plan_node_completed
plan_node_blocked
plan_node_failed
plan_node_skipped
plan_completed
plan_cancelled
plan_revision_conflict
plan_validation_failed
plan_finalization_blocked
```

Trace хранит:

```text
plan_id
revision before/after
node_id
transition
counts
validation code
```

Trace не хранит полный plan JSON, длинные descriptions, raw results, tool
output и полный `outcome_summary`.

User-visible progress:

```text
🗺️ Создан план работы.
▶️ Выполняю этап: Проверить конфигурацию
⏸️ Этап заблокирован.
✅ План выполнен.
```

Structural revision без смены active work остаётся internal/debug.

Cycle compaction сохраняет только runtime-owned `active_plan_id`,
`active_plan_revision` и `active_plan_node_id`. Полный plan и вычисляемые
ready/stalled projections повторно из source segment не восстанавливаются.

---

## 88. Scope, configuration и переход к следующим версиям

### 88.1. Ограничения `v0.4`

```text
один active plan на cycle
один in_progress node
не более 32 nodes
не более 16 dependencies на node
нет subplans
нет conditional expressions
нет loop edges
нет parallel execution
нет automatic retry
нет node timeout
нет worker assignment
нет background scheduler
нет automatic tool selection per node
```

```python
class PlanningConfigType(BaseModel):
    enabled: bool = True
    max_nodes: int = 32
    max_dependencies_per_node: int = 16
    max_ready_nodes_in_context: int = 5
    max_reconciliation_attempts: int = 2
```

### 88.2. Что переносится в `v0.5`

- PostgreSQL `PlanStore`;
- exact SQL projections;
- persistent plan revision history;
- cross-cycle plan continuation;
- связь plan nodes с session memory;
- RAG по outcome, history и связанным results;
- retrieval сохранённых результатов по node;
- optional planner/critic LLM;
- richer plan queries;
- persistent `estimator_identity` и другие runtime snapshots.

Current plan и в `v0.5` читается точно, а не через vector search.

### 88.3. Что переносится в `v0.6`

- automatic scheduler;
- несколько `in_progress` nodes;
- parallel ready branches;
- workers;
- node retries/backoff;
- timeouts/deadlines;
- distributed locks;
- durable event bus;
- idempotent node execution;
- automatic resume;
- human approval gates;
- automatic branch conditions;
- subplans;
- compensation/rollback workflows.

Именно в `v0.6` plan превращается из карты в исполняемый workflow.

### 88.4. Итоговые решения `v0.4-dag-planning`

1. DAG необязателен и создаётся только когда снижает риск ошибки или потери
   состояния.
2. Plan является отдельным domain artifact, а не message, summary или
   RAG-документом.
3. Полный current plan читается точными manager tools.
4. `ready` и `stalled` являются вычисляемыми projections.
5. LLM управляет plan через typed commands, а не raw JSON patch.
6. Runtime обеспечивает один active node, association tool calls, revision
   checks и final-answer guard.
7. `v0.4` не исполняет DAG автоматически и создаёт фундамент для
   PostgreSQL/RAG `v0.5` и scheduler `v0.6`.

---

# Часть VIII-E. v0.4 Unified Input and Artifact Architecture

## 89. Назначение и архитектурные инварианты

`v0.4-file-artifacts` и `v0.4-input-runtime` являются двумя последовательными,
но тесно связанными обновлениями. Они используют один transport-independent
контракт входных данных и артефактов, однако реализуются отдельно:

```text
v0.4-file-artifacts
→ initial input, file ingress, artifact workspace, processing и delivery

v0.4-input-runtime
→ дополнительные committed InputBatch во время active cycle,
   CycleInbox, safe checkpoints и session coordination
```

Общая формула:

```text
client transport event
≠ logical user input
≠ committed InputBatch
≠ agent cycle
≠ artifact payload
≠ client delivery
```

Основные инварианты:

1. Клиент не создаёт authoritative runtime IDs, statuses, provenance и refs.
2. Агент запускается только на полностью committed `InputBatch`.
3. Файлы не передаются в JSON как base64, локальные пути или download URL.
4. Канонические байты хранятся в `ContentStore`; artifact domain хранит
   пользовательскую семантику, lineage, immutable versions и `content_id`.
5. `artifact_id` обозначает точную неизменяемую версию, а не mutable latest file.
6. После commit `InputBatch` и `ArtifactVersion` не изменяются.
7. Client-specific grouping заканчивается до `MessageProcessor` и Agent Runtime.
8. `CycleInbox` принимает только ссылки на committed batches, а не Telegram/Web
   transport messages.
9. Agent Runtime не вызывает Telegram/Web API и не владеет bot tokens,
   multipart streams или client download URLs.
10. Выполнение задачи, доставка текста и доставка каждого файла являются
    разными наблюдаемыми lifecycle.
11. Все входные файлы, tool outputs и extracted document text считаются
    недоверенными данными и не становятся инструкциями для агента.
12. Filesystem-реализация `v0.4` рассчитана на один runtime process; межпроцессная
    атомарность и distributed locks относятся к `v0.5/v0.6`.

Архитектура должна поддерживать Telegram, будущий Web, CLI и новые клиенты без
изменения agent loop и artifact manager tools.

---

## 90. Сквозной lifecycle

### 90.1. Входной путь

```text
Client transport
→ durable ClientIngressEvent
→ client grouping policy
→ InputBatchDraft
→ streaming ArtifactIngressService
→ committed Content + input ArtifactVersion
→ atomic CommittedInputBatch
→ CycleAdmissionService
   ├─ session idle    → initial ActiveAgentCycle
   └─ cycle active    → CycleInbox
→ bounded AgentInputBatch / input_batch_update
→ LLM + manager/MCP tools
```

### 90.2. Обработка и создание результата

```text
input artifact
→ artifact manager tool или artifact binding в mcp_call_tool
→ internal text operation / external MCP processor
→ ContentRef или ArtifactCandidate
→ explicit artifact promotion/version creation
→ validation
→ exact artifact version selected for delivery
```

### 90.3. Исходящий путь

```text
AgentResult(final text + ArtifactDeliveryRef[])
→ durable ClientResponseOutbox
→ client-specific DeliveryPlanner
→ Telegram/Web/CLI sink
→ per-item delivery receipt
→ delivered / failed / unknown
```

Agent cycle не запускается повторно из-за ошибки доставки. Delivery retry не
должен повторять LLM-работу или tool side effects.

---

## 91. Общие идентификаторы и входные модели

Opaque IDs:

```text
evt_*   ingress event
ibat_*  input batch
aln_*   artifact lineage
art_*   immutable artifact version
cnt_*   canonical content payload
cand_*  artifact candidate from tool output
dlv_*   client delivery
resp_*  durable client response
ibox_*  CycleInbox item
ctrl_*  session control command
```

### 91.1. `ClientIngressEvent`

Одно событие транспортного клиента:

```python
class ClientIngressEvent(BaseModel):
    event_id: str
    client_type: ClientType
    client_instance_id: str

    conversation: ClientConversationRef
    sender: ClientSenderRef

    source_update_id: str | None
    source_message_id: str
    source_group_id: str | None
    reply_to_message_id: str | None

    occurred_at: datetime
    text_parts: list[IngressTextPart]
    attachment_slots: list[IngressAttachmentSlot]

    locale: str | None
    admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO
    metadata: dict[str, Any]
```

`conversation` содержит нормализованные `conversation_id` и optional
`thread_id`. `sender` содержит stable external principal ID. Session key строит
server-side `SessionKeyPolicy`; transport metadata не используется как
произвольный источник `session_id`.

### 91.2. Text и attachment parts

```python
class IngressTextPart(BaseModel):
    kind: Literal["message_text", "caption"]
    text: str
    attachment_slot_ids: list[str]


class IngressAttachmentSlot(BaseModel):
    slot_id: str
    media_kind: str
    original_filename: str | None
    declared_mime_type: str | None
    declared_size_bytes: int | None
    transport_locator: ClientAttachmentLocator
    upload_field_name: str | None
    metadata: dict[str, Any]
```

Caption сохраняет связь с конкретным attachment/message. Несколько captions не
склеиваются безусловно в одну строку.

### 91.3. Server ownership

Клиент может передать correlation/idempotency key, текст, порядок частей,
filename, declared MIME и bytes/locator. Только runtime создаёт:

```text
event_id, input_batch_id, content_id, lineage_id, artifact_id,
session_id, sequence_number, hashes, detected MIME, statuses,
provenance, cycle association и delivery IDs.
```

Web-клиент может отправить структурированный `ClientInputEnvelope`, но не готовый
authoritative `CommittedInputBatch`.

---

## 92. Durable ingress и `InputBatch`

### 92.1. Сохранение события до acknowledgement

Webhook/HTTP request получает success только после durable persistence события:

```text
validate client authentication
→ normalize event
→ IngressEventStore.save_if_absent(idempotency_key)
→ atomic commit
→ 2xx / InputSubmissionReceipt
→ asynchronous ingress dispatcher
```

Если событие невозможно надёжно сохранить, transport возвращает retryable
ошибку. Нельзя отвечать Telegram `200 OK` после одного `asyncio.create_task`,
когда update ещё существует только в памяти процесса.

`IngressEventRecord`:

```text
received → normalized → dispatched
                    ↘ failed
```

Telegram idempotency key:

```text
telegram + bot_instance_id + update_id
```

Web idempotency key scoped к authenticated principal/session.

### 92.2. `InputBatchDraft`

Mutable server-side draft:

```python
class InputBatchDraft(BaseModel):
    input_batch_id: str
    session_id: str
    client_type: ClientType
    conversation: ClientConversationRef
    sender: ClientSenderRef

    grouping_mode: InputGroupingMode
    grouping_key: str
    state: InputBatchDraftState

    source_events: list[InputEventRef]
    text_parts: list[InputTextPart]
    attachment_parts: list[InputAttachmentPart]

    opened_at: datetime
    last_event_at: datetime
    quiet_deadline: datetime | None
    sealing_deadline: datetime | None
    maximum_deadline: datetime | None

    response_route: ClientResponseRoute
```

State machine:

```text
collecting
→ sealing
→ ingesting
→ ready_to_commit
→ committed

collecting/sealing/ingesting
→ failed | cancelled | abandoned
```

Новый элемент во время `sealing` возвращает draft в `collecting` и обновляет
таймер. `maximum_deadline` не позволяет draft зависнуть навсегда.

### 92.3. `CommittedInputBatch`

Immutable logical user input:

```python
class CommittedInputBatch(BaseModel):
    input_batch_id: str
    session_id: str
    client_type: ClientType
    sequence_number: int

    source_event_ids: list[str]
    text_parts: list[InputTextPart]
    artifact_refs: list[str]
    referenced_artifact_refs: list[str]

    admission_mode: InputAdmissionMode
    response_route: ClientResponseRoute

    continuation_of_batch_id: str | None
    correction_of_batch_id: str | None

    committed_at: datetime
    commit_reason: InputBatchCommitReason
    content_fingerprint: str
```

После commit нельзя добавлять файлы, менять текст, порядок, conversation или
refs. Поздний fragment/edit создаёт continuation/correction batch.

### 92.4. Atomic commit

```text
1. draft lock acquired
2. все expected events зарегистрированы
3. все attachment states == stored
4. content/artifact refs проверены
5. batch/file/count/size limits проверены
6. immutable payload сформирован
7. committed manifest опубликован atomic replace
8. input_batch_committed event записан
9. admission dispatcher получает batch
```

Если один файл не сохранён, агент не запускается с частичным набором. Уже
созданные orphan contents остаются недоступны runtime и очищаются sweeper после
grace period.

### 92.5. `InputBatchStore`

```text
storage/input_batches/ibat_*/
  draft.json
  committed.json
  events/evt_*.json
```

`committed.json` authoritative. Commit идемпотентен. После crash committed, но
ещё не consumed batch повторно передаётся admission dispatcher.

---

## 93. Практическая группировка по клиентам

`InputBatchAssembler` реализует общую state machine. Client adapter предоставляет
`InputGroupingPolicy`:

```python
class InputGroupingPolicy(Protocol):
    def resolve_grouping(
        self,
        event: ClientIngressEvent,
        open_drafts: Sequence[InputBatchDraftRef],
    ) -> GroupingDecision: ...
```

### 93.1. Web

Web отправляет atomic multipart request:

```text
manifest JSON + file parts
→ один draft
→ stream всех частей
→ immediate commit после полной валидации
```

При disconnect до commit draft становится failed/abandoned, временные payloads
не публикуются, agent cycle не запускается. Retry с тем же `Idempotency-Key`
возвращает прежний result/receipt и не повторяет side effects.

Большой Web upload в будущем может использовать resumable upload session, но
итоговый `CommittedInputBatch` остаётся тем же.

### 93.2. Telegram: text и command

- text без открытого attachment draft → immediate batch;
- command → отдельный control/input event и не объединяется с файлами;
- text при ровно одном открытом standalone attachment draft → instruction к
  draft и explicit/fast commit;
- text после commit → новый batch.

### 93.3. Telegram media group

Telegram присылает элементы альбома отдельными messages с общим
`media_group_id`. Grouping key:

```text
bot instance + conversation + thread + sender + media_group_id
```

Порядок определяется `source_message_id`, а не временем обработки webhook.
Окончание группы определяется:

```text
quiet timeout
→ sealing grace
→ commit
```

Отдельного `album_finished` event нет. Новая часть в sealing возвращает draft в
collecting. Часть, пришедшая после commit, создаёт continuation batch с причиной
`late_media_group_fragment`; committed batch не открывается повторно.

### 93.4. Telegram standalone files

Без `media_group_id` намерение неоднозначно. Поэтому у пользователя в одной
conversation/thread допускается максимум один standalone attachment draft.
После первого файла бот показывает один status с действиями:

```text
Начать обработку | Добавить ещё | Отменить
```

Последующие файлы и текст добавляются в draft. Explicit commit предпочтителен;
maximum timeout выполняет bounded auto-commit. Filename не используется для
автоматического определения версии.

### 93.5. Telegram file provider

Webhook не держит соединение во время скачивания файла. После durable event
`ArtifactIngressService` получает bytes через закрытый `ClientFileProvider`:

```text
Gateway/Ingress
→ authenticated TelegramFileProvider
→ getFile(file_id)
→ stream в ContentStore
```

Bot token и временный Telegram download URL не покидают Telegram service и не
сохраняются в event metadata. Исходные filename/MIME сохраняются из Message,
поскольку transport download metadata может быть неполной.

### 93.6. Duplicate, ordering, edit и reply

- duplicate update отбрасывается по idempotency key;
- per-conversation/sender lock защищает порядок и draft mutation;
- edited message до commit обновляет draft;
- edit после commit создаёт correction batch;
- reply разрешается через `ClientMessageBindingStore`;
- reply может явно повторно ввести ранее доставленный/input artifact в текущий
  или новый cycle, но только после ownership checks.

### 93.7. Session scope

Recommended defaults:

```text
Telegram private chat     → conversation
Telegram group            → conversation + sender
Telegram topic/thread     → conversation + thread + sender
Web                       → authenticated principal + explicit session
```

Collaborative group session включается явно. События разных senders не
объединяются в один draft автоматически.

---

## 94. Streaming, лимиты и безопасность ingress

`ContentStore` расширяется:

```python
async def save_stream(
    chunks: AsyncIterator[bytes],
    *,
    source_type: str,
    source_name: str | None,
    mime_type: str | None,
    metadata: dict[str, Any],
    max_size_bytes: int,
) -> ContentRef: ...

async def iter_content(
    content_id: str,
    *,
    chunk_size: int,
) -> AsyncIterator[bytes]: ...
```

`save_stream` пишет temporary payload, одновременно считает size/hash, проверяет
лимит и публикует content atomic replace. Gateway/API не загружают крупный файл
полностью в RAM.

Лимиты многоуровневые:

```text
per-file bytes
per-batch total bytes
attachments/events/text per batch
open drafts per principal
queued batches/bytes per session
global concurrent downloads
temporary storage budget
client transport limit
```

Filename sanitization удаляет path components/control chars и не влияет на
storage path. Declared MIME недоверенный. Сохраняются `declared_mime_type`,
`detected_mime_type`, `format_id`; для известных форматов выполняется bounded
signature/container validation. Core не исполняет scripts/macros и не делает
автоматическую распаковку архивов или Office containers.

`photo` и `document` считаются разными transport media kinds. Telegram photo
может быть преобразовано платформой; provenance отмечает
`transport_transformation_possible=true`. Для точного возврата файл по умолчанию
отправляется как document.

---

## 95. Admission и session coordination

```python
class CycleAdmissionService(Protocol):
    async def submit(
        self,
        batch: CommittedInputBatch,
    ) -> InputSubmissionResult: ...
```

Реализации:

```text
DirectCycleAdmissionService (v0.4-file-artifacts)
  idle session  → start cycle
  active cycle  → session_busy

InboxCycleAdmissionService (v0.4-input-runtime)
  idle session  → start cycle
  active cycle  → enqueue CycleInbox
```

`SessionRuntimeState`:

```text
session_id
generation
active_cycle_id
resumable_cycle_id
next_input_sequence
state: idle | running | waiting_user | finalizing
```

Один per-session lock защищает start/enqueue, active cycle pointer,
`WAITING_USER`, finalization и terminal commit. Перед terminal commit runtime под
тем же lock повторно проверяет inbox; принятый batch не может потеряться в гонке
между последней проверкой и `DONE`.

`InputAdmissionMode`:

```text
auto
continue_cycle
new_cycle
```

Web может указывать mode явно. Telegram reply на active-cycle message означает
`continue_cycle`, `/new` — `new_cycle`, обычный input — `auto`. Параллельные
независимые cycles одной session в `v0.4` не поддерживаются.

---

# Часть VIII-F. v0.4-file-artifacts

## 96. Artifact domain и storage

### 96.1. Разделение сущностей

```text
Content       — immutable canonical bytes/text
StoredResult  — сохранённый tool result
Artifact      — пользовательский/рабочий/выходной именованный файл
```

Artifact payload хранится только в `ContentStore`. `ArtifactStore` хранит
lineage/version metadata и `content_id`, но не вторую копию `file.bin`.

### 96.2. IDs и linear version history

```text
aln_* — logical artifact lineage
art_* — exact immutable version
cnt_* — exact payload
```

```text
aln_report
 ├─ art_v1 → cnt_a
 ├─ art_v2 → cnt_b
 └─ art_v3 → cnt_c (current head)
```

В `v0.4` история линейная. Mutation принимает `current_artifact_id` как optimistic
concurrency token. Если head уже изменился, возвращается
`artifact_version_conflict`; silent merge и branching запрещены.

Конвертация формата обычно создаёт новый lineage. Например DOCX → PDF не является
новой версией DOCX.

### 96.3. Models

`ArtifactLineage`:

```text
artifact_lineage_id
session_id
created_cycle_id
current_artifact_id
current_version
purpose: input | working | deliverable
status: active | archived
title
created_at / updated_at
metadata
```

`ArtifactVersion`:

```text
artifact_id
artifact_lineage_id
version
parent_artifact_id
content_id
filename
format_id
encoding
declared_mime_type
detected_mime_type
size_bytes
content_hash
created_cycle_id
created_at
provenance
metadata
```

Provenance runtime-owned и включает origin, source content/result/artifact refs,
tool call, plan/node association и creator. LLM не задаёт provenance напрямую.

### 96.4. Filesystem layout и atomic head

```text
storage/
  contents/cnt_*/content.bin + metadata.json
  artifacts/lineages/aln_*/metadata.json
  artifacts/versions/art_*/metadata.json
  deliveries/cycles/<cycle_id>.json
```

Новая version metadata публикуется раньше atomic update lineage head. Orphan
version, не указанная committed manifest, не доступна через public service и
очищается позднее.

---

## 97. Масштабируемый format registry

`format_id` не является закрытым Enum. Форматы регистрируются через
`ArtifactFormatSpec`:

```python
ArtifactFormatSpec(
    format_id="docx",
    canonical_mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    extensions=(".docx",),
    content_kind="binary_container",
    capabilities={...},
)
```

Capabilities:

```text
can_create_inline
can_read_text
can_search_text
can_replace_text
can_patch_text
can_deliver
can_bind_to_tool
requires_external_processor
```

Уровни `v0.4`:

1. Native text: TXT, Markdown, JSON, CSV, source/config text — create/read/search/
   replace/patch/version/deliver.
2. Managed binary: PDF, DOCX, XLSX, PPTX, PNG, JPEG, WebP — store/version/bind/
   deliver; semantic processing через MCP.
3. Opaque allowed binary — store/version/deliver при policy.

JSON и другие structured text проходят syntax validation после create/edit.
Password-protected/unsupported container может храниться, но получает ограниченные
capabilities.

---

## 98. Artifact manager functions и runtime state

Command-oriented tools:

```text
artifact_list
artifact_get
artifact_read_text
artifact_search_text
artifact_create_text
artifact_create_from_content
artifact_replace_text
artifact_patch_text
artifact_create_version_from_content
artifact_set_delivery
```

Правила:

- tools не принимают session/cycle/client type/local path;
- runtime проверяет current-cycle access set;
- read/search bounded и возвращают offsets/excerpts;
- exact patch использует `old_text`, `new_text`, `expected_occurrences`;
- fuzzy/line-number patch отсутствует;
- mutation создаёт новую immutable version;
- unknown refs и stale head отклоняются;
- `purpose=deliverable` может автоматически выбрать current version для delivery;
- обновление deliverable переносит selection на новый committed head.

`runtime_iteration_state` получает bounded `artifact_state`:

```json
{
  "available_count": 3,
  "items": [
    {
      "artifact_id": "art_...",
      "artifact_lineage_id": "aln_...",
      "version": 2,
      "filename": "report.md",
      "purpose": "deliverable",
      "format_id": "markdown",
      "size_bytes": 12400,
      "is_current": true,
      "selected_for_delivery": true
    }
  ],
  "items_truncated": false
}
```

Content, full provenance и version history автоматически в LLM context не
попадают. Compactor сохраняет только runtime-owned artifact refs; authoritative
state восстанавливается из store после resume/compaction.

---

## 99. Работа с внутренними и внешними tools

### 99.1. Artifact binding в `mcp_call_tool`

```json
{
  "artifact_inputs": [
    {
      "artifact_id": "art_...",
      "argument_name": "input_file",
      "transfer_mode": "auto"
    }
  ]
}
```

Modes:

```text
auto | local_path | inline_text | base64
```

Runtime проверяет schema, ownership, format capability и размер. `local_path`
доступен только доверенному local executable/stdio server. Remote HTTP MCP не
получает путь локальной машины. LLM не получает materialized path.

### 99.2. Per-call workspace

```text
workspace/<tool_call_id>/
  inputs/   read-only copies
  outputs/  allowed output root
  temp/
```

Canonical content immutable. In-place processor работает с копией. Output path
принимается только внутри выделенного workspace и импортируется в `ContentStore`.
Произвольный путь из tool output отклоняется. Workspace имеет lease и cleanup.

### 99.3. MCP output normalization

Text остаётся tool result. Embedded image/audio/resource/blob сохраняется как
content candidate. Binary/base64 не попадает в messages_for_llm.

```json
{
  "type": "artifact_candidate",
  "candidate_id": "cand_...",
  "content_id": "cnt_...",
  "filename": "result.docx",
  "mime_type": "...",
  "size_bytes": 18422,
  "source_tool_call_id": "...",
  "needs_promotion": true
}
```

Агент явно создаёт новый artifact либо version. Каждый технический output не
становится пользовательским artifact автоматически. Partial tool success сохраняет
полезные candidates и отдельно фиксирует limitations.

Предпочтительный контракт будущих Word/PDF/PowerPoint/Excel MCP servers —
embedded resource или structured output manifest, а не произвольный local path.

---

## 100. Delivery, outbox и client bindings

Delivery selection является отдельной сущностью, а не mutable полем immutable
ArtifactVersion:

```text
selected → delivering → delivered | failed | unknown
```

`AgentResult` содержит `ArtifactDeliveryRef[]`, но не bytes. Перед `DONE` runtime
проверяет, что selected versions committed, доступны session и проходят integrity
check.

`ClientResponseOutbox` сохраняется до terminal publication:

```text
pending → claimed → partially_delivered → delivered | failed
```

Crash после LLM final answer не теряет result; delivery продолжается после
restart без повторного agent cycle.

Telegram/Web transport получает `delivery_id`, потоково читает content и пишет
receipt. Execution success, text delivery и каждый artifact delivery наблюдаются
раздельно. Неопределённый сетевой timeout после начала Telegram upload получает
`unknown`; бесконечный автоматический retry запрещён, поскольку может создать
дубликат.

`ClientMessageBindingStore` связывает incoming/outgoing client message IDs с
batch/cycle/artifact/delivery/waiting request. Reply позволяет безопасно сослаться
на ранее доставленный artifact после ownership check.

Client DeliveryPlanner учитывает capabilities/limits. Telegram по умолчанию
использует document, а media group только для совместимых типов/количества. Web
выдаёт authenticated streaming download endpoint; опасные HTML/SVG/script formats
не открываются inline по умолчанию.

Artifact progress/trace:

```text
artifact_ingested
artifact_created
artifact_version_created
artifact_version_conflict
artifact_validation_failed
artifact_candidate_saved
artifact_tool_input_materialized
artifact_tool_input_released
artifact_delivery_selected
artifact_delivery_started
artifact_delivery_done
artifact_delivery_failed
```

Events содержат IDs/version/filename/format/size и plan provenance, но не content,
base64, patch text или temporary paths.

---

# Часть VIII-F2. v0.4-input-runtime

## 101. `CycleInbox`

`CycleInbox` хранит только committed batch refs:

```python
class CycleInboxItem(BaseModel):
    inbox_item_id: str
    cycle_id: str
    input_batch_id: str
    sequence_number: int

    status: Literal[
        "queued", "claimed", "applied", "failed", "cancelled"
    ]

    claim_token: str | None
    claim_expires_at: datetime | None
    enqueued_at: datetime
    claimed_at: datetime | None
    applied_at: datetime | None
```

At-least-once delivery компенсируется idempotent consumption по
`input_batch_id`. Просроченный claim после restart возвращается в queued.

`ActiveAgentCycle` хранит bounded replay state:

```text
applied_input_batch_ids
last_applied_inbox_sequence
```

Unread batches остаются authoritative в `CycleInboxStore` и не включаются в
cycle compaction summary.

---

## 102. Safe checkpoints и control plane

Обычный batch применяется только:

1. перед следующим main LLM request;
2. после полного assistant tool-call block и всех role=tool results;
3. перед `WAITING_USER`;
4. перед final processing/terminal commit;
5. после resume перед первой новой iteration.

Новый input не вставляется между assistant tool call и соответствующим tool
result. Если batch пришёл во время LLM call, уже выбранный tool action не
игнорируется: tool block завершается, затем выполняется checkpoint.

Перед `DONE`/`WAITING_USER` inbox проверяется повторно. Если batch уже принят,
terminal action подавляется, batch применяется, cycle продолжается.

Несколько batches представляются одной runtime-generated user message, но границы
сохраняются:

```json
{
  "type": "input_batch_update",
  "batches": [
    {
      "input_batch_id": "ibat_...",
      "text_parts": [],
      "artifacts": [],
      "continuation_of_batch_id": null,
      "correction_of_batch_id": null
    }
  ],
  "runtime_generated": true
}
```

Управляющие команды отделены от обычного FIFO:

```text
cancel_cycle
cancel_draft
reset_session
start_new_cycle
commit_draft
```

`SessionControlInbox` проверяется до side-effect tool, между последовательными
tool calls, перед finalization и delivery. Если `/cancel` пришёл после первого из
нескольких native tool calls, оставшиеся calls получают корректные cancelled
`role=tool` results, чтобы не нарушить OpenAI-compatible sequence.

---

## 103. Resume, correction и finalization races

Применение inbox replay-safe:

```text
claim item
→ проверить applied_input_batch_ids
→ добавить one input_batch_update
→ добавить runtime-owned artifact refs
→ persist cycle replay state
→ mark item applied
```

После crash повтор не добавляет batch второй раз.

`WAITING_USER`:

```text
inbox уже содержит ответ
→ не переходить в waiting
→ применить batch

waiting уже committed
→ новый AUTO batch enqueue
→ resume того же cycle
```

Edited message после commit создаёт correction batch. Late album fragment создаёт
continuation batch. Они не изменяют исторический committed input.

Finalization race закрывается одним session lock:

```text
begin finalization
→ lock
→ final inbox/control recheck
→ если input есть: cancel finalization и continue
→ иначе: durable final result + outbox + terminal state
```

Нельзя доставить ответ, который заведомо игнорирует уже accepted batch.

---

## 104. Recovery, configuration, scope и acceptance

После restart восстанавливаются:

```text
received/dispatched ingress events
open/sealing/ingesting drafts
committed-but-unconsumed batches
expired CycleInbox claims
active/resumable cycle references
pending client response outbox
delivery states/tool workspaces cleanup
```

Basic sweeper удаляет только expired temporary uploads, abandoned drafts, orphan
content после grace period и expired workspaces. Committed artifacts автоматически
не удаляются.

Configuration categories:

```text
artifacts:
  max_artifacts_per_cycle
  max_versions_per_lineage
  max_artifact_size_bytes
  max_inline_text_chars / max_read_chars / max_search_matches
  max_patch_operations
  max_runtime_artifact_summaries
  allow_opaque_binary
  auto_select_deliverables

input_runtime:
  max_events_per_batch
  max_attachments_per_batch
  max_batch_total_bytes
  max_open_drafts_per_principal
  max_queued_batches/bytes_per_session
  claim_lease_seconds

telegram_input:
  album_quiet_timeout_seconds
  album_sealing_grace_seconds
  album_maximum_wait_seconds
  standalone_attachment_maximum_wait_seconds
```

Concrete timing/size values определяются тестами и client capabilities, но сами
лимиты обязательны.

Acceptance `v0.4-file-artifacts`:

- atomic Web text+files ingress;
- Telegram text, single file, media group и standalone draft;
- no cycle start before full batch commit;
- immutable input artifact and versioned edits;
- native text create/read/search/replace/patch;
- binary bind to trusted MCP processor;
- candidate promotion/version creation;
- deliverable selected, persisted in outbox and streamed to Telegram/Web;
- crash/retry does not lose accepted input or duplicate artifact versions.

Acceptance `v0.4-input-runtime`:

- batch during LLM/tool execution is queued, not injected concurrently;
- safe checkpoint preserves assistant/tool protocol;
- accepted input suppresses stale DONE/WAITING_USER;
- multiple queued batches preserve order and boundaries;
- duplicate/replayed batch is applied once;
- `/cancel` control command is observed before next side effect;
- finalization/enqueue race is closed under session lock;
- restart restores inbox claims and resumable cycle state.

Не входят в `v0.4`:

```text
semantic RAG по artifact history
PDF/DOCX/XLSX/PPTX extraction in core
OCR, previews, thumbnails
antivirus/macro sandbox
object storage и presigned URLs
resumable large Web upload
branching/merge artifact versions
cross-cycle automatic artifact search
multi-process/distributed admission
background conversion workers
automatic retry of ambiguous client delivery
```

# Часть VIII-G. Реализация v0.4

## 105. Пакеты

### `v0.4-storage-foundation`

- `ContentStore`;
- `ArtifactStore`;
- models refs;
- filesystem backend;
- atomic writes;
- configuration.

### `v0.4-result-compaction`

- `result_handling`;
- relative budgets;
- raw result persistence;
- single-pass LLM-summary;
- oversized fallback;
- progress/trace events.

### `v0.4-cycle-compaction`

- active cycle object;
- atomic segment selection;
- `CycleWorkingMemory`;
- no summary tree;
- generations;
- recovery tests.

### `v0.4-dag-planning`

- filesystem `PlanStore` with revisions and optimistic concurrency;
- typed `AgentPlan` / `PlanNode` models;
- computed ready/stalled projections;
- `PlanningService` validation and strict node transitions;
- seven command-oriented manager tools;
- bounded `ActivePlanState` in runtime context;
- runtime-owned plan/node/result/artifact refs;
- tool-call association and final-answer guard;
- progress/trace events without scheduler semantics.

### `v0.4-file-artifacts`

- durable `ClientIngressEvent` and initial `InputBatchDraft`;
- streaming `ContentStore` ingress/egress;
- artifact lineage and immutable versions;
- format registry and bounded text operations;
- artifact manager tools and MCP bindings;
- candidate promotion and provenance;
- `AgentResult.artifacts`;
- durable response outbox and Telegram/Web delivery.

### `v0.4-input-runtime`

- immutable `CommittedInputBatch`;
- `CycleInbox<CommittedInputBatch>` with leases/idempotency;
- session admission and runtime manifest;
- safe checkpoints and batch update messages;
- `SessionControlInbox`;
- per-session lock and finalization race guard;
- resume/replay after restart.

---

## 106. Порядок реализации

```text
1. storage foundation
2. result compaction
3. cycle compaction
4. DAG planning
5. file artifacts
6. input runtime
7. integration tests
8. docs/README stabilization
```

Storage идёт первым, потому что все последующие подсистемы используют refs.

Input runtime идёт после file artifacts, потому что batch может содержать attachments.

---

## 107. Acceptance criteria v0.4

### Small/large/oversized result и cycle compaction

Сохраняются критерии `v0.4-result-compaction` и `v0.4-cycle-compaction`:

```text
small result → inline → immediate use
large result → original persisted → compact/store_only → no raw in context
oversized result → needs_retrieval=true → no false complete summary
context trigger → closed atomic segment → one CycleWorkingMemory
repeated compaction → one visible generation → no summary tree
```

### DAG

```text
invalid mutation/revision
→ validation/conflict
→ stored plan remains intact
```

```text
active plan + no in_progress node
→ substantive tool call blocked
```

```text
active unresolved plan + final answer
→ reconciliation or controlled resumable handoff
```

### Atomic client input

```text
Web multipart text + files
→ all streams stored and validated
→ one committed InputBatch
→ one cycle
```

```text
one attachment fails
→ batch failed
→ cycle not started with partial input
```

### Telegram grouping

```text
media_group messages
→ one draft by media_group_id
→ quiet + sealing
→ one committed InputBatch
```

```text
late media-group fragment after commit
→ continuation batch
→ original batch remains immutable
```

```text
standalone files + text
→ one explicit attachment draft
→ commit button/command or bounded timeout
```

### Artifact versioning

```text
agent edits text artifact
→ new art_* version in same aln_*
→ original exact version remains available
→ stale current_artifact_id rejected
```

```text
DOCX processor returns modified bytes
→ content candidate
→ explicit new version
→ no arbitrary local path in LLM context
```

### Active-cycle input

```text
batch during LLM/tool execution
→ durable CycleInbox
→ current atomic action finishes
→ batch applied at safe checkpoint
```

```text
batch accepted before terminal commit
→ stale DONE/WAITING_USER suppressed
→ cycle continues
```

```text
duplicate input/inbox replay
→ applied once by stable IDs
```

### Delivery

```text
final answer + deliverable artifact
→ durable response outbox
→ client-specific streaming delivery
→ independent text/file receipts
```

```text
process restart after final LLM answer
→ outbox replay
→ no repeated agent cycle
```

### Recovery and isolation

```text
restart
→ persisted drafts/batches/inbox/outbox recovered
```

```text
group chat users
→ session/grouping policy prevents accidental cross-user file access
```

---

## 108. Что переносится

В `v0.5`:

- PostgreSQL implementations для content/artifact/input/inbox/outbox stores;
- transactional session admission и terminal commit;
- persistent artifact lineage/version/delivery relations;
- session/cross-cycle artifact discovery с exact authorization;
- lazy extraction сложных файлов;
- page/sheet/slide/section representations;
- persistent chunk cache и embeddings;
- keyword/semantic/hybrid RAG;
- search over old cycles/results/artifacts;
- provenance-aware retrieval по plan node и artifact version;
- processing oversized sources by parts;
- durable resume после process restart;
- optional object storage backend для payloads.

В `v0.6`:

- Redis/arq и distributed locks;
- durable distributed ingress/admission/CycleInbox;
- background workers для extraction, conversion, scanning и delivery;
- Notification/Delivery Service и durable event bus;
- automatic DAG scheduler и parallel nodes;
- background hierarchical summarization;
- resumable large uploads и remote signed URLs;
- antivirus/sandbox/preview pipelines;
- retention/garbage collection workers;
- multi-service AgentRun lifecycle;
- microservice runtime.

---

# Часть IX. v0.5 — PostgreSQL, lazy indexing и RAG для agent workspace

## 109. Главная идея v0.5

`v0.5` переносит memory/workspace metadata в PostgreSQL и добавляет retrieval по текущим и предыдущим cycles, stored results, files, trace/events и plan relations.

```text
PostgreSQL + pgvector
для долговременной памяти и agent workspace,
без полной микросервисной перестройки.
```

---

## 110. Совместимость backend

PostgreSQL implementations реализуют те же contracts, что filesystem backend `v0.4`.

Возможна hybrid storage:

```text
PostgreSQL
→ metadata, relations, statuses, indexes

filesystem / object storage
→ large binary/raw payloads
```

Agent loop не меняет business logic при смене backend.

---

## 111. Минимальные сущности PostgreSQL

```text
agent_sessions
session_runtime_states
agent_cycles
cycle_messages
cycle_trace_events
cycle_working_memories

stored_contents
stored_result_refs
result_compactions

artifact_lineages
artifact_versions
artifact_extractions
artifact_candidates
artifact_delivery_selections
artifact_deliveries
artifact_delivery_attempts

agent_plans
agent_plan_nodes
agent_plan_edges
agent_plan_revisions

ingress_events
input_batch_drafts
input_batches
input_batch_items
cycle_inbox_items
session_control_commands
client_message_bindings
client_response_outbox

content_chunks
chunk_embeddings
retrieval_events
```

Физические таблицы могут объединяться, но domain boundaries, immutable IDs,
ownership, status transitions и idempotency relations должны сохраниться.

---

## 112. Основные таблицы cycle

### `agent_sessions`

```text
id
client_type
external_user_id
created_at
updated_at
metadata_json
```

### `agent_cycles`

```text
id
session_id
status
activity
original_user_request
final_answer
error
error_kind
can_resume
active_plan_id
active_plan_revision
active_plan_node_id
working_memory_generation
created_at
updated_at
completed_at
metadata_json
```

### `cycle_messages`

```text
id
cycle_id
role
content_json
tool_call_id
message_index
input_batch_id
created_at
```

### `cycle_trace_events`

```text
id
cycle_id
event_type
payload_json
created_at
```

---

## 113. Stored content/results

### `stored_contents`

```text
id
storage_backend
storage_uri
mime_type
content_hash
size_bytes
size_chars
size_tokens_estimate
source_type
created_at
metadata_json
```

### `stored_result_refs`

```text
id
content_id
cycle_id
tool_call_id
tool_name
summary_status
summary_json
preview
needs_retrieval
created_at
metadata_json
```

---

## 114. Artifacts и plans

Artifacts:

```text
artifact_lineages
  id, session_id, current_artifact_id, current_version, purpose, status

artifact_versions
  id, lineage_id, version, parent_artifact_id, content_id,
  filename, format_id, detected_mime_type, size_bytes, content_hash,
  provenance_json, created_cycle_id

artifact_extractions
  artifact_id, extractor/version, extracted_content_id, status

artifact_candidates
  content_id, source_tool_call_id, status, promotion target

artifact_deliveries / attempts
  exact artifact_id, client target, state, attempt, receipt/error
```

Current artifact version читается точно по lineage head, а не через vector search.
Payload может оставаться на filesystem/object storage; PostgreSQL хранит metadata,
relations и transaction boundary.

Plans:

```text
agent_plans
agent_plan_nodes
agent_plan_edges
agent_plan_revisions
```

Plan revision может храниться snapshot или event log. Plan refs связываются с
exact artifact/result versions.

---

## 115. Ingress, batches, inbox и outbox

```text
ingress_events
input_batch_drafts
input_batches
input_batch_items
cycle_inbox_items
session_control_commands
session_runtime_states
client_message_bindings
client_response_outbox
```

PostgreSQL transaction закрывает критические переходы:

```text
persist/commit batch + admission decision
claim/apply inbox item + cycle replay state
final inbox recheck + terminal cycle state + response outbox
artifact version commit + lineage head update
delivery claim + attempt state
```

`CycleInbox` по-прежнему оперирует committed `InputBatch`, а не transport
messages. Persisted draft/event metadata позволяет восстановить pending input
после рестарта. Redis в `v0.5` не обязателен: database polling/notifications или
in-process dispatcher могут использовать те же contracts.

---

## 116. Lazy extraction и chunking

Первый retrieval:

```text
ContentRef / ArtifactRef
→ processor
→ text extraction при необходимости
→ chunker
→ chunks
→ embeddings
→ cached index
```

Chunking зависит от типа:

```text
plain text → paragraphs/semantic boundaries
PDF → pages/sections
spreadsheet → sheets/ranges
source code → modules/classes/functions
HTML → headings/content blocks
```

Chunks и embeddings можно перестроить при смене extractor/chunker version.

---

## 117. `content_chunks` и embeddings

```text
content_chunks:
  id
  content_id
  chunk_index
  text
  char_start
  char_end
  page_number
  section_path
  token_estimate
  chunker_name
  chunker_version
  metadata_json
```

```text
chunk_embeddings:
  id
  chunk_id
  embedding_model
  embedding
  created_at
```

`embedding` хранится через pgvector.

---

## 118. Retrieval tools

```text
agent_memory_get_cycle
agent_memory_search_cycles
content_get_metadata
content_read_range
content_list_chunks
content_get_chunk
content_search
artifact_get_metadata
artifact_search_content
agent_plan_get
agent_plan_search_results
```

```json
{
  "content_id": "cnt_...",
  "query": "ошибка подключения",
  "limit": 5,
  "search_type": "keyword | semantic | hybrid"
}
```

---

## 119. Retrieved context временный

```text
retrieve chunks
→ LLM uses data
→ extracted notes/facts persisted
→ raw chunks removed at later compaction
→ chunk IDs + content ID + facts remain
```

```json
{
  "type": "retrieved_context_summary",
  "content_id": "cnt_...",
  "used_chunk_ids": ["ch_1", "ch_8"],
  "extracted_facts": ["...", "..."],
  "retrieval_event_id": "ret_..."
}
```

---

## 120. RAG, activity и grounding

Retrieval выполняется при:

```text
status = RUNNING
activity = PROCESSING
```

Agent может чередовать processing, planning, executing, validating и collecting.

Для evidence сохраняется provenance:

```text
content_id
chunk_id/range
retrieval_event_id
source result/artifact
```

Final grounding использует только фактически retrieved или inline evidence.

---

## 121. Что не входит в v0.5

- обязательная микросервисная архитектура;
- Redis как обязательная runtime dependency;
- distributed workers как обязательный execution path;
- automatic DAG scheduler;
- automatic parallel plan nodes;
- массовая eager-индексация всех files;
- сложное branching/merge artifact versions;
- полноценный multi-tenant shared workspace;
- обязательный antivirus/macro sandbox;
- полная замена local development mode.

---

## 122. Acceptance criteria v0.5

```text
filesystem metadata backend заменяется PostgreSQL-compatible implementations
без переписывания agent loop, manager tools и client adapters.
```

```text
batch/admission/finalization critical transitions
→ one database transaction
→ no lost input or duplicate terminal response.
```

```text
first RAG read of PDF/DOCX/XLSX/PPTX
→ lazy extraction/chunking
→ provenance-aware index persisted
→ repeated retrieval uses cache.
```

```text
semantic/hybrid search returns exact refs + retrieved ranges/chunks,
not uncontrolled raw payload.
```

```text
cycle resume after process restart restores messages, working memory,
current plan, artifact access set, pending inbox and response outbox.
```

```text
artifact lineage/current head/version history and delivery state remain exact,
while extracted chunks/embeddings are rebuildable derived data.
```

---

# Часть X. v0.6 — microservices, workers и distributed runtime

## 123. Главная идея v0.6

`v0.6` разделяет выросшую систему на сервисы и переносит тяжёлые операции в workers.

```text
устойчивый distributed agent runtime
с durable queues, workers и service boundaries
```

Микросервисы нужны, когда появляются concurrent users, тяжёлая file processing, длительный indexing, независимые DAG nodes и resume после restart.

---

## 124. Возможные сервисы

```text
Gateway / Client API
→ Telegram/Web/CLI ingress authentication
→ durable ClientIngressEvent
→ client file providers and response routes
→ Web AgentRun endpoints

Ingress / Session Coordination Service
→ InputBatchDraft assembly
→ batch commit/idempotency
→ session admission and control commands
→ CycleInbox routing

Agent Runtime Service
→ agent cycle
→ LLM iterations
→ safe checkpoints
→ working memory
→ plan/artifact decisions
→ canonical progress/trace events

MCP Tool Runtime Service
→ MCP servers
→ lifecycle/recovery
→ isolated per-call artifact workspaces
→ tool output normalization

Memory / Workspace Service
→ sessions/cycles/content/results
→ artifact lineages/versions
→ plans/provenance/RAG

Worker Service
→ extraction/chunking/embeddings
→ conversion/rendering/scanning
→ hierarchical summarization
→ retention/cleanup

Notification / Delivery Service
→ durable response outbox
→ progress/final text/artifacts
→ Telegram/Web/CLI-specific planners and sinks
→ delivery attempts/receipts/retry policy
```

Не все services обязаны появиться одновременно. Local development mode может
использовать in-process implementations тех же ports.

---

## 124.1. Разделение Agent Runtime и client delivery

Agent Runtime и клиенты не должны использовать один общий модуль, смешивающий
domain events с UI/delivery-логикой.

Целевая граница:

```text
Agent Runtime
→ создаёт canonical ProgressEvent
→ сохраняет event как часть trace/source of truth
→ публикует event через transport port

Progress transport
→ local/in-process или HTTP callback в compatibility mode
→ PostgreSQL event log / Redis Stream / PubSub в distributed mode

Client delivery
→ принимает ProgressEvent envelope
→ применяет общий только для клиентов delivery lifecycle
→ передаёт event в client-specific sink
```

Agent Runtime:

- не вызывает Telegram/Web/CLI API;
- не управляет edit throttling, spinner, SSE/WebSocket reconnect или terminal UI;
- не решает, какие client-visible события можно coalesce;
- остаётся источником истины для `event_id`, `cycle_id`, event type, severity,
  visibility и безопасного structured payload.

Общая client-side логика может включать:

- delivery session на `request_id`/target;
- ordering и bounded buffering;
- deduplication/idempotency по `event_id`;
- lifecycle `active → closing → closed`;
- защиту от late delivery после final response;
- structured delivery receipts, metrics и safe logging;
- pluggable policy для throttling/coalescing.

Основное отображение остаётся индивидуальным:

```text
Telegram
→ edit одного status message
→ Telegram rate limits/retry
→ semantic coalescing

Web
→ WebSocket/SSE
→ sequence/reconnect/replay
→ timeline или concurrent stages

CLI
→ TTY line/spinner
→ plain lines или JSONL для non-TTY
→ controlled shutdown
```

Повторная доставка event bus должна быть безопасной: client consumer применяет
один `event_id` не более одного раза. Важные runtime stages не должны
безусловно вытесняться более новым transient event. Конкретная политика
отображения и coalescing принадлежит client adapter/sink, а не Agent Runtime.

Local development mode сохраняет прямой callback/in-process transport и не
требует обязательного запуска Redis или Notification / Delivery Service.

---

## 124.2. Durable Web request lifecycle

Длительный agent run не должен быть неявно привязан к lifetime одного
синхронного HTTP-соединения.

Agent cycle может включать несколько LLM-вызовов, tool calls, transport retry,
result/cycle compaction и final audit. Поэтому даже исправный run способен
работать дольше timeout браузера, reverse proxy или HTTP-клиента.

Недопустимо промежуточное состояние:

```text
HTTP client disconnected
→ Agent Runtime продолжил работу
→ final result был создан
→ клиент не может узнать status или получить result
```

Отключение клиента и lifecycle run являются разными событиями:

```text
client connection / delivery subscription
≠ logical request
≠ AgentRun
≠ AgentCycle
```

Минимальные идентификаторы:

```text
request_id       конкретный ingress request и idempotency boundary
run_id           durable исполнение пользовательской задачи
session_id       диалоговая/клиентская сессия
cycle_id         внутренний agent cycle
event_id         progress/trace event
```

Целевой Web-контракт:

```text
POST /web/runs
Idempotency-Key: ...
→ 202 Accepted
→ request_id, run_id, session_id
→ status_url, events_url, result_url

GET /web/runs/{run_id}
→ durable status and safe metadata

GET /web/runs/{run_id}/events
→ SSE/WebSocket replay from event_id / sequence

GET /web/runs/{run_id}/result
→ persisted final result after completion

POST /web/runs/{run_id}/cancel
→ explicit idempotent cancellation request
```

Названия routes могут меняться, но semantic contract обязателен.

`AgentRun` имеет явный lifecycle:

```text
accepted
queued
running
waiting_user
finalizing
succeeded
failed
cancelled
expired
```

Disconnect сам по себе не должен оставлять outcome неопределённым. Политика
может разрешать run продолжиться либо запросить cancellation, но решение
принимает server-side lifecycle policy, сохраняет его в durable state и не
зависит от случайного закрытия socket.

Если run продолжается после disconnect:

- progress events сохраняются и доступны для replay;
- final result сохраняется до перехода в `succeeded`;
- reconnect не создаёт новый run;
- клиент может получить status/result без нового LLM-цикла;
- delivery metrics отдельно фиксируют disconnect и последующий result fetch.

Если run отменяется:

- cancellation является явной и идемпотентной;
- runtime останавливается в safe checkpoint;
- незавершённые LLM/tool tasks получают bounded cancellation;
- уже сохранённые results/artifacts не повреждаются;
- run получает terminal status `cancelled`, а не исчезает.

Timeout budget разделяется по уровням:

```text
HTTP sync wait timeout
LLM/tool per-attempt timeout
retry/backoff budget
queue/start deadline
total AgentRun wall-clock deadline
```

Per-call retry не может бесконечно продлевать общий run. Достижение total
deadline должно давать контролируемый terminal/resumable outcome и сохранять
достаточное состояние для диагностики или продолжения согласно policy.

Session concurrency также задаётся явно:

- один active run на session либо очередь `InputBatch`;
- повтор ingress с тем же `Idempotency-Key` возвращает существующий `run_id`;
- resume `WAITING_USER` продолжает существующий run/cycle;
- новый request не запускает конкурентный cycle поверх незавершённого;
- duplicate/replayed HTTP request не повторяет tool side effects.

Compatibility mode для текущего `/web/message` может работать поверх того же
run contract:

```text
create/get idempotent AgentRun
→ ждать не более sync_wait_timeout
→ если run завершён: вернуть прежний 200 response
→ если run продолжается: вернуть 202 + run_id/status URLs
```

Так short requests сохраняют простой local UX, а long requests не теряют
результат после timeout. Redis не обязателен для local mode: in-process runner
может использовать тот же interface с durable metadata в PostgreSQL/storage.

Acceptance criteria:

1. Disconnect во время LLM retry не теряет final result и не создаёт duplicate
   run.
2. Reconnect/replay возвращает события строго после известного
   `event_id`/sequence.
3. Один `Idempotency-Key` соответствует одному logical run.
4. Terminal `succeeded` публикуется только после durable save final result.
5. Total deadline и explicit cancellation оставляют согласованное состояние.
6. Один session не исполняет два конфликтующих active cycles.
7. Execution success, delivery success и result retrieval наблюдаются
   раздельно.

---

## 125. Redis/arq

Используются для:

- durable background jobs;
- delayed retries;
- distributed locks;
- progress/pub-sub;
- active cycle signals;
- CycleInbox delivery;
- scheduler state;
- rate limiting.

PostgreSQL остаётся долговременным source of truth. Redis не должен быть единственным storage cycle state.

---

## 126. Durable ingress, `CycleInbox` и response outbox

```text
ClientIngressEvent persisted in PostgreSQL
→ draft assembled / payload stored
→ CommittedInputBatch
→ transactional admission
→ Redis event/queue
→ active runtime worker claims inbox item
→ safe checkpoint applies batch
→ application state persisted
```

Требования:

- at-least-once event/queue delivery;
- idempotent processing by event/batch/inbox IDs;
- ordering within session/cycle;
- claim leases and replay after worker restart;
- distributed session lock or transactional fencing token;
- no duplicate addenda, artifact versions or side effects;
- control commands delivered with higher checkpoint priority;
- finalization transaction rechecks input and writes response outbox;
- delivery worker claims `resp_*`/`dlv_*` independently from agent worker;
- execution success and client delivery success remain separate.

Redis является acceleration/event transport, но PostgreSQL остаётся source of
truth для batches, inbox application, terminal result и outbox.

---

## 127. Automatic DAG scheduler

Scheduler:

- calculates ready nodes;
- queues nodes;
- runs safe independent nodes in parallel;
- observes resource limits;
- performs retries;
- blocks dependants on failure;
- updates plan revision;
- persists node results.

LLM отвечает за смысловой plan. Scheduler отвечает за валидное исполнение зафиксированного graph.

Необратимые действия требуют policy/confirmation и не запускаются параллельно автоматически.

---

## 128. Background operations

```text
document extraction
lazy/bulk chunking
embedding generation
hierarchical summarization
reindex
artifact conversion
security scanning
cleanup/retention
large-result preprocessing
```

Каждая job имеет ID, status, retry policy, idempotency key, progress, input/output refs и error.

---

## 129. Hierarchical summarization

Для oversized source:

```text
source
→ chunks
→ per-chunk summaries
→ section summaries
→ final summary
```

Summary хранит provenance и не заменяет original content.

---

## 130. Object storage

Локальная папка может быть заменена на S3-compatible storage/MinIO/cloud blob storage.

`ContentStore` и `ArtifactStore` interfaces из `v0.4` должны позволять замену backend без изменения agent logic.

---

## 131. Idempotency и lifecycle

Ключи:

```text
cycle_id
request_id
run_id
tool_call_id
batch_id
plan_id + revision
artifact_id + version
job_id
idempotency_key
```

Повторная доставка события не должна дважды выполнять необратимое действие, создавать лишнюю file version, завершать node или добавлять batch.

Tool/job lifecycle:

```text
queued
running
retrying
recovered
failed
done
cancelled
```

---

## 132. Observability

Нужны structured logs, trace IDs, cycle/tool/job correlations, metrics, distributed tracing, context/compaction metrics, queue depth и worker health.

User-visible progress остаётся отдельным адаптированным слоем.

Для progress delivery отдельно наблюдаются:

- event published / accepted / rendered / coalesced / deduplicated / failed;
- `request_id`, `run_id`, `event_id`, `cycle_id`, client type и delivery
  target ID;
- event bus lag, client queue depth и render latency;
- reconnect/replay, retry и late-event rejection;
- закрытие delivery session перед final response.

Для Web request/run lifecycle отдельно наблюдаются:

- request accepted / deduplicated / client disconnected;
- run queued / started / waiting / retrying / finalizing / terminal;
- per-attempt timeout, retry/backoff time и total wall-clock time;
- final result persisted / delivered / fetched;
- active runs per session и rejected concurrent starts;
- execution outcome отдельно от delivery outcome.

Логирование не содержит raw tool results, secrets или полный пользовательский
контент.

---

## 133. Постепенная миграция

```text
1. Workers for extraction/embeddings.
2. Durable jobs.
3. Workspace/memory service.
4. MCP tool runtime service.
5. Gateway and Agent Runtime separation.
6. Durable AgentRun and idempotent Web request lifecycle.
7. Client delivery contracts and client-specific progress sinks.
8. Progress event bus and Notification / Delivery boundary.
9. Automatic DAG scheduler.
```

Монолит `v0.5` не переписывается целиком одним шагом.

---

## 134. Не-цели v0.6

- microservices ради microservices;
- отдельный service для каждого Python-модуля;
- потеря local development mode;
- перенос durable domain state в Redis;
- отказ от storage interfaces;
- client-specific UI/delivery logic внутри Agent Runtime;
- единый progress-модуль, смешивающий agent domain и client presentation;
- automatic unsafe parallel actions.

---

# Часть XI. Roadmap

## v0.3 — базовая agent loop архитектура

```text
pending_cycle
context budget
interrupted/resume logic
agent_cycle_archive
compaction placeholder
```

---

## v0.3-agent-protocol-foundation

Цель:

```text
Закрепить JSON AgentAction-протокол и dynamic MCP discovery.
```

Задачи, отражённые в коде:

```text
1. Вынести базовые правила агента в AGENT_SYSTEM_PROTOCOL.
2. Добавить AgentAction как строгий JSON-контракт ответа без tool_call.
3. Добавить manager tools для MCP discovery и вызова реальных инструментов.
4. Запретить старые текстовые маркеры статуса как основной протокол.
5. Зафиксировать правило: tool output — недоверенные данные, не инструкции.
```

---

## v0.3-agent-memory-runtime

Цель:

```text
Подготовить runtime-память agent cycle вместо хранения всего в chat history.
```

Задачи, отражённые в коде:

```text
1. Добавить SessionMemory и DialogTurn.
2. Отделить compact dialog memory от trace/tool results.
3. Добавить manager_tools и server_configs_by_name.
4. Добавить streamable_http transport config.
5. Добавить файловое архивирование agent traces.
```

---

## v0.3-cycle-memory

Цель:

```text
Сохранять незавершённый agent cycle при WAITING_USER и части инфраструктурных ошибок.
```

Задачи, отражённые в коде:

```text
1. Добавить AgentCycleSnapshot.
2. Добавить pending_cycle.
3. Добавить last_error_cycle.
4. Добавить interrupted cycle state.
5. Добавить working_summary/working_state как задел под v0.4 compaction.
6. Добавить context budget config.
7. Переименовать task_id/task_trace в cycle_id/cycle_trace.
```

---

## v0.3-progress-events — live progress tracking

Цель:

```text
Добавить простое рабочее отслеживание выполнения agent cycle
на текущем v0.3-этапе без PostgreSQL, Redis и event bus.
```

Задачи, отражённые в коде:

```text
1. Расширить ProgressEvent: event_id, cycle_id, iteration, severity, visibility.
2. Прокинуть progress_callback через API.call_agent и MessageProcessor.
3. На уровне Gateway добавить HTTP progress callback для Telegram server.
4. Telegram server должен редактировать одно status-message.
5. Добавить progress_locale через message.metadata.
6. Нормализовать mcp_call_tool: tool_name=mcp_call_tool, target_tool_name=реальный инструмент.
7. Добавить throttling/deduplication для Telegram editMessageText.
8. Санитизировать event.data и не класть туда raw tool results/secrets.
9. Сохранять progress_events в pending_cycle и agent_cycle_archive.
10. Оставить LLM-generated progress только как будущий optional layer.
```

---

## v0.3-progress-events refinements

Цель:

```text
Довести progress layer до единого локализованного механизма без Telegram-specific костылей в runtime.
```

Задачи, отражённые в коде:

```text
1. Добавить progress/error события для LLM retry/exhausted: llm_retry, llm_error, infrastructure_error.
2. Добавить error_kind и can_resume.
3. Оставить логи инфраструктурных ошибок техническими.
4. Для Telegram добавить режим TELEGRAM_FINAL_DELIVERY_MODE.
5. Не затирать последний runtime-status финальным "✅ Готово".
6. Классифицировать non-retryable LLM HTTP errors как llm_configuration_error.
7. Вынести PROGRESS_MESSAGES из MCPClient.
8. Добавить локализованные fallback kwargs.
9. Добавить progress_key/progress_arg_map для ManagerToolSpec.
10. Унифицировать эмиссию через _emit_progress_event().
```

---

## v0.3-mcp-server-manager — lifecycle-aware MCP runtime

Цель:

```text
Сделать MCPServerManager настоящим lifecycle coordinator для MCP-серверов и инструментов.
```

Задачи, отражённые в коде:

```text
1. Расширить MCPServerRuntime lifecycle-полями: healthy, reconnecting, last_error, connected_at, generation.
2. Перенести orchestration вызова реальных MCP-tools в MCPServerManager.call_tool().
3. Добавить resolve_tool_binding(), get_runtime(), call_tool_once(), call_tool_with_recovery().
4. Добавить mark_unhealthy(), recover_runtime(), replace_runtime().
5. Поддержать recovery для streamable_http/http и executable/stdio.
6. Разделить transport/lifecycle errors и tool/application errors.
7. Добавить per-transport timeout для одной попытки вызова и timeout для reconnect/restart.
8. Ограничить retry: одна попытка после восстановления runtime.
9. Защитить parallel recovery через per-server lock.
10. Обновлять tool_registry/available_tools после replace_runtime().
11. Классифицировать Session terminated / CancelledError как управляемые lifecycle errors.
12. Не позволять сбою внешнего MCP-сервера ронять Gateway request как HTTP 500.
13. Подготовить структуру к будущему PostgreSQL-хранению MCP servers/tools/tool calls.
```

---

## v0.3-prompt-optimization

Цель:

```text
Убрать surface-specific formatting из system prompt и перенести его в финальную обработку ответа.
```

Задачи, отражённые в коде:

```text
1. Добавить _delivery_constraints(client_type).
2. Не добавлять Telegram/Web formatting rules в _create_system_message().
3. Применять delivery_constraints только к форме финального ответа.
4. Использовать delivery_constraints в final audit / forced final answer.
5. Не давать delivery_constraints влиять на факты, выводы или выбор инструментов.
```

---

## v0.3-final-processing-pipeline

Цель:

```text
Разделить финальную обработку ответа на выбор режима, grounding и форматирование.
```

Задачи, отражённые в коде:

```text
1. Добавить FinalProcessingMode: SKIP, FORMAT_ONLY, GROUNDED, STRICT_GROUNDED.
2. Добавить FinalProcessingDecision.
3. Добавить _select_final_processing_mode().
4. Добавить _build_final_evidence_pack().
5. Добавить _format_final_answer().
6. Добавить _ground_final_answer().
7. Добавить _process_final_answer().
8. Перевести forced final answer на evidence-based сценарий.
9. Добавить trace events final_processing_decision и final_processing_done.
```

---

## v0.3-final-processing-progress

Цель:

```text
Показывать пользователю этап финальной подготовки/проверки ответа.
```

Задачи, отражённые в коде:

```text
1. Добавить ProgressEvent.type = final_processing_started.
2. Добавить user-friendly progress messages для final processing modes.
3. Добавить _final_processing_progress_key().
4. Эмитить final_processing_started после final_processing_decision и до _process_final_answer().
5. Не показывать пользователю внутренние термины evidence_pack / strict_grounded / audit.
```

---

## v0.4 — agent workspace, planning & context management

Цель:

```text
Создать рабочее пространство агента:
полные данные хранятся вне LLM-контекста,
а runtime работает с компактными представлениями,
файлами, InputBatch и необязательным DAG-планом.
```

Пакеты:

```text
v0.4-storage-foundation
v0.4-result-compaction
v0.4-cycle-compaction
v0.4-dag-planning
v0.4-file-artifacts
v0.4-input-runtime
```

Ключевые результаты:

```text
ContentStore + streaming payload IO
ArtifactStore with lineage + immutable versions
filesystem backend and atomic manifests
result/content/artifact refs
relative context budgets
result_handling with runtime override
single-pass result summary
oversized fallback
one CycleWorkingMemory
optional DAG artifact
ClientIngressEvent + durable IngressEventStore
InputBatchDraft + immutable CommittedInputBatch
Telegram album/sealing + standalone attachment draft
Web atomic multipart input
artifact format registry and manager tools
MCP artifact binding and candidate promotion
CycleAdmissionService
CycleInbox<CommittedInputBatch>
SessionControlInbox + safe checkpoints
per-session lock and finalization race guard
ClientResponseOutbox + delivery lifecycle
```

### Учёт токенов и восстановление после переполнения контекста

Runtime разделяет две задачи оценки:

```text
raw-result accounting
    консервативная верхнесмещённая оценка недоверенного результата;
    применяется для inline/store/summary policy и fidelity checks.

main-request accounting
    оценка полного запроса к основной LLM:
    messages + runtime state + tool schemas + protocol overhead.
```

Для main request используется следующий порядок источников:

```text
1. Локальный tokenizer, явно заданный валидным tokenizer_encoding.
2. Локальный tokenizer, автоматически выбранный по полному имени model,
   затем по имени модели без provider-префикса. Этот шаг также выполняется,
   если явно заданный tokenizer_encoding не существует в tiktoken.
3. Model-neutral UTF-8/character heuristic, если tokenizer неизвестен.
4. Фактический prompt_tokens из успешного ответа провайдера как
   calibration snapshot для последующих запросов того же model и набора tools.
```

Валидный явно заданный `tokenizer_encoding` остаётся приоритетным override.
Если автоматический mapping модели указывает на другую кодировку, runtime
сохраняет явную настройку и пишет безопасное предупреждение без содержимого
запроса. Диагностика выбора содержит requested/resolved encoding и источник:
`explicit`, `model_mapping`, `model_mapping_after_explicit_failure` или
`heuristic`.

`prompt_tokens` не переносится между разными моделями и схемами инструментов.
Snapshot привязывается к fingerprint запроса и tool schemas; для изменившегося
low-confidence request независимо от направления изменения используются
одновременно additive- и ratio-calibration от последнего фактического usage,
после чего выбирается более безопасная оценка. Для high-confidence estimator
используется additive calibration. Выбор источника, confidence,
estimate/actual ratio и факт применения snapshot сохраняются в безопасной
диагностике без содержимого сообщений.

До сохранения token usage snapshots в persistent CycleStore требуется ввести
стабильный `estimator_identity`: implementation, encoding name, protocol
overhead и algorithm version. Совместимость snapshot должна проверять эту
identity вместе с model и tool-schema fingerprint.

Для non-OpenAI provider необходим общий `ProviderInputAdapter`, формирующий
одинаковое tokenizable input representation для фактического LLM-вызова и
token accounting. В него входят только prompt-bearing поля (`messages` или
форматированный `prompt`, а также `tools`), но не generation-параметры вроде
`temperature` и `max_tokens`.

Compaction target относится к реально компактируемой части цикла. Системный
prompt, tool schemas, исходный пользовательский запрос, незакрытые tool
последовательности и защищённые последние блоки учитываются как fixed/protected
overhead и не создают недостижимую цель для selector.

Если провайдер всё же возвращает распознаваемую ошибку context overflow,
основной LLM-вызов выполняет ровно один recovery:

```text
provider context overflow
    -> принудительная безопасная cycle compaction
    -> повторная сборка полного main request
    -> один повтор LLM-вызова
```

Recovery не применяется к произвольным `400/413/422`, не делает повторов без
компактизации и не превращает остальные configuration errors в resumable.
Повторный overflow после recovery завершается `CycleContextLimitError` с
сохранением рабочего цикла.

---

## v0.5 — PostgreSQL, lazy indexing и RAG

Цель:

```text
Перенести memory/workspace metadata в PostgreSQL,
добавить pgvector и retrieval по results, files, cycles и plans.
```

Задачи:

```text
PostgreSQL + migrations
Postgres implementations of storage/workspace/input contracts
transactional session admission and finalization
hybrid raw content/object storage
artifact lineage/version/delivery persistence
persistent ingress/draft/batch/inbox/control/outbox state
lazy file extraction and structured representations
pgvector embeddings
keyword/semantic/hybrid retrieval
provenance-aware memory/artifact/plan tools
resume full workspace after restart
```

---

## v0.6 — microservices, Redis и workers

Цель:

```text
Перейти к distributed runtime
с durable queues, workers и DAG scheduling.
```

Задачи:

```text
Redis/arq
durable jobs/retries
distributed CycleInbox
worker extraction/chunking/embeddings
background hierarchical summarization
automatic DAG scheduler
safe parallel nodes
object storage
service boundaries
observability/idempotency
Gateway / Client API and Agent Runtime separation
durable AgentRun lifecycle
idempotent Web ingress with request_id/run_id
bounded synchronous compatibility mode
status/result/cancel endpoints for long runs
separate per-attempt, retry and total run deadlines
durable final result before succeeded status
canonical progress event contract
progress event bus with at-least-once delivery
idempotent client consumption by event_id
Notification / Delivery boundary
common client delivery lifecycle
Telegram/Web/CLI-specific progress sinks
SSE/WebSocket reconnect and event replay
separate execution, delivery and result-retrieval metrics
local callback compatibility mode
```

---

# Главные принципы

1. `messages_for_llm` — рабочий контекст, а не долговременное хранилище.
2. Полные results, files и старые cycle segments сохраняются через storage interfaces.
3. Runtime зависит от `ContentStore` / `ArtifactStore`, а не от файловых путей.
4. Большой result сначала сохраняется и только потом компактизируется.
5. Агент может указать `result_handling`, но runtime имеет последнее слово по безопасности.
6. `prefer_inline` не отключает защиту от переполнения.
7. Размер result оценивается относительно context budget модели.
8. Абсолютные лимиты — только техническая защита процесса.
9. Small result простой задачи может остаться inline.
10. Single-pass summary применяется только к result, помещающемуся в отдельный compact request.
11. Oversized result получает `needs_retrieval=true`, а не ложное полное summary.
12. Canonical original content хранится целиком.
13. Chunks/embeddings — перестраиваемые производные данные.
14. Lazy chunking и semantic RAG относятся к `v0.5`.
15. Cycle compaction работает только с закрытыми атомарными segments.
16. Нельзя разрывать assistant tool calls и corresponding tool messages.
17. В visible context максимум одно актуальное `CycleWorkingMemory`.
18. Старые summary generations не образуют дерево.
19. Исходные cycle events остаются source of truth.
20. Compaction не удаляет segment до успешного replacement.
21. Compaction является trace/progress event.
22. Progress не содержит raw results, secrets и большие payloads.
23. Final grounding использует только фактически доступное evidence.
24. Непрочитанная часть stored content не считается evidence.
25. DAG — отдельный artifact cycle, а не system-prompt text.
26. DAG необязателен для simple tasks.
27. В `v0.4` DAG — карта, а не scheduler.
28. Authoritative current plan читается через точный `PlanStore`, не через RAG.
29. Plan/node IDs принадлежат runtime; LLM использует только `client_key`.
30. `ready` и `stalled` вычисляются и не сохраняются как lifecycle statuses.
31. Каждая plan mutation, кроме create, требует `expected_revision`.
32. При active plan содержательный tool call требует один `in_progress` node.
33. Active plan должен быть completed либо cancelled до final answer.
34. Lifecycle status и `AgentActivity` — разные оси.
35. File представлен `ArtifactRef`, а не arbitrary local path.
36. Edit пользовательского file создаёт новую version.
37. File delivery выполняет adapter layer.
38. Прочитанное file content проходит result-compaction policy.
39. Transport message не равен logical user turn.
40. `InputBatch` объединяет text и attachments.
41. `CycleInbox` принимает sealed `InputBatch`.
42. Initial request и active-cycle addendum используют одну batch model.
43. Addendum вставляется только в safe checkpoint.
44. Полезный tool call не игнорируется ради нового input.
45. Per-session lock защищает active cycle и inbox.
46. `WAITING_USER`/infrastructure interruption сохраняют resumable workspace.
47. v0.4 работает без PostgreSQL, но через PostgreSQL-friendly interfaces.
48. v0.5 добавляет PostgreSQL/pgvector без обязательных microservices.
49. v0.6 вводит Redis/workers/services при реальной необходимости.
50. PostgreSQL — durable source of truth; Redis его не заменяет.
51. Background jobs должны быть idempotent.
52. MCPServerManager остаётся lifecycle coordinator MCP runtime.
53. Agent loop не управляет reconnect/restart transport напрямую.
54. Surface-specific formatting применяется на финальной стадии.
55. Delivery constraints влияют на форму, а не на facts/actions.
56. Новые слои сохраняют local development mode.
57. Длительный AgentRun не зависит от lifetime одного HTTP-соединения.
58. Client disconnect не оставляет run в неопределённом состоянии.
59. Final result сохраняется до terminal status `succeeded`.
60. Повтор Web request с тем же idempotency key не создаёт duplicate run.
61. Per-attempt timeout/retry budget отделён от total run deadline.
62. Execution outcome, delivery outcome и result retrieval наблюдаются
    раздельно.
