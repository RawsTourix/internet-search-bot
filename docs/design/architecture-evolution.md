---
id: design.architecture-evolution
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-01
---

# Эволюция архитектуры до v0.10

## Назначение

Документ определяет устойчивый путь развития проекта без преждевременного
перехода к микросервисам и без последующего полного переписывания agent runtime.
Подробные контракты принадлежат version specifications; здесь зафиксированы
границы этапов и критерии выделения новых deployment units.

## Целевая последовательность

```text
монолитный orchestration core
→ модульный монолит с явными ports
→ PostgreSQL-backed modular monolith
→ multi-process workers и durable AgentRun
→ выборочно выделенные сервисы
→ single-node isolated execution plane
→ distributed runner fleet
```

Появление нескольких Docker-контейнеров само по себе не означает
микросервисную архитектуру. PostgreSQL, Redis и worker могут быть отдельными
процессами при сохранении одного модульного приложения и общих application
contracts.

## Этапы

### v0.4 — устойчивый модульный runtime

- Полные данные вынесены из LLM-контекста через refs и stores.
- Ingress, artifacts, planning, compaction и delivery имеют собственные
  ответственности.
- После `v0.4-input-runtime` центральный orchestration core декомпозируется без
  изменения пользовательского поведения.
- `ConfigProvider` становится единственным owner чтения и валидации
  `agent.config`, публикует immutable revisioned snapshots и позволяет применять
  поддерживаемые изменения без обязательного restart Gateway.
- `AgentRuntime` становится владельцем agent loop, а MCP — одним из tool
  backends.
- Local/config-backed MCP registry получает scopes
  `builtin|instance|user|session`, trusted tool metadata и remote-resource
  lifecycle contracts.
- Новые builtin MCP integrations работают через Streamable HTTP и остаются за
  network/service boundary.
- stdio/executable остаётся поддерживаемым MCP transport для user integrations;
  legacy являются только существующие builtin stdio/executable servers.

### v0.5 — durable persistence

- PostgreSQL становится source of truth для metadata и runtime state.
- Filesystem implementations заменяются совместимыми repository/store adapters.
- Транзакции закрывают admission, finalization, lineage head и outbox changes.
- Lazy extraction, pgvector и RAG добавляются поверх exact stores.
- Полная микросервисная декомпозиция не требуется.

### v0.6 — distributed application

- Вводятся `AgentRun`, `TaskRun`, execution modes, durable jobs и workers.
- Redis/arq используются для coordination и acceleration, но не как единственный
  durable store.
- Gateway, agent worker, background workers и delivery могут стать отдельными
  процессами одного repository/deployment.
- Workflow scheduler исполняет committed revisions с leases, idempotency,
  budgets и safe fork/join.
- MCP registry foundation v0.4 становится PostgreSQL-backed, worker-visible и
  ownership-ready; scopes не проектируются заново.
- ConfigProvider snapshot/revision contract расширяется на multi-process
  propagation, а не заменяется новым способом конфигурации.

### v0.7 — extension platform

- Skills подключаются декларативно и task-scoped.
- Runtime extensions используют providers, policies и hooks, а не subclasses
  центрального клиента.
- Capability requirements skill не являются разрешениями.
- SkillRegistry переиспользует scope/revision semantics MCP registry.

### v0.8 — identity и multi-tenancy

- Ownership, principal context и authorization применяются ко всем durable
  resources.
- Account, linked identity, conversation, workspace, run, task и cycle остаются
  разными сущностями.
- Quotas и security audit становятся release gate перед multi-user execution.

### v0.9 — single-node isolated execution

- Control plane отделяется от execution plane.
- `ExecutionBackend` имеет local и container implementations.
- Ephemeral sandbox создаётся для `TaskRun` или bounded execution attempt, а не
  навсегда для пользователя или conversation.
- Workspace materialized из exact refs; output сохраняется до teardown.

### v0.10 — distributed execution plane

- Sandbox Manager управляет fleet runner-узлов.
- Placement использует profiles, capacity, quotas и security classes.
- Leases и fencing tokens исключают commit устаревшей попытки.
- Object storage и immutable refs заменяют предположение об общем локальном
  filesystem.

## Когда выделять отдельный сервис

Подсистема получает самостоятельный network/deployment boundary, когда есть
хотя бы одна подтверждённая причина:

- независимое масштабирование;
- отдельная security boundary;
- другой lifecycle или failure domain;
- тяжёлые или длительные операции;
- самостоятельная команда/темп релизов;
- устойчивый контракт и необходимость независимого deployment.

Новый Python package, repository или таблица сами по себе не являются причиной
создавать микросервис.

Builtin MCP service может быть отдельным deployable component уже при наличии
самостоятельного lifecycle/failure domain. Новые builtin integrations используют
Streamable HTTP; внутренняя реализация сервиса не является частью Agent Runtime и
определяется собственным deployment/repository.

Существующий builtin stdio/executable server может быть migration source для
такого сервиса, но поддержка stdio transport в MCP runtime сохраняется для user
MCP definitions.

Общий integration contract:
[`contracts/builtin-mcp-service-contract.md`](contracts/builtin-mcp-service-contract.md).

## Рекомендуемый порядок физических границ

```text
v0.4–v0.5:
один application process в local mode + PostgreSQL
optional external builtin MCP services через Streamable HTTP
user MCP servers через Streamable HTTP или stdio/executable

v0.6:
Gateway
Agent worker
Document/index worker
Delivery worker

v0.7–v0.8:
те же deployables + extension/auth boundaries внутри модульного приложения

v0.9:
отдельный Sandbox Manager как security boundary

v0.10:
Sandbox Manager control plane + runner agents
```

MCP Tool Runtime, Memory/Workspace Service и Notification/Delivery Service
выделяются независимо только при появлении реальной operational необходимости.

## Неизменяемые ограничения

- PostgreSQL хранит canonical durable state; Redis его не заменяет.
- Agent/runtime domain не зависит от FastAPI, SQLAlchemy, Redis, Docker или
  Kubernetes напрямую.
- Process boundary использует тот же contract, который ранее прошёл проверку в
  in-process implementation либо отдельном integration contract.
- MCP transport lifecycle не является lifecycle remote resource.
- MCP transport type не является scope или trust level.
- Configuration reload не публикует частично validated state.
- Local и self-hosted deployment остаются first-class.
- Execution environment не получает неограниченный доступ к control plane.
- Переход на следующую версию не должен требовать переписывания завершённого
  agent loop из-за отсутствия заранее определённых ports.
