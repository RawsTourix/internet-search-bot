---
id: design.v0.8.identity-and-multi-user
version: v0.8
spec_status: provisional
implementation_status: planned
last_reviewed: 2026-08-02
---

# Часть XII. v0.8 — предварительная концепция Identity & Multi-user Workspace

> **Статус раздела:** предварительная архитектурная концепция. Точные auth
> protocols, account schema, token/session strategy, UI и deployment model не
> выбраны и должны проектироваться отдельными пакетами после стабилизации
> предыдущих версий.

Application/hosting profiles определены в
[`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md).

## 143. Главная идея v0.8

`v0.8` превращает текущий single-user/self-hosted Service Application baseline в
систему, где несколько пользователей могут иметь изолированные аккаунты,
conversations, memory, artifacts, skills, MCP configurations, workflows и
settings через разные client surfaces.

```text
Identity + Authorization + Conversations
→ один account в Web и Telegram
→ точное ownership всех durable resources
→ единый multi-client workspace
```

Авторизация является входной частью обновления. Главная архитектурная задача —
не форма логина, а корректная изоляция и владение данными.

Future Local Agent Application остаётся отдельным application profile. Он может
переиспользовать identity/ownership models, но его local single-user UX и
configuration не проектируются этой версией.

---

## 144. Разделение identity, conversations и runtime entities

Термин `session` нельзя использовать для всех уровней одновременно.
Предварительное разделение:

```text
User / Account
  устойчивый владелец данных

Identity
  способ входа или привязанный внешний principal

AuthSession
  факт активного входа/device token lifecycle

Conversation
  отдельный чат/тема

AgentRun / Workflow
  durable исполнение одного пользовательского запроса

TaskRun
  выполнение одной workflow task

AgentCycle
  внутренний LLM/tool cycle конкретного executor
```

```text
User
└── Conversation
    ├── Messages / InputBatches
    └── AgentRun / Workflow
        ├── TaskRun
        │   └── AgentCycle
        └── Final result / deliveries
```

Ранее используемый authenticated principal может обозначать технический
transport/client principal. Он не становится полноценным account автоматически
до явного Identity linking.

Self-hosted single-user Service Application и Future Local Agent Application
используют explicit local/system principal, а не скрытый глобальный `default` без
ownership model.

---

## 145. Accounts и linked identities

Предварительный MVP account layer может включать регистрацию и вход по
email/password, профиль, logout/auth sessions, восстановление доступа,
деактивацию аккаунта и привязку Telegram identity.

Telegram рассматривается как identity/client channel, а не отдельная копия
аккаунта.

```text
user authenticated in Web
→ requests one-time Telegram linking token
→ opens bot/deep link
→ bot confirms Telegram user_id
→ identity attached to existing account
→ Web and Telegram use the same authorized workspace
```

Linking token должен быть короткоживущим, одноразовым и scoped. Bot token,
password hashes, API keys и auth tokens хранятся отдельно от обычных metadata.

---

## 146. Ownership, authorization и scopes

Каждый доступ к durable resource должен проверять не только opaque ID, но и
principal/ownership/scope.

Изолируются conversations/messages, workflows/tasks, contents/results/artifacts,
memory/RAG indexes, user MCP credentials, user/session skills и LLM settings.

Scopes `builtin`, `instance`, `user`, `session` становятся реально enforced:

```text
builtin  → system policy
instance → deployment operator policy
user     → owner_user_id / explicit grants
session  → связанная conversation/run boundary
```

Получение ID объекта не является разрешением на чтение. Exact store/retrieval
query обязана включать authorization predicate. Negative tests должны доказать,
что user A не получает artifact, chunk, result или MCP config user B ни прямым
ID, ни semantic search, ни reply/client binding.

Scope не принимается как доверенное пользовательское заявление. Service API
назначает `user`/`session` scope на основании authenticated principal и не
позволяет обычному пользователю создать `builtin` или `instance` definition.

Role/team/workspace sharing можно добавить позднее; MVP может оставаться strictly
private-per-user.

---

## 147. Conversations и multi-client workspace

Пользователь получает явные chats/conversations: создать, продолжить,
переименовать, архивировать, переключить и просмотреть durable run/artifacts.

Web, Telegram, network CLI и будущий network VS Code client обращаются к одному
Service Application API, а не содержат собственную бизнес-логику агента.

```text
Telegram ─────┐
Web ──────────┤
network CLI ──┼→ Client API / Service Application / Agent Runtime
VS Code client┤
other clients ┘
```

Network client не становится Future Local Agent Application только потому, что
работает на машине пользователя. Локальный executable profile имеет отдельный
composition root и permission model.

Точная Telegram UX-модель требует отдельного проектирования: меню, commands,
reply bindings и выбор conversation не должны нарушать active run или смешивать
темы.

---

## 148. Hosting modes и application profiles

Финальный продукт не обязан сразу становиться публичным SaaS. Service
Application сохраняет два first-class hosting modes:

```text
self-hosted service
  пользователь, команда или разработчик разворачивает собственный instance
  на локальной машине, сервере или в контейнерах

managed service
  deployment контролируется оператором и может обслуживать множество users
```

Self-hosted и managed используют одни identity/domain contracts. Self-hosted
mode может допускать bootstrap local admin или явно упрощённый trusted-local flow,
но ownership/principal не исчезают из модели.

Runtime topology задаётся отдельно:

```text
single-process | multi-process | distributed
```

Локально запущенный self-hosted service остаётся Service Application и не
становится Future Local Agent Application.

Future Local Agent Application — отдельный возможный executable profile поверх
общего AgentRuntime. Его packaging, local config, host permissions, offline mode
и синхронизация с service не входят в `v0.8`.

Пользователь Service Application может выбирать hosted model, собственный API
endpoint или local OpenAI-compatible/Ollama endpoint, когда runtime имеет к нему
разрешённый сетевой доступ. Managed service для пользовательской local LLM
потребует отдельного authenticated connector/node с исходящим соединением;
открытие LLM-порта в интернет не является рекомендуемой архитектурой и не
обязано входить в `v0.8`.

---

## 148.1. Operator и user configuration

Service operator configuration (`agent.config` после modularization) содержит
развёртывание, builtin/instance integrations, infrastructure policies и secret
references.

Per-user settings не редактируют общий service config:

```text
user MCP definitions
user credentials
model/runtime preferences
conversation/workspace settings
retention/privacy preferences
→ owner-aware repositories and application APIs
```

`v0.8` добавляет полноценные ownership, authorization и protected credential
boundaries для этих данных.

Future Local Agent Application получит отдельную local root configuration; её
формат не определяется этой версией.

---

## 149. Security audit как release gate

Глубокий source-aware security assessment полезно проводить после реализации
auth/authorization boundaries, но считать не feature, а этапом приёмки release
candidate.

```text
v0.8 implementation
→ internal authorization tests
→ isolated test deployment
→ source-aware adversarial assessment
→ remediation
→ regression and repeated verification
→ stable release
```

Audit проводится только на принадлежащем разработчику тестовом deployment с
явным scope, тестовыми accounts, backup и logging. Его отчёт не является
гарантией отсутствия других уязвимостей.

Security развивается раньше `v0.8`:

```text
v0.4 → untrusted files/tool outputs, artifact isolation и transport admission
v0.5 → retrieval authorization hooks and provenance
v0.6 → worker/service trust boundaries, queues and secrets isolation
v0.7 → untrusted external skills and capability enforcement
v0.8 → account isolation, auth sessions and linked identities
```

Предварительный MVP `v0.8` не обязан включать billing, public signup,
organization roles, marketplace или production SaaS operations. Открытыми
остаются auth protocol, recovery policy, Telegram linking UX, trusted-local
self-hosted flow и deployment topology.

---
