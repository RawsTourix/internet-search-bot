---
id: design.glossary
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-07-27
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

Это не `AgentActivity` и не lifecycle status.

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

## Sandbox profile

Versioned и server-approved описание execution environment, capabilities,
resource limits и network policy. LLM выбирает профиль, а не произвольный image.

## Sandbox instance / lease

Sandbox instance — конкретное ephemeral environment. Lease — ограниченное право
определённого worker/runner управлять instance и commit результат текущей
попытки.

## Runner

Узел execution plane, способный запускать поддерживаемые sandbox profiles и
сообщающий control plane capacity, health и active leases.

## Fencing token

Монотонная generation/epoch, запрещающая старой попытке или runner commit после
выдачи более нового lease.

## Control plane

Управляющая часть: API, auth, scheduler, durable state, policies, LLM/tool
gateways и Sandbox Manager.

## Execution plane

Ограниченная среда выполнения кода, processes и файловых операций. Не является
source of truth для durable application state.

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

## Scope

Область видимости registry/resource: `builtin`, `instance`, `user`, `session`.
User scope полноценно enforced после v0.8.

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