---
id: design.glossary
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-08-02
---

# Глоссарий

## Agent cycle

Локальный LLM/tool loop одного executor от task input до terminal outcome,
`WAITING_USER` или поддерживаемого interruption. В v0.3–v0.4 cycle также является
основной единицей выполнения запроса; начиная с v0.6 он находится внутри
`TaskRun`.

## Agent run

Durable выполнение пользовательской цели на уровне distributed runtime. Не равно
lifetime HTTP-соединения, conversation или `AgentCycle`.

## Task run

Изолированное durable выполнение одной задачи внутри `AgentRun`. Имеет bounded
context, executor profile, dependencies, budgets и проверяемый output contract.

## Workflow / Workflow revision

Workflow — граф крупных tasks одного `AgentRun`. Revision — committed версия
графа, которую scheduler исполняет до controlled replan или следующей revision.

## Execution mode

Выбранная стратегия организации работы:

```text
DIRECT | SINGLE_TASK | PLANNED_TASK | WORKFLOW
```

Это не `AgentActivity`, application profile или lifecycle status.

## Application profile

Тип приложения и его composition root:

```text
Service Application
Future Local Agent Application
```

Application profile определяет верхнюю security/capability boundary и не
переключается произвольным пользовательским config value внутри AgentRuntime.

## Service Application

Текущий server-side application profile. Принимает запросы через Gateway/client
API, использует server-side stores, MCP services и delivery adapters. Может быть
self-hosted или managed и иметь single-process, multi-process либо distributed
topology.

## Future Local Agent Application

Возможный будущий executable/desktop/CLI/IDE application profile на машине
пользователя. Использует общий AgentRuntime через отдельный composition root и
получает собственную local configuration, host permissions и approval policy.
Не равен локально запущенному self-hosted service.

## Hosting mode

Способ владения и эксплуатации Service Application:

```text
self-hosted
managed
```

Hosting mode не является application profile или runtime topology.

## Self-hosted service

Service Application, развёрнутый пользователем, командой или разработчиком на
собственной машине/сервере. Может работать локально, но не становится Future
Local Agent Application.

## Managed service

Service Application, развёртывание и infrastructure policy которого контролирует
оператор сервиса. Может обслуживать множество пользователей.

## Runtime topology

Физическая раскладка Service Application:

```text
single-process | multi-process | distributed
```

Topology выбирает adapters/process boundaries, но не меняет agent domain
contracts.

## Environment

Операционная среда `development|test|production`. Не выдаёт дополнительных
capabilities и не подменяет application/hosting policy.

## TaskContextManifest

Runtime-owned manifest входов `TaskRun`: goal, constraints, exact refs,
dependencies, allowed tools/skills, budgets, expected output и success criteria.
Он заменяет неограниченное копирование parent message history.

## TaskResult

Structured outcome задачи: identity, compact summary, typed fields, exact refs,
provenance, limitations и verification status.

## User intervention

Durable пользовательский input, поступивший во время active `AgentRun` и
классифицированный относительно текущего workflow: add context, create task,
revise, cancel, replace или defer.

## Execution attempt

Одна попытка выполнить `TaskRun` или отдельную execution operation. Retry создаёт
новую попытку с отдельным identity и fencing generation.

## ExecutionBackend

Port для создания, выполнения, snapshot и teardown изолированного execution
environment. Возможные adapters: local process, Docker, remote runner.

`LocalProcessExecutionBackend` является execution adapter, а не Future Local
Agent Application.

## CommandExecutionPort

Нейтральный application port terminal/process manager tools. Service Application
связывает его с approved sandbox/execution backend. Future Local Agent Application
сможет связать его с host executor под отдельной permission policy.

## Sandbox profile

Versioned и server-approved описание execution environment, capabilities,
resource limits и network policy. LLM выбирает профиль, а не произвольный image.

## Sandbox instance / lease

Sandbox instance — конкретное ephemeral environment. Lease — ограниченное право
определённого worker/runner управлять instance и commit результат текущей
попытки.

Sandbox instance не является постоянным local-agent environment.

## Runner

Узел execution plane, способный запускать поддерживаемые sandbox profiles и
сообщающий control plane capacity, health и active leases.

## Fencing token

Монотонная generation/epoch, запрещающая старой попытке или runner commit после
выдачи более нового lease.

## Control plane

Управляющая часть Service Application: API, auth, scheduler, durable state,
policies, LLM/tool gateways и Sandbox Manager.

## Execution plane

Ограниченная среда выполнения кода, processes и файловых операций. Не является
source of truth для durable application state и не равна Future Local Agent
Application.

## Principal

Аутентифицированный или системный субъект, от имени которого выполняется
операция. Не обязательно равен account или transport identity.

## Account / Identity / AuthSession

Account — пользовательская учётная запись; Identity — связанный способ входа,
например Telegram; AuthSession — отдельная сессия аутентификации.

## Conversation / Workspace

Conversation хранит логическую историю общения. Workspace объединяет durable
resources и настройки области работы. Они не равны runtime session/process.

## Capability

Явно именованное разрешённое действие или класс доступа. Required capability
skill не выдаёт разрешение; effective capabilities вычисляет runtime policy.

## ConfigProvider

Application-level owner чтения и полной валидации operator/root configuration.
Публикует immutable snapshot с revision. Невалидный reload не заменяет последний
рабочий snapshot.

Service и Future Local Agent application profiles могут использовать общий
ConfigProvider contract с разными root snapshot types.

## AgentConfigSnapshot / ConfigRevision

Validated immutable представление Service Application configuration и
идентификатор её версии. Один active `AgentCycle` использует одну revision;
следующая операция может получить более новую.

Канонический service filename после modularization — `agent.config`. Старое имя
`mcp.config` используется только как временный compatibility alias.

Future Local Agent root config пока не имеет утверждённого filename/schema.

## Operator configuration

Deployment-wide configuration Service Application: runtime, infrastructure,
builtin/instance integrations, client adapters, policies и secret references.
Не является хранилищем per-user settings многопользовательского сервиса.

## User configuration

Owner-scoped MCP definitions, credentials, preferences и grants пользователя.
В Service Application загружается через application services/repositories, а не
как редактируемая секция общего `agent.config`.

## Scope

Область видимости registry/resource: `builtin`, `instance`, `user`, `session`.
Scope определяет visibility/precedence, но не заменяет permission или transport
admission. User scope полноценно enforced после v0.8.

## Builtin MCP service

Отдельный MCP-сервис, поставляемый и тестируемый как системная capability и
зарегистрированный со scope `builtin`. `Builtin` не означает in-process,
обязательную доступность или обход authorization policy.

Новые builtin integrations Service Application используют Streamable HTTP.
Существующие builtin stdio/executable integrations являются migration legacy.

## MCP transport

Способ соединения MCP runtime с сервером. Поддерживаемые adapters могут включать
Streamable HTTP и stdio/executable.

Transport не определяет scope, trust или permission.

## Transport admission policy

Policy application/hosting profile, определяющая, какие сочетания scope и
transport можно подключать до process spawn/connect.

Service Application запрещает user/session-provided executable MCP в trusted
control plane. Self-hosted operator может отдельно разрешить instance stdio.
Future Local Agent Application сможет иметь другую local admission policy.

## MCP registry / Registry revision

MCP registry хранит definitions, scopes, visibility и exact server/tool bindings.
Registry revision — immutable идентификатор snapshot, изменяющийся при
add/remove, enable/disable, generation/tool-list или trusted metadata change.

## Tool execution semantics

Trusted metadata вызова: side-effect class, retry policy, timeout, normalized
outcome, presentation и remote-resource behavior. Она принадлежит registry/policy,
а не произвольному tool output.

## Tool outcome `unknown`

Результат, при котором external operation могла завершиться, но runtime не
получил достоверного подтверждения. Mutating operation с `unknown` не повторяется
автоматически.

## Trusted presentation profile

Одобренное со стороны агента описание semantic progress presentation для
конкретного tool binding. Server-supplied текст не становится trusted profile.

## Remote resource handle

Opaque идентификатор stateful ресурса внешнего сервиса. Agent Runtime хранит
server/resource/owner coordinates и cleanup policy, но не внутреннее состояние
ресурса.

## Lifecycle owner

Runtime boundary, к которой привязан remote resource: `tool_call`, `cycle`,
`run`, `task_run`, `session` или explicit owner. Завершение owner запускает
policy-controlled best-effort cleanup request.

## `messages_for_llm`

Текущий видимый контекст модели. Это рабочее представление, а не долговременное
хранилище и не полный trace.

## `cycle_trace`

Полная техническая трассировка agent cycle: ответы LLM, tool calls/results,
ошибки, progress и compaction events.

## `pending_cycle`

Снимок незавершённого agent cycle, используемый для `WAITING_USER` и
поддерживаемых resumable interruptions.

## `ContentStore`

Интерфейс хранения immutable content и больших результатов. Runtime работает с
refs и не зависит от физических файловых путей.

## Artifact

Логический пользовательский или агентный файл с identity, lineage и версиями.
Конкретное состояние представлено `ArtifactVersion`/`ArtifactRef`.

Execution workspace file становится artifact только после declared validation и
import через artifact contracts.

## `InputBatch`

Атомарный логический пользовательский input, объединяющий текст и attachments.
Transport message сам по себе не обязан быть отдельным logical turn.

## `CycleInbox`

Durable очередь sealed `CommittedInputBatch`, ожидающих применения к agent cycle
на безопасной границе.

## Plan / local DAG

Необязательный runtime-owned план одной задачи. В v0.4 DAG является картой
работы, а не автоматическим scheduler.

## Progress event

Структурированное событие о ходе выполнения. Не содержит raw tool result,
секреты или большие payload.

## Exact retrieval и RAG

Exact store используется для authoritative текущего состояния. RAG помогает
находить релевантные данные, но не определяет current revision, lineage head,
lease generation или полный актуальный plan.

## Canonical document

Единственный source of truth для одной темы и одной версии. README, roadmap, ADR
и historical-файлы могут ссылаться на него, но не переопределяют контракт.
