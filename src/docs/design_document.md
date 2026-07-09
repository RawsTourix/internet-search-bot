# Дизайн-документ: архитектура памяти агента v0.3 → v0.6

## 0. Назначение документа

Этот документ фиксирует развитие архитектуры памяти ИИ-агента после перехода на JSON-протокол, динамические MCP-инструменты и разделение контекста.

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
- ближайшую v0.4: обработку больших данных и подготовку storage-архитектуры;
- v0.5: PostgreSQL + RAG только для памяти агента;
- v0.6: возможную backend-перестройку с Redis/arq/workers;
- принципы context compaction;
- принципы large result handling;
- будущие инструменты доступа агента к старой памяти.

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
   - `mcp_transport_call_timeout`;
   - `mcp_reconnect_timeout`;
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

mcp_transport_call_timeout
  timeout одной попытки вызова runtime.

mcp_reconnect_timeout
  timeout восстановления runtime.

mcp_call_retries_after_recovery
  количество повторов после восстановления.
```

Рекомендуемые значения на v0.3-mcp-server-manager:

```text
tool_call_timeout = 240 sec          общий предохранитель
mcp_transport_call_timeout = 30-45 sec
mcp_reconnect_timeout = 15-20 sec
mcp_call_retries_after_recovery = 1
```

Смысл: общий `tool_call_timeout` остаётся safety net, но stale runtime не должен молча висеть до него.

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

# Часть VI. Context budget

## 52. Настройки LLM context budget

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

# Часть VII. v0.4 — large context & storage preparation

## 53. Главная идея v0.4

v0.4 должна решить проблему:

```text
инструменты, браузер, файлы, документы или страницы могут вернуть огромные данные,
которые нельзя напрямую класть в messages_for_llm.
```

Пример проблемы:

```text
tool_result = 2+ млн символов
context_window ~= 256k токенов
```

Такой результат нельзя отправлять в LLM напрямую.

---

## 54. Новый принцип v0.4

Не так:

```text
huge tool_result → messages_for_llm
```

А так:

```text
huge tool_result
→ LargeResultStore
→ result_id
→ preview + metadata в messages_for_llm
→ raw content остаётся во внешнем хранилище
```

Агент видит не весь текст, а ссылку на него:

```json
{
  "type": "large_result_ref",
  "result_id": "res_123",
  "source_type": "tool_result",
  "tool_name": "browser_snapshot",
  "size_chars": 2140000,
  "preview": "Первые 1000-2000 символов...",
  "note": "Полный результат сохранён во внешнем хранилище."
}
```

---

## 55. v0.4 — без PostgreSQL, но с правильными интерфейсами

В v0.4 не нужно сразу подключать PostgreSQL.

Нужно подготовить интерфейсы:

```text
AgentMemoryStore
LargeResultStore
ChunkStore
ArtifactStore
```

Первая реализация может быть файловой:

```text
FileSystemMemoryStore
```

Структура на диске:

```text
storage/
  cycles/
  large_results/
  chunks/
  artifacts/
  indexes/
```

Главное:

```text
MCPClient не должен знать, где физически хранится память:
в файлах, PostgreSQL или другом backend.
Он должен работать через storage interface.
```

---

## 56. LargeResultStore

Нужно ввести слой хранения больших результатов.

Минимальная модель:

```text
result_id
cycle_id
tool_call_id
source_type
source_name
raw_path
content_hash
size_chars
size_tokens_estimate
preview
metadata
created_at
```

`source_type` может быть:

```text
tool_result
browser_snapshot
browser_html
downloaded_file
webpage
pdf_text
document_text
user_file
generated_artifact
```

---

## 57. Политика размеров

Примерная политика:

```text
small:
  до 20-40k символов
  можно положить в messages_for_llm напрямую

medium:
  40k-200k символов
  preview + archive_ref

large:
  200k+ символов
  LargeResultStore + result_id + preview

huge:
  2 млн+ символов
  только store/index/retrieve, raw в контекст не класть
```

Пороговые значения должны быть в конфиге.

Пример:

```json
{
  "memory": {
    "inline_result_max_chars": 40000,
    "preview_result_max_chars": 200000,
    "large_result_preview_chars": 2000,
    "enable_large_result_store": true
  }
}
```

---

## 58. Ingestion вместо поздней очистки

v0.4 должна не только чистить уже переполненный контекст, а заранее маршрутизировать большие данные.

Алгоритм:

```text
tool_result получен
→ estimate size
→ small: inline
→ medium: preview + archive_ref
→ large: save_large_result + result_id
→ huge: save_large_result + chunking + result_id
```

Так агент вообще не получает в `messages_for_llm` 2 млн символов.

---

## 59. Chunking в v0.4

В v0.4 можно сделать basic chunking без embeddings.

Например:

```text
chunk_size_chars = 4000-8000
chunk_overlap_chars = 500-1000
```

Каждый chunk получает:

```text
chunk_id
result_id
index
text
char_start
char_end
metadata
```

Это уже позволит реализовать:

- list chunks;
- get chunk by id;
- simple keyword search;
- preview around match.

Semantic search можно оставить на v0.5.

---

## 60. Context compaction в v0.4

v0.4 должна реализовать хотя бы Level 1 / Level 2 compaction.

### Level 1

Большие tool results заменяются на:

```json
{
  "type": "large_result_ref",
  "result_id": "...",
  "preview": "...",
  "size_chars": 123456,
  "archive_ref": "..."
}
```

### Level 2

Старая середина цикла заменяется на compact segment:

```json
{
  "type": "compacted_cycle_segment",
  "summary": "...",
  "preserved": {
    "original_user_request": "...",
    "tools_used": [],
    "important_results": [],
    "important_decisions": []
  },
  "archived_trace_refs": [],
  "large_result_refs": []
}
```

---

## 61. Что нельзя терять при compaction

Нельзя терять:

- исходный запрос пользователя;
- текущую цель;
- последний вопрос агента пользователю;
- последний ответ пользователя;
- подтверждённые действия;
- отклонённые действия;
- изменённые файлы;
- ошибки, влияющие на продолжение;
- ID больших результатов;
- ссылки на chunks;
- ссылки на архивированные trace events.

---

## 62. v0.4 как подготовка к PostgreSQL

v0.4 должна подготовить структуру, но не подключать БД.

Рекомендуемый модуль:

```text
src/memory/
  __init__.py
  models.py
  stores.py
  file_store.py
  chunking.py
  serializers.py
```

Возможные интерфейсы:

```python
class AgentMemoryStore(Protocol):
    async def save_cycle(...)
    async def update_cycle(...)
    async def save_trace_event(...)
    async def save_large_result(...)
    async def get_large_result(...)
    async def list_result_chunks(...)
```

---

# Часть VIII. v0.5 — PostgreSQL + RAG только для памяти

## 63. Главная идея v0.5

v0.5 подключает PostgreSQL и RAG, но **только для памяти агента/сессии**.

Не нужно сразу перестраивать весь проект под полноценный backend.

Не нужно сразу вводить:

- users/workspaces;
- отдельные API routers;
- очереди;
- Redis;
- arq workers;
- микросервисную архитектуру.

Цель v0.5:

```text
PostgreSQL + pgvector только для agent memory.
```

---

## 64. Зачем PostgreSQL в v0.5

PostgreSQL нужен для:

- долговременных сессий;
- agent cycles;
- сообщений цикла;
- trace events;
- больших результатов;
- chunks;
- artifacts;
- summaries;
- embeddings;
- связей между объектами.

`pgvector` нужен для semantic search.

---

## 65. Минимальные таблицы v0.5

```text
agent_sessions
agent_cycles
cycle_messages
cycle_trace_events
large_results
large_result_chunks
chunk_embeddings
cycle_artifacts
cycle_summaries
```

---

## 66. Таблица `agent_sessions`

```text
id
client_type
external_user_id
created_at
updated_at
metadata_json
```

---

## 67. Таблица `agent_cycles`

```text
id
session_id
status
original_user_request
final_answer
error
error_kind
can_resume
working_summary
working_state
created_at
updated_at
completed_at
metadata_json
```

---

## 68. Таблица `cycle_messages`

```text
id
cycle_id
role
content_json
tool_call_id
message_index
created_at
```

---

## 69. Таблица `cycle_trace_events`

```text
id
cycle_id
event_type
payload_json
created_at
```

Примеры event_type:

```text
cycle_started
llm_response
assistant_tool_calls
tool_call
tool_result_full
large_result_saved
tool_error
context_compaction
critical_error
cycle_completed
```

---

## 70. Таблица `large_results`

```text
id
cycle_id
tool_call_id
source_type
source_name
raw_text_path
content_hash
size_chars
size_tokens_estimate
preview
metadata_json
created_at
```

---

## 71. Таблица `large_result_chunks`

```text
id
result_id
chunk_index
text
char_start
char_end
token_estimate
metadata_json
created_at
```

---

## 72. Таблица `chunk_embeddings`

```text
id
chunk_id
embedding_model
embedding
created_at
```

`embedding` хранится через pgvector.

---

## 73. Таблица `cycle_artifacts`

```text
id
cycle_id
artifact_type
name
uri
mime_type
metadata_json
created_at
```

---

## 74. Таблица `cycle_summaries`

Необязательная на первом этапе.

Может понадобиться для разных summary:

```text
dialog_summary
working_summary
error_summary
embedding_summary
tool_summary
```

---

## 75. RAG-инструменты v0.5

Read-only инструменты для агента:

```text
agent_memory_get_cycle
agent_memory_get_result
agent_memory_get_chunk
agent_memory_list_result_chunks
agent_memory_search_result
agent_memory_search_cycles
agent_memory_get_tool_result
```

---

## 76. `agent_memory_get_cycle`

Получить конкретный agent cycle.

Параметры:

```json
{
  "cycle_id": "string",
  "mode": "summary | messages | trace | full"
}
```

---

## 77. `agent_memory_search_cycles`

Найти релевантные старые циклы.

Параметры:

```json
{
  "query": "string",
  "limit": 5,
  "scope": "summary | full | errors | tools"
}
```

---

## 78. `agent_memory_search_result`

Поиск внутри большого результата.

Параметры:

```json
{
  "result_id": "string",
  "query": "string",
  "limit": 5,
  "search_type": "keyword | semantic"
}
```

---

## 79. Retrieved chunks тоже временные

Если агент достал chunks из RAG, их нельзя навсегда оставлять в `messages_for_llm`.

Правильный цикл:

```text
retrieve chunks
→ LLM использовала их
→ агент сделал extracted notes
→ raw chunks убираются из visible context
→ остаются chunk_ids + extracted_facts
```

Пример compact replacement:

```json
{
  "type": "retrieved_context_summary",
  "result_id": "res_123",
  "used_chunk_ids": ["ch_1", "ch_8"],
  "extracted_facts": ["...", "..."],
  "archive_ref": "..."
}
```

---

# Часть IX. v0.6 — backend architecture / workers

## 80. Главная идея v0.6

v0.6 — потенциальная перестройка архитектуры, если проект вырастет.

Сюда можно отложить:

- Redis;
- arq;
- background workers;
- очереди;
- background indexing;
- background embedding generation;
- background summarization;
- ретраи;
- периодическую очистку;
- полноценные сервисные слои.

---

## 81. Когда нужен Redis/arq

Redis/arq нужен, когда появляются тяжёлые фоновые операции:

```text
1. Индексация огромных PDF/HTML/браузерных snapshot.
2. Разбиение 2+ млн символов на chunks.
3. Генерация embeddings для сотен/тысяч chunks.
4. Background summarization.
5. Reindex старой памяти.
6. Очистка старых данных.
7. Retry при падении embedding API.
```

До этого можно обойтись синхронной/полусинхронной обработкой.

---

## 82. Возможная структура v0.6

```text
src/
  memory/
    models.py
    repositories.py
    services.py
    stores.py
    rag.py
    chunking.py
    embeddings.py

  workers/
    tasks.py
    arq_settings.py

  db/
    session.py
    migrations/

  mcp/
    mcp_client.py

  agent/
    protocol.py
    prompts.py
```

---

# Часть X. Roadmap

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

## v0.4 — storage preparation + large result handling

Цель:

```text
не класть огромные данные в messages_for_llm
```

Задачи:

```text
1. Добавить memory/storage interfaces.
2. Добавить FileSystemMemoryStore.
3. Добавить LargeResultStore.
4. Добавить result_id для huge outputs.
5. Добавить preview + metadata.
6. Добавить basic chunking без embeddings.
7. Добавить large_result_refs в working_state.
8. Реализовать context compaction Level 1/2.
9. Стабилизировать agent_cycle_archive под будущую БД.
```

---

## v0.5 — PostgreSQL + RAG только для agent memory

Цель:

```text
долговременная память агента + semantic retrieval
```

Задачи:

```text
1. Подключить PostgreSQL.
2. Подключить pgvector.
3. Добавить SQLAlchemy/asyncpg.
4. Добавить Alembic migrations.
5. Реализовать repository layer.
6. Реализовать PostgresAgentMemoryStore.
7. Перенести FileSystemMemoryStore на интерфейсную совместимость.
8. Добавить таблицы agent memory.
9. Добавить embeddings для chunks.
10. Добавить read-only memory tools.
```

---

## v0.6 — workers / Redis / архитектурное расширение

Цель:

```text
фоновые задачи и зрелая backend-архитектура
```

Задачи:

```text
1. Redis.
2. arq workers.
3. Background chunking.
4. Background embeddings.
5. Background summarization.
6. Retry logic.
7. Очистка старой памяти.
8. Retention policy.
9. Возможное разделение API/services/repositories.
```

---

# Главные принципы

1. Не делать тяжёлый task manager в v0.3/v0.4.
2. Не пытаться программно угадывать смену темы.
3. `WAITING_USER` сохраняет текущий pending cycle.
4. Завершённые циклы не загрязняют новый LLM-контекст.
5. Огромные данные не кладутся напрямую в `messages_for_llm`.
6. Большие результаты получают `result_id`.
7. Raw content хранится во внешнем storage.
8. LLM видит preview + metadata + retrieval instructions.
9. Context compaction заменяет подробности на summary/state, а не удаляет смысл.
10. v0.4 должна работать без PostgreSQL, но через интерфейсы.
11. v0.5 подключает PostgreSQL/pgvector только для памяти агента.
12. v0.6 — отдельный этап для Redis/arq/workers.
13. Progress events на v0.3-progress-events генерирует Agent Runtime, а не внешние MCP-серверы.
14. Telegram live-progress в MVP делается через callback/editMessageText, а не через PostgreSQL watcher.
15. `mcp_call_tool` в progress events должен хранить и manager tool, и target tool.
16. Progress localization задаётся через `message.metadata.progress_locale`, дефолт — `ru`.
17. Progress event не должен содержать секреты, raw tool results и большие payloads.
18. LLM-generated progress может быть только дополнительным слоем, источник истины — runtime event.
19. Для Telegram notification-center сигнала финальный ответ/ошибка должны отправляться новым сообщением; `editMessageText` остаётся для live-progress.
20. Инфраструктурные ошибки отображаются в трёх слоях: технические logs/trace, короткий progress event с кодом/типом ошибки, финальное notification-сообщение с compact technical summary.
21. Логи нельзя заменять абстрактными человекочитаемыми сообщениями; коды HTTP, классы ошибок, attempts и context должны сохраняться.
22. MCPServerManager должен быть lifecycle coordinator для MCP-серверов, а не тонкой прокладкой к MCPClient.
23. Agent loop не должен напрямую управлять reconnect/restart конкретных MCP runtime.
24. Recovery-сценарий должен быть единым, но правила восстановления — transport-specific.
25. Stale runtime не должен висеть до общего tool_call_timeout; lifecycle-layer должен fail/recover быстрее.
26. Runtime/tool metadata должны проектироваться PostgreSQL-friendly, даже если PostgreSQL появится только в v0.5+.
27. Будущие workers должны опираться на тот же жизненный цикл tool calls: queued/running/retrying/recovered/failed/done.
28. Surface-specific formatting не должно жить в system prompt; для этого используется delivery_constraints в финальной обработке.
29. Delivery constraints влияют только на форму ответа, а не на факты, выводы, выбор инструментов или содержание.
30. Финальная обработка ответа должна разделять форматирование и проверку по собранным данным.
31. Внутренние режимы final processing сохраняются в trace/data, но user-visible progress должен быть простым и понятным.
32. DAG/task planner в будущих версиях должен быть отдельным artifact agent cycle, а не частью system prompt или полного messages_for_llm.

