---
id: design.v0.6.implementation-plan
version: v0.6
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-01
---

# Пошаговый план v0.6

## Общая цель

Преобразовать PostgreSQL-backed modular runtime в durable multi-process system:

```text
request
→ AgentRun
→ execution mode
→ one or more TaskRun
→ AgentCycle executor
→ structured TaskResult
→ durable final result and delivery
```

`CycleInbox` сохраняет v0.4 admission/safe-checkpoint semantics. Новые inputs во
время active run дополнительно получают intervention semantics.

Local/config-backed MCP scopes и registry contracts уже подготовлены в
`v0.4-mcp-registry-foundation`. v0.6 переносит их в durable multi-process
runtime, а не создаёт вторую несовместимую scope-модель.

## Реестр updates

| Порядок | Update | Главный результат |
|---:|---|---|
| 1 | `v0.6.1-job-runtime-foundation` | Redis/arq, durable job contract, leases и retry |
| 2 | `v0.6.2-agent-run-lifecycle` | Durable request/run API и deadlines |
| 3 | `v0.6.3-task-runtime` | Execution modes, TaskRun и bounded context |
| 4 | `v0.6.4-workflow-orchestration` | Workflow revisions, scheduler, fork/join |
| 5 | `v0.6.5-interventions-and-cycle-inbox` | User interventions поверх safe inbox |
| 6 | `v0.6.6-event-bus-and-delivery` | Durable progress transport и delivery boundary |
| 7 | `v0.6.7-background-workers` | Extraction, embeddings, conversion и cleanup workers |
| 8 | `v0.6.8-object-storage-and-payload-runtime` | Multi-process payload transport и signed access |
| 9 | `v0.6.9-distributed-capability-registry` | Durable registry, worker-visible revisions и ownership-ready scopes |
| 10 | `v0.6.10-service-boundary-stabilization` | Проверка process/service boundaries и v0.7 readiness |

## v0.6.1-job-runtime-foundation

### Scope

- Redis connection/configuration;
- arq queues/workers;
- `Job`, `JobAttempt`, status и result/error envelope;
- stable idempotency key;
- claim/lease/heartbeat;
- per-attempt timeout и retry/backoff;
- total operation deadline;
- cooperative cancellation;
- dead-letter/terminal failure policy;
- queue depth и worker health metrics.

### Инварианты

- PostgreSQL хранит canonical job/run outcome там, где job влияет на durable
  application state;
- Redis loss допускает восстановление queued/running intent из PostgreSQL;
- worker повторно открывает dependencies/UnitOfWork по IDs, а не получает живые
  service/session objects;
- at-least-once delivery компенсируется idempotent handler.

## v0.6.2-agent-run-lifecycle

### Scope

- `request_id` и idempotent Web/Telegram ingress binding;
- durable `AgentRun` identity/status;
- accepted/queued/running/waiting/retrying/finalizing/terminal states;
- explicit status/result/cancel endpoints;
- bounded synchronous compatibility wait;
- client disconnect independence;
- per-attempt, retry и total run deadlines;
- final result persisted before `succeeded`;
- run ownership/scope preparation;
- execution, delivery и retrieval outcomes отдельно.

### Acceptance

- повтор request id не создаёт duplicate run;
- Gateway restart/disconnect не прекращает run;
- worker crash переводит run в recoverable state;
- result можно получить после завершения без повторного agent execution.

## v0.6.3-task-runtime

### Execution modes

```text
DIRECT
SINGLE_TASK
PLANNED_TASK
WORKFLOW
```

`ExecutionModeSelector` получает bounded input envelope и имеет deterministic
rules + optional structured LLM classification. Safe fallback — `SINGLE_TASK`.

### Domain

```text
AgentRun
└── TaskRun
    └── AgentCycle
```

`TaskRun` получает `TaskContextManifest`:

- source batch/request projection;
- goal/constraints/success criteria;
- executor profile;
- exact dependency refs;
- bounded summaries/user addenda;
- allowed tools/skills;
- context/model/tool/token budgets;
- expected typed output.

`TaskContextBuilder` проверяет provenance, ownership-ready scope, dependencies и
OpenAI-compatible message/tool sequence. Parent или sibling trace не наследуется
неявно.

### TaskResult

Содержит identity, compact outcome, typed fields, exact refs, provenance,
limitations и verification status.

## v0.6.4-workflow-orchestration

### Scope

- workflow goal и committed revisions;
- workflow tasks/edges/dependencies;
- runtime-owned task IDs;
- scheduler claims/leases;
- local task DAG внутри `PLANNED_TASK`;
- workflow DAG между `TaskRun`;
- bounded adaptive replan;
- parallel-safety/resource policy;
- fork groups и join/integration tasks;
- structured task handoff;
- supersession/reverification.

LLM/executor не создаёт произвольную копию себя. Он возвращает typed
`needs_replan`/proposal; orchestrator валидирует limits и commit revision.

Минимальные limits:

```text
max tasks per run
max parallel tasks
max task/workflow depth
max replanning rounds
max total LLM/tool calls and tokens
per-task and total deadlines
```

## v0.6.5-interventions-and-cycle-inbox

### Scope

Committed input во время active run становится `UserIntervention` с disposition:

```text
answer_pending
add_context / attach_artifact
create_task
revise_workflow / change_constraints
cancel_or_replace
defer
```

Intervention Router решает relation к текущему run/workflow; Execution Mode
Selector решает способ исполнения возникшей task.

### Инварианты

- immutable context snapshot уже запущенного TaskRun не мутируется посреди LLM
  request/tool block;
- intervention применяется в safe checkpoint;
- finalization повторно проверяет relevant interventions;
- stale result может стать partial/superseded;
- duplicate intervention применяется один раз;
- новый input не создаёт автоматически конфликтующий AgentRun той же session.

## v0.6.6-event-bus-and-delivery

### Scope

- canonical `ProgressEvent` envelope и sequence;
- durable trace/event log;
- Redis Stream/PubSub adapter по выбранной semantics;
- at-least-once publication;
- idempotent consumer по `event_id`;
- replay/reconnect;
- Notification/Delivery boundary;
- Telegram/Web/CLI sinks;
- client-side ordering, buffering, coalescing и late-event rejection;
- response/artifact outbox workers и receipts;
- SSE/WebSocket status timeline.

Runtime создаёт canonical events и не управляет UI throttling/rendering.

## v0.6.7-background-workers

Workers:

- extraction/chunking/embeddings;
- conversion/rendering/preview;
- optional scanning;
- hierarchical summarization;
- delivery;
- retention/garbage collection;
- migration/backfill;
- workflow evaluation при необходимости.

Каждый handler имеет stable input IDs, transaction boundary, retry class,
resource/deadline policy и idempotent durable commit.

## v0.6.8-object-storage-and-payload-runtime

### Scope

- `BlobStorage`/object storage adapter;
- content-addressed or opaque storage keys;
- immutable payload hash/size verification;
- streaming upload/download;
- resumable large upload при необходимости;
- short-lived signed URLs;
- multi-process artifact/content access;
- retention и orphan reconciliation;
- local filesystem adapter сохраняется.

Object storage не заменяет PostgreSQL metadata/ownership/transactions.

## v0.6.9-distributed-capability-registry

Update развивает local/config-backed registry v0.4:

```text
builtin
instance
user
session
```

Scopes, trusted tool descriptors, retry/outcome semantics и opaque remote handle
contract не переопределяются. Добавляются distributed guarantees:

- PostgreSQL-backed canonical server/tool definitions и ownership-ready metadata;
- immutable durable registry snapshot/revision;
- deterministic precedence, совместимый с v0.4;
- worker-visible revision publication и invalidation;
- enable/disable/hot change generation между процессами;
- discovery result хранит binding coordinates/revision;
- call повторно проверяет snapshot freshness на executing worker;
- reconnect/server generation не рассинхронизирует registry replicas;
- active run/task сохраняет применённый registry snapshot либо controlled
  rediscovery policy;
- remote-resource ownership может быть durable там, где ресурс переживает
  process boundary;
- startup/reconciliation восстанавливает unresolved cleanup intent;
- `user` scope хранится ownership-ready, но полноценно enforced только в v0.8;
- scope model совместима с SkillRegistry v0.7.

Redis может ускорять invalidation/event delivery, но не является единственным
source of truth registry state.

### Acceptance

- два agent workers видят одну committed revision;
- stale worker не вызывает binding после disable/rebind;
- restart восстанавливает definitions и unresolved lifecycle metadata;
- hot change не меняет уже committed task snapshot молча;
- local single-process adapter проходит тот же registry contract suite.

## v0.6.10-service-boundary-stabilization

### Минимальные deployables

```text
Gateway / Client API
Agent worker
Document/index worker
Delivery worker
PostgreSQL
Redis
Object storage optional/local-compatible
```

MCP Tool Runtime, Workspace Service и Notification Service выделяются отдельно
только при подтверждённой operational причине.

### Проверки

- process restart/redeploy;
- lease expiry и duplicate attempt;
- queue overload/backpressure;
- DB/Redis partial outage;
- event replay;
- workflow fork/join conflicts;
- intervention/finalization race;
- service contracts/versioning;
- distributed tracing и run/task/cycle/job correlation;
- local in-process compatibility mode.

### Gate v0.7–v0.9

- skills могут прикрепляться к TaskContextManifest;
- capability registry имеет stable distributed scopes/revisions;
- execution operation уже представима через port/job, но full sandbox не входит
  в v0.6;
- Sandbox Manager сможет стать отдельной security boundary в v0.9;
- control plane state не зависит от worker process memory.

## Non-goals v0.6

- повторное проектирование local MCP scope model;
- полноценная authentication/authorization UI;
- untrusted user-code sandbox platform;
- per-user permanent containers;
- Kubernetes как обязательный local/runtime dependency;
- distributed runner fleet;
- автоматическое выделение каждого module в microservice.
