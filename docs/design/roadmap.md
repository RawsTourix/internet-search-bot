---
id: design.roadmap
version: cross-version
spec_status: summary
implementation_status: mixed
---
# Часть XIII. Roadmap

> **Роль документа:** хронологическая сводка. Рабочий список именованных
> обновлений и их порядок определяются README соответствующей версии в
> [`versions/`](versions/), а текущая применимая версия — в
> [`current.md`](current.md).

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
ownership/scope-ready metadata without full account authorization
structured task-output refs for future orchestration
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
с durable queues, workers и многоуровневой workflow/task orchestration.
```

Задачи:

```text
Redis/arq
durable jobs/retries
distributed CycleInbox
worker extraction/chunking/embeddings
background hierarchical summarization
durable workflow/job/task domain
optional request decomposition and workflow planner boundary
local task-DAG scheduler
workflow-level scheduler for major dependent tasks
structured task result/artifact handoff
separate task status, agent activity and task type
safe parallel nodes
optional MCP builtin/instance/user/session registry patch
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

## v0.7 — Skills Library (предварительно)

Цель:

```text
Добавить подключаемые декларативные skills,
выбираемые по необходимости для отдельных workflow tasks.
```

Предварительные направления:

```text
skill.md + metadata/frontmatter
SkillRegistry with builtin/instance/user/session scopes
compact index and hybrid retrieval
bounded on-demand loading
skill selection per workflow task
skill-guided local DAG
capability and trust enforcement
builtin domain/system skills
memory skill through typed memory service
trace/progress and regression tests
```

Точный формат и промежуточные пакеты определяются после `v0.6`.

---

## v0.8 — Identity & Multi-user Workspace (предварительно)

Цель:

```text
Добавить accounts, linked identities, conversations
и точное ownership/authorization всех durable resources.
```

Предварительные направления:

```text
email/password account MVP
auth sessions and profile
Telegram identity linking
conversation/chat management
Web and Telegram shared workspace
user-scoped memory/artifacts/MCP/skills/settings
negative authorization tests
local/self-hosted compatibility
security audit and hardening as release gate
```

Точные auth protocols, UI и deployment model пока не утверждены.

---

