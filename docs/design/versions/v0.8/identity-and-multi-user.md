---
id: design.v0.8.identity-and-multi-user
version: v0.8
spec_status: provisional
implementation_status: planned
---
# Часть XII. v0.8 — предварительная концепция Identity & Multi-user Workspace

> **Статус раздела:** предварительная архитектурная концепция. Точные auth
> protocols, account schema, token/session strategy, UI и deployment model не
> выбраны и должны проектироваться отдельными пакетами после стабилизации
> предыдущих версий.

## 143. Главная идея v0.8

`v0.8` превращает однопользовательский/local runtime в систему, где несколько
пользователей могут иметь изолированные аккаунты, conversations, memory,
artifacts, skills, MCP configurations, workflows и settings через разные client
surfaces.

```text
Identity + Authorization + Conversations
→ один account в Web и Telegram
→ точное ownership всех durable resources
→ единый multi-client workspace
```

Авторизация является входной частью обновления. Главная архитектурная задача —
не форма логина, а корректная изоляция и владение данными.

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
instance → deployment policy
user     → owner_user_id / explicit grants
session  → связанная conversation/run boundary
```

Получение ID объекта не является разрешением на чтение. Exact store/retrieval
query обязана включать authorization predicate. Negative tests должны доказать,
что user A не получает artifact, chunk, result или MCP config user B ни прямым
ID, ни semantic search, ни reply/client binding.

Role/team/workspace sharing можно добавить позднее; MVP может оставаться strictly
private-per-user.

---

## 147. Conversations и multi-client workspace

Пользователь получает явные chats/conversations: создать, продолжить,
переименовать, архивировать, переключить и просмотреть durable run/artifacts.

Web, Telegram, CLI и будущий VS Code client должны обращаться к одному Agent
Runtime/API, а не содержать собственную бизнес-логику агента.

```text
Telegram ─────┐
Web ──────────┤
CLI ──────────┼→ Client API / Agent Runtime
VS Code ──────┤
other clients ┘
```

Точная Telegram UX-модель требует отдельного проектирования: меню, commands,
reply bindings и выбор conversation не должны нарушать active run или смешивать
темы.

---

## 148. Deployment и LLM provider modes

Финальный продукт не обязан сразу становиться публичным SaaS. Архитектура должна
сохранять несколько режимов:

```text
local
  Agent + PostgreSQL/Redis + Web/CLI + local/cloud LLM

self-hosted
  пользователь или команда разворачивает свой instance

managed
  потенциальный публичный сервис
```

Local/self-hosted mode остаётся first-class: допускается bootstrap local admin
или явно упрощённый trusted-local mode.

Пользователь может выбирать hosted model, собственный API endpoint или local
OpenAI-compatible/Ollama endpoint, когда runtime работает в той же сети/машине.
Hosted runtime для пользовательской local LLM потребует отдельного authenticated
connector/node с исходящим соединением; открытие LLM-порта в интернет не является
рекомендуемой архитектурой и не обязано входить в `v0.8`.

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
v0.4 → untrusted files/tool outputs and artifact isolation
v0.5 → retrieval authorization hooks and provenance
v0.6 → worker/service trust boundaries, queues and secrets isolation
v0.7 → untrusted external skills and capability enforcement
v0.8 → account isolation, auth sessions and linked identities
```

Предварительный MVP `v0.8` не обязан включать billing, public signup,
organization roles, marketplace или production SaaS operations. Открытыми
остаются auth protocol, recovery policy, Telegram linking UX, local trusted mode
и deployment topology.

---

