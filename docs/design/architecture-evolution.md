---
id: design.architecture-evolution
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Эволюция архитектуры до v0.10

## Назначение

Документ определяет устойчивый путь развития проекта без преждевременного
перехода к микросервисам и без последующего полного переписывания agent runtime.
Подробные контракты принадлежат version specifications; здесь зафиксированы
границы этапов и критерии выделения новых deployment units.

Application profiles, hosting modes и transport admission определены в
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).

## Целевая последовательность Service Application

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

Текущая последовательность описывает эволюцию Service Application. Future Local
Agent Application не является дополнительной ступенью этой физической
декомпозиции: он сможет использовать тот же AgentRuntime через отдельный
composition root после стабилизации ядра.

## Application и hosting boundaries

Следующие оси не смешиваются:

```text
application profile
  Service Application | Future Local Agent Application

hosting mode Service Application
  self-hosted | managed

runtime topology Service Application
  single-process | multi-process | distributed
```

Текущая локальная разработка является single-process self-hosted Service
Application. Она не доказывает наличие Future Local Agent Application и не
задаёт его host permission model.

Self-hosted и managed service используют одни domain/application contracts.
Различаться могут operator policy defaults, infrastructure adapters, tenancy и
operational topology.

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
- Создаётся явный Service Application composition root; Future Local Agent
  Application в v0.4 не реализуется.
- Local/config-backed MCP registry получает scopes
  `builtin|instance|user|session`, trusted tool metadata и remote-resource
  lifecycle contracts.
- Новые builtin MCP integrations работают через Streamable HTTP и остаются за
  network/service boundary.
- stdio/executable остаётся поддерживаемым MCP transport runtime, но admission
  определяется application/hosting policy.

### v0.5 — durable persistence

- PostgreSQL становится source of truth для metadata и runtime state.
- Filesystem implementations заменяются совместимыми repository/store adapters.
- Транзакции закрывают admission, finalization, lineage head и outbox changes.
- Lazy extraction, pgvector и RAG добавляются поверх exact stores.
- Полная микросервисная декомпозиция не требуется.
- Single-process self-hosted Service Application остаётся рабочим profile.
- Persistence contracts сохраняют owner-ready границы для будущей per-user
  configuration и multi-user service.

### v0.6 — distributed application

- Вводятся `AgentRun`, `TaskRun`, execution modes, durable jobs и workers.
- Redis/arq используются для coordination и acceleration, но не как единственный
  durable store.
- Gateway, agent worker, background workers и delivery могут стать отдельными
  процессами одного repository/deployment.
- Agent Runtime worker/service оборачивает тот же `AgentRuntime`, а не создаёт
  второй agent loop.
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
- Application profile задаёт capability ceiling; skill не может включить host
  execution или другой запрещённый transport.

### v0.8 — identity и multi-tenancy

- Ownership, principal context и authorization применяются ко всем durable
  resources.
- Account, linked identity, conversation, workspace, run, task и cycle остаются
  разными сущностями.
- Quotas и security audit становятся release gate перед multi-user execution.
- Self-hosted и managed остаются hosting modes Service Application.
- Per-user settings, MCP definitions и credentials отделяются от operator
  `agent.config`.
- Future Local Agent Application может использовать explicit local/system
  principal без имитации ненужного публичного account flow.

### v0.9 — single-node isolated execution

- Control plane отделяется от execution plane.
- `ExecutionBackend` имеет local и container implementations.
- Ephemeral sandbox создаётся для `TaskRun` или bounded execution attempt, а не
  навсегда для пользователя или conversation.
- Workspace materialized из exact refs; output сохраняется до teardown.
- Sandbox является execution plane Service Application и не равен Future Local
  Agent Application.

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

Agent Runtime может стать отдельным worker/service после стабилизации
in-process contract и появления operational необходимости. Это физическое
выделение не превращает его в Future Local Agent Application.

Builtin MCP service может быть отдельным deployable component уже при наличии
самостоятельного lifecycle/failure domain. Новые builtin integrations используют
Streamable HTTP; внутренняя реализация сервиса не является частью Agent Runtime и
определяется собственным deployment/repository.

Существующий builtin stdio/executable server может быть migration source для
такого сервиса. Поддержка stdio transport MCP runtime сохраняется, но admission
определяется [`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md)
и concrete composition policy.

Общий integration contract:
[`contracts/builtin-mcp-service-contract.md`](contracts/builtin-mcp-service-contract.md).

## Рекомендуемый порядок физических границ

```text
v0.4–v0.5:
single-process self-hosted Service Application
optional external builtin MCP services через Streamable HTTP
remote user MCP definitions согласно service admission policy
optional operator-managed instance stdio только по явной self-hosted policy

v0.6:
Gateway
Agent Runtime worker/service
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

Future Local Agent Application проектируется отдельно после стабилизации
AgentRuntime. Его возможное появление не меняет порядок service-side physical
boundaries v0.4–v0.10.

## Неизменяемые ограничения

- PostgreSQL хранит canonical durable state; Redis его не заменяет.
- Agent/runtime domain не зависит от FastAPI, SQLAlchemy, Redis, Docker или
  Kubernetes напрямую.
- Process boundary использует тот же contract, который ранее прошёл проверку в
  in-process implementation либо отдельном integration contract.
- MCP transport lifecycle не является lifecycle remote resource.
- MCP transport type не является scope, trust level или admission decision.
- Configuration reload не публикует частично validated state.
- Self-hosted Service Application остаётся first-class после появления managed
  service и accounts.
- Application profile, hosting mode, topology и execution backend не смешиваются.
- Service configuration не включает Future Local Agent Application обычным
  runtime toggle.
- Execution environment не получает неограниченный доступ к control plane.
- Переход на следующую версию не должен требовать переписывания завершённого
  agent loop из-за отсутствия заранее определённых ports.
