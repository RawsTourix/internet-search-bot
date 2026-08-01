---
id: design.roadmap
version: cross-version
spec_status: summary
implementation_status: mixed
last_reviewed: 2026-08-01
---

# Roadmap v0.3 → v0.10

> **Роль документа:** хронологическая сводка. Канонический список именованных
> updates и их порядок определяются README соответствующей версии в
> [`versions/`](versions/), текущий baseline — в [`current.md`](current.md), а
> подробные contracts — в тематических спецификациях и [`contracts/`](contracts/README.md).

## Общая траектория

```text
v0.3  agent loop baseline
  ↓
v0.4  workspace, artifacts, input runtime, modularization и MCP registry foundation
  ↓
v0.5  PostgreSQL, durable state и RAG
  ↓
v0.6  AgentRun/TaskRun, workers, workflow orchestration и distributed registry
  ↓
v0.7  skills и extension platform
  ↓
v0.8  identity, authorization и multi-user workspace
  ↓
v0.9  single-node isolated execution
  ↓
v0.10 distributed execution plane
```

Физическая архитектура развивается постепенно:

```text
large orchestration class
→ modular monolith
→ PostgreSQL-backed modular monolith
→ multi-process workers
→ selective services
→ isolated execution plane
→ distributed runners
```

См. [`architecture-evolution.md`](architecture-evolution.md).

---

## v0.3 — Agent loop baseline

Реализованная основа:

- JSON `AgentAction` protocol;
- dynamic MCP discovery и manager tools;
- dialog memory, LLM context и cycle trace разделены;
- `pending_cycle`, WAITING_USER и resumable interruptions;
- context budget;
- progress events;
- lifecycle-aware MCP Server Manager;
- final processing/grounding/formatting pipeline.

Канонический реестр: [`versions/v0.3/README.md`](versions/v0.3/README.md).

---

## v0.4 — Agent Workspace

Цель:

```text
полные данные вне LLM-контекста
+ compact runtime projections
+ durable files/input/delivery
+ optional local DAG
+ modular tool runtime and local MCP registry foundation
+ revisioned application configuration
```

Именованные updates:

```text
v0.4-storage-foundation
v0.4-result-compaction
v0.4-cycle-compaction
v0.4-dag-planning
v0.4-file-artifacts
v0.4-file-artifacts-advanced
v0.4-batch-workflows
v0.4-input-runtime
v0.4-runtime-modularization
v0.4-mcp-registry-foundation
```

`v0.4-file-artifacts-advanced` завершает semantic input/output, capabilities,
localization, `OutputBatch`, delivery/recovery и READY outbox.

`v0.4-input-runtime` добавляет `CommittedInputBatch`, `CycleInbox`, safe
checkpoints, control inbox и finalization race barrier.

`v0.4-runtime-modularization` после functional v0.4 декомпозирует
`mcp_client.py`, вводит `AgentRuntime`, LLM/tool/event/repository ports,
composition extensions, composition root и `ConfigProvider`. Канонический
configuration filename меняется с `mcp.config` на `agent.config`; validated
immutable snapshots позволяют применять поддерживаемые изменения без
обязательного restart Gateway.

`v0.4-mcp-registry-foundation` добавляет config-backed scopes
`builtin|instance|user|session`, trusted execution/presentation metadata,
side-effect-aware retry и lifecycle ownership opaque remote handles. Новые
builtin MCP integrations используют Streamable HTTP. stdio/executable остаётся
поддерживаемым MCP transport для user servers; legacy являются только
существующие builtin stdio/executable integrations.

Registry update реализует только сторону агента; внутреннее устройство
конкретного MCP-сервиса не входит в документацию агента.

Общий contract:
[`contracts/builtin-mcp-service-contract.md`](contracts/builtin-mcp-service-contract.md).

Канонический реестр: [`versions/v0.4/README.md`](versions/v0.4/README.md).

---

## v0.5 — PostgreSQL и RAG

Цель:

```text
filesystem-friendly contracts v0.4
→ PostgreSQL-backed durable metadata/runtime
→ lazy structured content processing
→ provenance-aware retrieval
```

Порядок:

```text
v0.5.1-postgresql-foundation
v0.5.2-repository-backends
v0.5.3-durable-runtime-state
v0.5.4-lazy-content-processing
v0.5.5-rag-and-memory-tools
v0.5.6-migration-and-recovery
v0.5.7-persistence-stabilization
```

Ключевые результаты:

- SQLAlchemy 2.x async, Alembic, UnitOfWork;
- transactional admission/finalization/outbox;
- PostgreSQL implementations existing stores;
- full restart recovery;
- lazy extraction/chunking;
- pgvector keyword/semantic/hybrid retrieval;
- provenance/evidence;
- filesystem migration и v0.6-ready durable state.

Канонический реестр: [`versions/v0.5/README.md`](versions/v0.5/README.md).

---

## v0.6 — Distributed runtime

Цель:

```text
request → AgentRun → execution mode → TaskRun → AgentCycle
→ structured TaskResult → durable final result/delivery
```

Порядок:

```text
v0.6.1-job-runtime-foundation
v0.6.2-agent-run-lifecycle
v0.6.3-task-runtime
v0.6.4-workflow-orchestration
v0.6.5-interventions-and-cycle-inbox
v0.6.6-event-bus-and-delivery
v0.6.7-background-workers
v0.6.8-object-storage-and-payload-runtime
v0.6.9-distributed-capability-registry
v0.6.10-service-boundary-stabilization
```

Execution modes:

```text
DIRECT | SINGLE_TASK | PLANNED_TASK | WORKFLOW
```

Версия вводит durable jobs/leases, `TaskContextManifest`, workflow revisions,
safe fork/join, user interventions, progress event bus, background workers,
object storage и первые обоснованные process boundaries.

`v0.6.9` не проектирует scopes заново: он переносит registry foundation v0.4 в
PostgreSQL-backed multi-process runtime, публикует worker-visible revisions и
добавляет ownership-aware synchronization/recovery.

ConfigProvider revision contract также расширяется на multi-process propagation,
а не заменяется новым способом загрузки конфигурации.

Канонический реестр: [`versions/v0.6/README.md`](versions/v0.6/README.md).

---

## v0.7 — Skills и extension platform

Порядок:

```text
v0.7.1-skill-package-format
v0.7.2-skill-registry
v0.7.3-skill-discovery
v0.7.4-skill-runtime-integration
v0.7.5-capability-and-trust
v0.7.6-builtin-skills
v0.7.7-extension-platform-stabilization
```

Skills выбираются task-scoped, загружаются bounded и подключаются через
providers/policies/hooks. Required capabilities не являются разрешениями.
Registry использует scopes `builtin`, `instance`, `user`, `session`, введённые
для MCP registry foundation и расширенные до distributed revisions в v0.6.

Канонический реестр: [`versions/v0.7/README.md`](versions/v0.7/README.md).

---

## v0.8 — Identity & Multi-user Workspace

Порядок:

```text
v0.8.1-identity-model
v0.8.2-authentication
v0.8.3-linked-identities
v0.8.4-conversations-and-workspaces
v0.8.5-authorization-and-ownership
v0.8.6-quotas-settings-and-secrets
v0.8.7-security-hardening
```

Account, Identity, AuthSession, Principal, Conversation, Workspace, AgentRun,
TaskRun и AgentCycle остаются разными сущностями. Telegram связывается с account
через explicit linking flow. Every durable resource получает owner/scope,
negative authorization tests и quota enforcement.

Канонический реестр: [`versions/v0.8/README.md`](versions/v0.8/README.md).

---

## v0.9 — Single-node isolated execution

Порядок:

```text
v0.9.1-execution-contracts
v0.9.2-sandbox-profiles
v0.9.3-workspace-materialization
v0.9.4-single-node-runner
v0.9.5-security-and-resource-policy
v0.9.6-lifecycle-and-recovery
v0.9.7-sandbox-hardening
```

Potentially untrusted code/process/file execution переносится в ephemeral
sandbox. AgentRuntime, DB, Redis, auth и provider credentials остаются в trusted
control plane. `ExecutionBackend` скрывает local/container implementation,
workspace materialized по exact refs, outputs commit-ятся до teardown.

Канонический реестр: [`versions/v0.9/README.md`](versions/v0.9/README.md).

---

## v0.10 — Distributed execution plane

Порядок:

```text
v0.10.1-runner-protocol
v0.10.2-placement-and-capacity
v0.10.3-leases-and-fencing
v0.10.4-remote-workspace
v0.10.5-execution-backends
v0.10.6-distributed-security
v0.10.7-observability-and-recovery
v0.10.8-distributed-execution-hardening
```

Single-host Sandbox Manager расширяется до runner fleet. Placement учитывает
profiles, resources, quotas и isolation classes. Leases/fencing исключают stale
double commit. Object storage обеспечивает remote workspace. Docker,
Kubernetes, gVisor и microVM остаются adapters одного execution contract.

Канонический реестр: [`versions/v0.10/README.md`](versions/v0.10/README.md).

---

## Общий ритм версии

Каждая основная версия следует ритму:

```text
functional updates
→ integration/recovery tests
→ architecture stabilization/hardening
→ current/roadmap/docs consistency
→ release gate
```

Общие критерии: [`release-gates.md`](release-gates.md).

Начиная с v0.5, стабильный update ID имеет формат
`v<major>.<minor>.<sequence>-<slug>`. Внутренние шаги не получают обязательную
четырёхуровневую нумерацию; dependencies и параллельность задаются
implementation plan.
