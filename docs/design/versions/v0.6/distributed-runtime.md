---
id: design.v0.6.distributed-runtime
version: v0.6
spec_status: draft
implementation_status: planned
---
# Часть X. v0.6 — microservices, workers и distributed runtime

## 123. Главная идея v0.6

`v0.6` разделяет выросшую систему на сервисы и переносит тяжёлые операции в workers.

```text
устойчивый distributed agent runtime
с durable queues, workers и service boundaries
```

Микросервисы нужны, когда появляются concurrent sessions/requests, тяжёлая file
processing, длительный indexing, независимые DAG nodes и resume после restart.
Полноценная account identity и multi-user authorization относятся к `v0.8`.

---

## 124. Возможные сервисы

```text
Gateway / Client API
→ transport authentication / trusted client ingress
→ durable ClientIngressEvent
→ client file providers and response routes
→ Web AgentRun endpoints

Ingress / Session Coordination Service
→ InputBatchDraft assembly
→ batch commit/idempotency
→ session admission and control commands
→ CycleInbox routing

Workflow Orchestration / Scheduler Service
→ optional request decomposition into major workflow tasks
→ workflow dependencies and task lifecycle
→ queue/worker assignment and resource policy
→ structured task result/artifact handoff
→ verification/finalization stages

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

## 127. Workflow orchestration и scheduler

`v0.6` должен различать два уровня планирования. Точная схема остаётся
предварительной и уточняется при проектировании distributed runtime.

```text
Workflow DAG
→ крупные самостоятельные задачи пользовательского запроса
→ dependencies, parallelism и task-to-task outputs

Task DAG
→ локальный план выполнения одной конкретной задачи
→ развитие `v0.4` AgentPlan
```

Один сложный пользовательский запрос не обязан исполняться одним большим
LLM-контекстом. Например:

```text
«Проанализируй архитектуру и составь план миграции»

Workflow task A: проанализировать архитектуру
→ structured ArchitectureReport

Workflow task B: составить план миграции
→ depends_on task A
→ consumes ArchitectureReport
```

### 127.1. Разделение компонентов

Предварительная ответственность:

```text
Request Orchestrator
→ создаёт durable workflow/run boundary

Task Decomposer
→ выделяет самостоятельные задачи и ожидаемые outputs

Workflow Planner
→ связывает задачи dependencies и формирует workflow graph

Scheduler
→ жёстко управляет очередью, readiness, retries, deadlines и workers

Agent Executor
→ получает одну хорошо определённую задачу
→ строит/исполняет её локальный DAG
→ сохраняет structured result
```

Компоненты могут жить в одном service/module на первом этапе, но их логическая
ответственность не должна смешиваться.

### 127.2. Planner и scheduler

```text
Planner / LLM
→ определяет смысл: что требуется сделать и какие dependencies необходимы

Scheduler
→ гарантирует допустимое исполнение уже зафиксированного graph
```

Scheduler не должен самостоятельно придумывать бизнес-смысл задачи из очереди.
Он:

- calculates ready workflow tasks и local plan nodes;
- queues executable units;
- runs safe independent work in parallel;
- observes model/tool/worker/resource limits;
- applies retry/backoff/deadline policy;
- blocks dependants on failure;
- persists lifecycle transitions and outputs;
- handles cancellation and safe resume;
- prevents duplicate side effects through idempotency/fencing.

Необратимые действия требуют policy/confirmation и не запускаются параллельно
автоматически.

### 127.3. Изоляция контекста и task handoff

Каждый Agent Executor получает bounded task contract:

```text
goal
input refs and summaries
constraints
available capabilities
expected output schema
success criteria
```

Он не обязан получать полный контекст producer task. Результат сохраняется как
structured task artifact/result с compact summary, exact refs, provenance и
limitations. Downstream task при необходимости читает original через `v0.5`
retrieval tools.

Так LLM работает над одной ясной ответственностью за раз и не смешивает анализ,
миграционное планирование, изменение файлов и итоговый отчёт в одном
неограниченном контексте.

### 127.4. Task status, activity и type

Нельзя объединять в один enum состояние исполнения, текущую активность модели и
предметный тип задачи.

```text
Task lifecycle status:
pending | queued | running | waiting_user | waiting_dependency |
completed | failed | cancelled

Agent activity:
planning | searching | reading | tool_calling | processing |
writing | verifying | finalizing

Task type:
architecture_analysis | migration_planning | file_modification |
research | documentation | other domain type
```

### 127.5. Verification и finalization

Общая проверка и подготовка пользовательского ответа являются явными stages
workflow lifecycle:

```text
all required tasks completed
→ cross-task verification / consistency check
→ durable finalization
→ final result + selected artifacts
→ client delivery
```

Они могут использовать системные policies или dedicated executor, но не должны
скрыто повторять всю работу и создавать вторую неподконтрольную версию facts.

### 127.6. Ограничение глубины

Для первой реализации достаточно двух уровней:

```text
workflow tasks
└── local task DAG nodes
```

Рекурсивные subworkflows, произвольная вложенность и автоматическое бесконечное
порождение задач не являются обязательной частью `v0.6`.

### 127.7. MCP registry scopes на стыке v0.5/v0.6

Отдельным переходным patch можно унифицировать области видимости MCP-серверов:

```text
builtin
  поставляется с системой и контролируется кодом проекта

instance
  подключён администратором конкретного deployment

user
  принадлежит конкретному account; полноценно enforced после v0.8

session
  временно доступен только одной conversation/session/run boundary
```

Registry хранит metadata, enabled state, owner/scope, capabilities и ссылку на
секретную конфигурацию. Секреты не возвращаются LLM и не помещаются в обычный
metadata JSON.

До `v0.8` `user` scope может существовать как schema-ready placeholder или
локальный principal scope. Нельзя делать вид, что account isolation уже
обеспечена, пока Identity/Authorization layer не реализован.

Этот patch логически связан с distributed registry/runtime, но не обязан входить
в первый минимальный release `v0.6`.

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
4. Optional MCP registry scopes patch and MCP tool runtime service.
5. Gateway and Agent Runtime separation.
6. Durable AgentRun and idempotent Web request lifecycle.
7. Client delivery contracts and client-specific progress sinks.
8. Progress event bus and Notification / Delivery boundary.
9. Durable workflow/job/task domain and structured task outputs.
10. Local task-DAG scheduler.
11. Optional workflow decomposition and workflow-level scheduler.
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
- automatic unsafe parallel actions;
- обязательная декомпозиция каждого простого запроса;
- неограниченные recursive subworkflows;
- преждевременная имитация account-level authorization до `v0.8`.

---

