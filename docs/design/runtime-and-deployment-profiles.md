---
id: design.runtime-and-deployment-profiles
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Runtime и deployment profiles

## Назначение

Документ определяет устойчивую границу между переиспользуемым агентным runtime,
текущим серверным приложением и возможным будущим локальным исполняемым агентом.
Он не проектирует готовый desktop/CLI-продукт, permission UI или конкретный
sandbox implementation. Его задача — не допустить, чтобы текущая service-first
архитектура сделала дальнейшее выделение Agent Runtime невозможным либо потребовала
полного переписывания agent loop.

Канонический принцип:

```text
общие Agent Core contracts
→ AgentRuntime
→ отдельные application composition roots
```

## Независимые архитектурные оси

Следующие понятия не объединяются в один универсальный `mode`:

```text
application profile
  какое приложение и composition root запущены

hosting mode
  кто разворачивает и контролирует Service Application

runtime topology
  как приложение разложено по процессам и узлам

environment
  development, test или production policy

execution backend
  где исполняется отдельная terminal/code/file operation
```

Пример текущей локальной разработки:

```text
application profile = service
hosting mode = self-hosted
topology = single-process
environment = development
```

Запуск Service Application на ноутбуке или localhost не превращает его в будущий
Local Agent Application. Аналогично `LocalProcessExecutionBackend` является
adapter отдельной execution operation и не обозначает local-agent runtime.

## Общий Agent Core и AgentRuntime

Переиспользуемое ядро включает либо постепенно получает явные contracts для:

- agent cycle и runtime lifecycle;
- planning и context management;
- memory, contents и artifacts;
- input, `CycleInbox` и finalization;
- `ToolDispatcher`, tool providers и MCP runtime;
- progress, traces и normalized outcomes;
- runtime policies и revision-bound configuration dependencies.

`AgentRuntime` координирует эти компоненты, но не знает, каким application
profile он создан. Он не импортирует Telegram/Web adapters, desktop/IDE UI,
конкретный sandbox, host shell или application configuration file.

Один и тот же runtime contract должен поддерживать:

```text
single-process Service Application
multi-process Agent Runtime worker/service
future Local Agent Application
```

Физическое выделение Agent Runtime в отдельный сервис выполняется только при
наличии operational необходимости и не меняет его application contracts.

## Service Application — текущий основной профиль

Текущий проект развивается прежде всего как server-side Agent Service:

```text
Telegram / Web / network CLI / future network clients
→ Gateway / application API
→ Agent Runtime
→ durable stores, MCP services и delivery adapters
```

Service Application может быть:

```text
self-hosted
  пользователь, команда или разработчик разворачивает собственный instance

managed
  deployment контролируется оператором и обслуживает несколько пользователей
```

Self-hosted и managed являются hosting modes одного Service Application, а не
разными реализациями agent loop.

Service Application сохраняет следующие security invariants:

- пользовательский input не становится командой запуска процесса на host control
  plane;
- обычный пользователь не может назначить своему MCP definition scope
  `builtin` или `instance`;
- пользовательский executable/stdio MCP не запускается внутри trusted server
  process;
- доступ к server filesystem происходит только через application contracts,
  artifacts, approved execution backends и authorization policy;
- network clients не содержат отдельную копию agent business logic.

### Service topology

Топология развивается независимо от hosting mode:

```text
single-process
→ multi-process
→ distributed
```

В single-process self-hosted development concrete ports могут быть связаны
in-process. В v0.6 те же contracts могут связывать Gateway, workers, Agent Runtime,
delivery и другие deployables через durable queues и service boundaries.

## Future Local Agent Application

Local Agent Application — возможный будущий самостоятельный executable profile,
работающий на машине пользователя. Он может быть представлен CLI, desktop
application, IDE extension с local daemon либо другой нативной оболочкой.

Предварительно этот профиль сможет:

- создавать `AgentRuntime` in-process;
- использовать локальные или remote LLM adapters;
- работать с локальным workspace через явные grants;
- запускать stdio/executable MCP на машине пользователя;
- предоставлять terminal/process capabilities через host-specific policy;
- при полностью локальных dependencies работать без постоянного подключения к
  интернет-сервису.

Точные UI, packaging, language, permission manifest, sandbox technology,
credential storage и synchronization с Service Application не утверждены.
Архитектура должна не блокировать профиль, но текущие версии не обязаны его
реализовывать.

Local Agent Application имеет отдельный composition root и отдельный security
ceiling. Он не включается переключением обычного поля в service configuration.

## Application selection и composition roots

Security-critical application profile определяется entrypoint и composition
root:

```text
service entrypoint
→ build_service_application(...)
→ ServiceApplication

future local-agent entrypoint
→ build_local_agent_application(...)
→ LocalAgentApplication
```

Допустим общий launcher, выбирающий заранее определённый entrypoint, но выбранное
значение не передаётся глубоко в `AgentRuntime` как условие вида
`if mode == local: allow_host_shell`.

Composition root:

- выбирает configuration root model;
- создаёт concrete ports/adapters;
- задаёт transport admission и security ceiling;
- связывает terminal tools с sandbox либо host executor;
- определяет persistence, identity и delivery adapters;
- публикует только допустимые capabilities.

## Configuration ownership

Общие config submodels могут переиспользоваться, но root configuration и её
владелец различаются.

### Service operator configuration

```text
agent.config
→ ConfigProvider
→ immutable ServiceConfigSnapshot / AgentConfigSnapshot revision
```

Operator configuration содержит deployment-wide настройки:

- LLM providers и runtime defaults;
- storage, ingress, artifacts, planning и delivery;
- Gateway, Telegram/Web и worker settings;
- builtin и instance MCP definitions;
- infrastructure policies и secret references;
- hosting mode, environment и topology metadata.

`hosting_mode`, `environment` и `topology` не переключают Service Application в
Local Agent Application.

### Per-user service configuration

Настройки множества пользователей не хранятся как редактируемые секции общего
`agent.config`. User MCP definitions, settings, credentials и ownership проходят
через application services и owner-aware repositories. Конкретный durable backend
развивается в v0.5–v0.8.

### Future local-agent configuration

Local Agent Application получает отдельную root schema и собственные настройки:
local workspace grants, terminal approval policy, local credentials, UI и local
MCP definitions. Он может переиспользовать `LLMConfig`, runtime, memory, artifact
и MCP models, не требуя полного совпадения root schema с Service Application.

Общий `ConfigProvider` contract допускает разные validated snapshot types; один
active `AgentCycle` по-прежнему фиксирует одну configuration revision.

## MCP transport admission

Поддержка transport adapter MCP runtime и разрешение transport в application
profile являются разными решениями.

MCP runtime может поддерживать:

```text
Streamable HTTP
stdio/executable
```

### Managed Service Application

```text
builtin  → Streamable HTTP
instance → Streamable HTTP
user     → Streamable HTTP
session  → Streamable HTTP
```

### Self-hosted Service Application

```text
builtin  → Streamable HTTP; старые builtin stdio являются migration legacy
instance → Streamable HTTP; operator policy может явно разрешить stdio
user     → Streamable HTTP
session  → Streamable HTTP
```

Operator-managed instance stdio означает доверенное решение владельца deployment.
Оно не разрешает обычному service user передавать произвольную executable command.

### Future Local Agent Application

Streamable HTTP и stdio/executable могут быть разрешены для локальных definitions
согласно local permission, approval и trust policy.

Scope, transport, trust и permission остаются разными осями. Поддерживаемый
transport не получает автоматического admission во всех profiles.

## Terminal tools и execution boundary

Terminal является важной общей capability, но manager tool работает через
нейтральный execution port, а не вызывает host shell напрямую:

```text
terminal manager tools
→ CommandExecutionPort
```

В Service Application:

```text
CommandExecutionPort
→ SandboxCommandExecutor
→ ExecutionBackend
→ ephemeral sandbox
```

Если approved sandbox backend недоступен, Service Application не выполняет
команду на host как fallback.

В Future Local Agent Application:

```text
CommandExecutionPort
→ HostCommandExecutor
→ permission / approval / workspace policy
→ host machine
```

Local host execution остаётся ответственностью пользователя, но продукт должен
иметь собственные ограничения: grants каталогов, dangerous-command approval,
timeouts, output bounds, environment filtering и audit.

`LocalProcessExecutionBackend` v0.9 остаётся execution adapter для development или
bounded attempts и не заменяет Local Agent Application.

## Artifacts и terminal outputs

Terminal или sandbox может создать файл внутри execution workspace, но это ещё не
пользовательский durable artifact и не delivery result.

Канонический путь Service Application:

```text
execution workspace file
→ declared-output validation
→ import immutable content
→ ArtifactRef / candidate or version
→ explicit delivery selection
→ OutputBatch
→ client delivery adapter
```

Terminal не получает Telegram token, direct durable storage credentials или право
самостоятельно объявлять произвольный host path пользовательским результатом.

Local Agent Application может дополнительно иметь export/import tools для
разрешённых локальных путей, но они также применяют permission и artifact
contracts.

## Identity, tenancy и scopes

Service Application развивается от single-user/self-hosted baseline к полноценной
multi-user authorization модели v0.8. Self-hosted deployment не отменяет явного
principal и ownership; managed hosting не меняет domain identity contracts.

Future Local Agent Application может использовать local/system principal и
single-user defaults, сохраняя совместимые ownership fields. Это не требует
имитировать публичную account registration там, где она не нужна.

Scope `builtin|instance|user|session` определяет registry visibility и precedence,
но application profile и authorization policy определяют, кто может создать
definition и какие transports/capabilities допустимы.

## Связь с execution plane v0.9–v0.10

Sandbox execution plane служит Service Application и потенциально другим
profiles как реализация bounded execution requests. Он не является постоянным
пользовательским local environment.

```text
Local Agent Application
≠ LocalProcessExecutionBackend
≠ ephemeral SandboxInstance
```

Будущий Local Agent может переиспользовать общие execution contracts, но его host
permissions, lifecycle и product UX проектируются отдельно.

## Эволюция по версиям

### v0.4

- выделить переиспользуемый `AgentRuntime` и ports;
- реализовать явный Service Application composition root;
- ввести `ConfigProvider` и revision-bound snapshots;
- добавить MCP registry и profile-aware transport admission contracts;
- не реализовывать Local Agent Application.

### v0.5

- заменить filesystem adapters PostgreSQL-backed implementations;
- сохранить single-process self-hosted service mode;
- подготовить owner-aware repositories для user configuration.

### v0.6

- позволить тому же `AgentRuntime` работать в worker/service process;
- разделить Gateway, runtime, delivery и background processing по operational
  необходимости;
- не называть in-process development mode Local Agent.

### v0.7–v0.8

- применить scope/capability contracts к skills;
- добавить identity, ownership, per-user settings, credentials и authorization;
- сохранить self-hosted service и local/system principal paths.

### v0.9–v0.10

- добавить isolated execution plane и distributed runners для Service Application;
- не считать sandbox реализацией Local Agent Application.

### После стабилизации Agent Core

- отдельно спроектировать Local Agent Application, packaging, permissions,
  configuration и UI;
- переиспользовать AgentRuntime и общие contracts вместо fork/rewrite ядра.

## Неутверждённые детали

Пока намеренно не фиксируются:

- имя и формат local-agent config файла;
- конкретный executable/desktop/IDE packaging;
- язык локального launcher;
- точная host permission schema;
- необходимость постоянного local daemon;
- синхронизация local и service conversations;
- способ подписи или распространения local plugins;
- конкретный sandbox/OS isolation backend;
- точный набор встроенных local terminal tools.

Эти решения принимаются отдельными спецификациями после стабилизации общего
AgentRuntime и не изменяют принятый service-first курс текущих версий.
