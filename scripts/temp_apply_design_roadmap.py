from __future__ import annotations

from pathlib import Path


PATH = Path("docs/design_document.md")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    original = text

    text = replace_once(
        text,
        "# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.6",
        "# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.8",
        "document title",
    )

    text = replace_once(
        text,
        """```text
Агент должен уметь выполнять длинные задачи,
не терять рабочий контекст при WAITING_USER,
не засорять LLM-контекст завершёнными tool results,
и постепенно перейти к долговременной памяти через PostgreSQL/RAG.
```""",
        """```text
Агент должен уметь выполнять длинные задачи,
не терять рабочий контекст при WAITING_USER,
не засорять LLM-контекст завершёнными tool results,
и постепенно перейти к долговременной памяти, durable orchestration,
подключаемым skills и многопользовательской среде.
```""",
        "main goal",
    )

    text = replace_once(
        text,
        """- v0.5: PostgreSQL, lazy indexing, pgvector и RAG для памяти и workspace;
- v0.6: микросервисную архитектуру, Redis/arq, workers и distributed runtime;
- принципы result/cycle compaction;
- работу с файлами и версиями артефактов;
- `InputBatch` и `CycleInbox`;
- будущие RAG-инструменты и автоматизируемое DAG-исполнение.

---""",
        """- v0.5: PostgreSQL, lazy indexing, pgvector и RAG для памяти и workspace;
- v0.6: микросервисную архитектуру, Redis/arq, workers, workflow orchestration и distributed runtime;
- v0.7: предварительную концепцию подключаемой библиотеки skills;
- v0.8: предварительную концепцию Identity & Multi-user Workspace;
- принципы result/cycle compaction;
- работу с файлами и версиями артефактов;
- `InputBatch` и `CycleInbox`;
- будущие RAG-инструменты, scheduler, skills и multi-user boundaries.

Разделы `v0.7` и `v0.8` фиксируют предварительные архитектурные концепции.
Они не являются готовым техническим заданием: точные схемы данных, интерфейсы,
пакеты и промежуточные версии должны уточняться после стабилизации `v0.5` и
`v0.6`.

---""",
        "document scope",
    )

    text = replace_once(
        text,
        "Именно в `v0.6` plan превращается из карты в исполняемый workflow.",
        """Именно в `v0.6` локальный plan превращается из карты в исполняемый
`task DAG`. Над ним может появиться отдельный workflow-level graph, который
связывает несколько самостоятельных задач и их результаты.""",
        "planning boundary",
    )

    text = replace_once(
        text,
        """Физические таблицы могут объединяться, но domain boundaries, immutable IDs,
ownership, status transitions и idempotency relations должны сохраниться.

---""",
        """Физические таблицы могут объединяться, но domain boundaries, immutable IDs,
ownership, status transitions и idempotency relations должны сохраниться.

### 111.1. Ownership-ready metadata и будущие scopes

`v0.5` ещё не вводит полноценные пользовательские аккаунты и multi-tenant
workspace, однако PostgreSQL-модели не должны навсегда закреплять один глобальный
owner для всех данных.

Для сущностей, где это уместно, заранее предусматриваются nullable или
system-owned поля и relations:

```text
owner_user_id
workspace_id
conversation_id
created_by_principal_id
scope
```

В `v0.5` они могут быть пустыми, ссылаться на локального/system principal либо
определяться текущим client/session context. Полноценная проверка account-level
ownership относится к `v0.8`.

`agent_sessions.external_user_id` на этом этапе остаётся идентификатором
transport/client principal и не должен ошибочно считаться глобальным account ID.
Точная авторизация доступа к artifacts, contents и retrieval выполняется через
текущий runtime/session access set.

Такой задел позволяет позднее добавить области видимости `builtin`, `instance`,
`user` и `session` без миграции от неявного глобального namespace.

---""",
        "v0.5 ownership extension",
    )

    text = replace_once(
        text,
        """Plan revision может храниться snapshot или event log. Plan refs связываются с
exact artifact/result versions.

---""",
        """Plan revision может храниться snapshot или event log. Plan refs связываются с
exact artifact/result versions.

### 114.1. Структурированные результаты для будущей orchestration

`v0.5` не вводит workflow scheduler, но сохранённые результаты должны быть
пригодны для передачи между будущими задачами без копирования полного LLM-контекста.

Концептуальный task output содержит:

```text
result/artifact type
producer cycle/plan node
compact summary
exact content/result/artifact refs
provenance and limitations
created_at / schema version
```

Полный payload остаётся в workspace и доступен через exact read или RAG. В
контекст следующего этапа передаются bounded summary, typed fields и refs. Эта
модель подготавливает `v0.6` к task-to-task handoff, но не добавляет task
scheduler в `v0.5`.

---""",
        "v0.5 structured task outputs",
    )

    text = replace_once(
        text,
        """- полноценный multi-tenant shared workspace;
- обязательный antivirus/macro sandbox;""",
        """- полноценный multi-tenant shared workspace;
- user accounts, account sessions и linked identities;
- обязательный antivirus/macro sandbox;""",
        "v0.5 non-goals",
    )

    text = replace_once(
        text,
        """```text
artifact lineage/current head/version history and delivery state remain exact,
while extracted chunks/embeddings are rebuildable derived data.
```

---""",
        """```text
artifact lineage/current head/version history and delivery state remain exact,
while extracted chunks/embeddings are rebuildable derived data.
```

```text
storage models preserve explicit ownership/scope extension points,
while v0.5 remains usable in single-user local mode without account authorization.
```

```text
structured result/artifact refs can be consumed by a later task through
bounded summary + exact/RAG retrieval without replaying the producer context.
```

---""",
        "v0.5 acceptance additions",
    )

    text = replace_once(
        text,
        "Микросервисы нужны, когда появляются concurrent users, тяжёлая file processing, длительный indexing, независимые DAG nodes и resume после restart.",
        """Микросервисы нужны, когда появляются concurrent sessions/requests, тяжёлая file
processing, длительный indexing, независимые DAG nodes и resume после restart.
Полноценная account identity и multi-user authorization относятся к `v0.8`.""",
        "v0.6 motivation",
    )

    text = replace_once(
        text,
        """Gateway / Client API
→ Telegram/Web/CLI ingress authentication
→ durable ClientIngressEvent""",
        """Gateway / Client API
→ transport authentication / trusted client ingress
→ durable ClientIngressEvent""",
        "gateway auth wording",
    )

    text = replace_once(
        text,
        """Ingress / Session Coordination Service
→ InputBatchDraft assembly
→ batch commit/idempotency
→ session admission and control commands
→ CycleInbox routing

Agent Runtime Service""",
        """Ingress / Session Coordination Service
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

Agent Runtime Service""",
        "orchestration service",
    )

    old_scheduler = """## 127. Automatic DAG scheduler

Scheduler:

- calculates ready nodes;
- queues nodes;
- runs safe independent nodes in parallel;
- observes resource limits;
- performs retries;
- blocks dependants on failure;
- updates plan revision;
- persists node results.

LLM отвечает за смысловой plan. Scheduler отвечает за валидное исполнение зафиксированного graph.

Необратимые действия требуют policy/confirmation и не запускаются параллельно автоматически.

---"""

    new_scheduler = """## 127. Workflow orchestration и scheduler

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

---"""

    text = replace_once(text, old_scheduler, new_scheduler, "v0.6 scheduler")

    text = replace_once(
        text,
        """```text
1. Workers for extraction/embeddings.
2. Durable jobs.
3. Workspace/memory service.
4. MCP tool runtime service.
5. Gateway and Agent Runtime separation.
6. Durable AgentRun and idempotent Web request lifecycle.
7. Client delivery contracts and client-specific progress sinks.
8. Progress event bus and Notification / Delivery boundary.
9. Automatic DAG scheduler.
```""",
        """```text
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
```""",
        "v0.6 migration order",
    )

    text = replace_once(
        text,
        """- единый progress-модуль, смешивающий agent domain и client presentation;
- automatic unsafe parallel actions.""",
        """- единый progress-модуль, смешивающий agent domain и client presentation;
- automatic unsafe parallel actions;
- обязательная декомпозиция каждого простого запроса;
- неограниченные recursive subworkflows;
- преждевременная имитация account-level authorization до `v0.8`.""",
        "v0.6 non-goals",
    )

    future_parts = r'''# Часть XI. v0.7 — предварительная концепция Skills Library

> **Статус раздела:** предварительная архитектурная концепция. Это не готовое ТЗ
> и не утверждённый набор промежуточных релизов. Точный формат skills, registry,
> selection policy и execution contracts следует проектировать после
> стабилизации `v0.6`.

## 135. Главная идея v0.7

`v0.7` добавляет расширяемую библиотеку декларативных навыков, позволяющую
агенту применять специализированные workflows для отдельных классов задач без
разрастания core system prompt и без жёсткого встраивания каждой методики в код.

```text
устойчивый Agent Runtime + RAG + workflow orchestration
→ выбор подходящего skill для конкретной task
→ загрузка bounded instructions
→ task-local DAG и выполнение
```

Core runtime отвечает на вопрос «как безопасно выполнять задачи вообще».
Skill описывает «как качественно выполнять конкретный класс задач».

Простые запросы не должны проходить обязательный skill-selection ceremony.
Если базового agent loop достаточно, задача выполняется без skill.

---

## 136. Skill как декларативный модуль поведения

Skill не сводится к произвольному prompt fragment. Предварительно он может
содержать:

- назначение и область применимости;
- краткое описание для retrieval;
- пошаговый workflow или authoring rules;
- ограничения и safety notes;
- declarative required capabilities/tools;
- expected inputs и outputs;
- критерии проверки результата;
- примеры и дополнительные resources.

Возможная файловая форма:

```text
skills/<skill-slug>/
  skill.md
  examples/
  resources/
```

`skill.md` может использовать YAML frontmatter:

```yaml
---
name: architecture-analysis
version: 1.0.0
description: Анализ архитектуры программного проекта
tags: [architecture, codebase, analysis]
required_tools: [repository_read]
execution_mode: workflow
---
```

Markdown остаётся человекочитаемым source, а metadata позволяет registry
валидировать, индексировать и выбирать skill программно. Точная schema не
фиксируется до отдельного проектирования `v0.7`.

---

## 137. Skill Registry и scopes

Skill storage не должен быть прямой зависимостью agent loop. Нужен
`SkillRegistry`/`SkillService`, скрывающий filesystem, PostgreSQL или другой
backend.

Предварительные scopes унифицируются с MCP registry:

```text
builtin
  поставляется вместе с проектом, versioned и tested

instance
  установлен администратором deployment

user
  принадлежит конкретному account

session
  временно подключён к одной conversation/run boundary
```

Для local development допускается приватная пользовательская папка, добавленная
в `.gitignore`. В self-hosted/multi-user mode metadata и ownership могут храниться
в PostgreSQL, а files/resources — на filesystem/object storage.

Registry должен предоставлять compact metadata, exact version/content hash,
enabled state, source/trust level и capability requirements.

---

## 138. Поиск и загрузка skills

Полное содержимое всех skills нельзя помещать в system prompt. Предварительный
двухэтапный механизм:

```text
1. Compact index
   name, description, tags, scope, version, required capabilities

2. Selected skill load
   полное skill.md + только необходимые resources
```

Candidate retrieval может сочетать exact filters, keyword search,
semantic/hybrid search через инфраструктуру `v0.5` и bounded final selection.
Agent должен иметь возможность выбрать skill, отказаться от всех candidates или
сообщить, что необходимая capability недоступна.

В первой реализации разумно ограничить task одним primary skill и, возможно,
одним совместимым supporting skill. Свободная композиция большого числа
инструкций повышает риск конфликтов и загрязнения контекста.

---

## 139. Связь skills с workflow и DAG

Skill выбирается не обязательно один раз на весь user request. Предпочтительная
граница — отдельная workflow task:

```text
Workflow task
→ skill candidate search
→ select/no-skill decision
→ adapt skill to concrete task contract
→ build local task DAG
→ execute
→ verify expected output
→ persist structured task result
```

Например:

```text
Task A: architecture analysis
→ architecture-analysis skill
→ ArchitectureReport

Task B: migration planning
→ migration-planning skill
→ consumes ArchitectureReport
→ MigrationPlan
```

Так разные skills не конкурируют за управление одной LLM-итерацией, а каждый
executor получает одну ясную ответственность.

До реализации `v0.7` scheduler `v0.6` должен уметь работать без skills, используя
общие task policies. Skills являются надстройкой, а не обязательным условием
существования workflow runtime.

---

## 140. Trust, capabilities и безопасность

External/user skills считаются недоверенными данными. Skill не может:

- отменять system/developer/user rules;
- самостоятельно выдавать доступ к MCP/tools/files/secrets;
- расширять собственный scope;
- скрытно изменять memory или другие skills;
- требовать произвольного code execution только потому, что это написано в Markdown;
- объявлять результат проверенным без фактического evidence.

`required_tools` и другие requirements являются декларацией зависимости, а не
разрешением. Capability/authorization policy принимает runtime.

Skill content, examples и resources проходят те же prompt-injection boundaries,
что files, webpages и tool outputs.

---

## 141. Domain skills, system policies и lifecycle handlers

Не следует называть skill-ом любой механизм агента.

```text
Domain skills
→ специализированные методики: research, architecture analysis,
   migration planning, document preparation

System policies / builtin skills
→ memory selection, evidence rules, safe tool use, final verification

Lifecycle handlers implemented in code
→ retry, timeout, cancellation, queue claim, terminal commit, delivery
```

Критические lifecycle invariants остаются кодом/runtime policy и не передаются
Markdown-инструкциям.

Memory skill может определять, что считать полезной памятью и как её
структурировать, но запись/чтение/удаление выполняют typed memory tools/service с
ownership checks. После `v0.5` основной backend памяти — PostgreSQL/RAG;
`memory.md` может остаться простым local backend, но не единственным
архитектурным источником истины.

---

## 142. Предварительный MVP, не-цели и открытые вопросы

Возможный MVP `v0.7`:

```text
skill.md + metadata validation
builtin/instance/user/session registry contracts
private local skills directory
compact index
keyword/semantic candidate retrieval
select/no-skill decision per workflow task
bounded skill loading
capability checks
trace/progress events
несколько эталонных builtin skills
regression and injection tests
```

Предварительно не входят public marketplace, silent auto-install/update,
embedded code execution, неограниченная композиция skills и сложный dependency
ecosystem.

Открытыми остаются frontmatter schema, ranking/selection policy,
primary/supporting composition, resource packaging/content hashes,
review/signing model и граница между builtin policy и выбираемым skill.

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

# Часть XIII. Roadmap'''

    text = replace_once(text, "# Часть XI. Roadmap", future_parts, "future parts")

    text = replace_once(
        text,
        """PostgreSQL + migrations
Postgres implementations of storage/workspace/input contracts
transactional session admission and finalization
hybrid raw content/object storage
artifact lineage/version/delivery persistence
persistent ingress/draft/batch/inbox/control/outbox state
lazy file extraction and structured representations
pgvector embeddings
keyword/semantic/hybrid retrieval
provenance-aware memory/artifact/plan tools
resume full workspace after restart""",
        """PostgreSQL + migrations
Postgres implementations of storage/workspace/input contracts
transactional session admission and finalization
hybrid raw content/object storage
artifact lineage/version/delivery persistence
persistent ingress/draft/batch/inbox/control/outbox state
ownership/scope-ready metadata without full account authorization
structured task-output refs for future orchestration
lazy file extraction and structured representations
pgvector embeddings
keyword/semantic/hybrid retrieval
provenance-aware memory/artifact/plan tools
resume full workspace after restart""",
        "roadmap v0.5",
    )

    text = replace_once(
        text,
        """Перейти к distributed runtime
с durable queues, workers и DAG scheduling.""",
        """Перейти к distributed runtime
с durable queues, workers и многоуровневой workflow/task orchestration.""",
        "roadmap v0.6 goal",
    )

    text = replace_once(
        text,
        """Redis/arq
durable jobs/retries
distributed CycleInbox
worker extraction/chunking/embeddings
background hierarchical summarization
automatic DAG scheduler
safe parallel nodes
object storage
service boundaries
observability/idempotency
Gateway / Client API and Agent Runtime separation""",
        """Redis/arq
durable jobs/retries
distributed CycleInbox
worker extraction/chunking/embeddings
background hierarchical summarization
durable workflow/job/task domain
optional request decomposition and workflow planner boundary
local task-DAG scheduler
workflow-level scheduler for major dependent tasks
structured task result/artifact handoff
separate task status, agent activity and task type
safe parallel nodes
optional MCP builtin/instance/user/session registry patch
object storage
service boundaries
observability/idempotency
Gateway / Client API and Agent Runtime separation""",
        "roadmap v0.6",
    )

    text = replace_once(
        text,
        """local callback compatibility mode
```

---

# Главные принципы""",
        """local callback compatibility mode
```

---

## v0.7 — Skills Library (предварительно)

Цель:

```text
Добавить подключаемые декларативные skills,
выбираемые по необходимости для отдельных workflow tasks.
```

Предварительные направления:

```text
skill.md + metadata/frontmatter
SkillRegistry with builtin/instance/user/session scopes
compact index and hybrid retrieval
bounded on-demand loading
skill selection per workflow task
skill-guided local DAG
capability and trust enforcement
builtin domain/system skills
memory skill through typed memory service
trace/progress and regression tests
```

Точный формат и промежуточные пакеты определяются после `v0.6`.

---

## v0.8 — Identity & Multi-user Workspace (предварительно)

Цель:

```text
Добавить accounts, linked identities, conversations
и точное ownership/authorization всех durable resources.
```

Предварительные направления:

```text
email/password account MVP
auth sessions and profile
Telegram identity linking
conversation/chat management
Web and Telegram shared workspace
user-scoped memory/artifacts/MCP/skills/settings
negative authorization tests
local/self-hosted compatibility
security audit and hardening as release gate
```

Точные auth protocols, UI и deployment model пока не утверждены.

---

# Главные принципы""",
        "roadmap v0.7/v0.8",
    )

    text = replace_once(
        text,
        """61. Per-attempt timeout/retry budget отделён от total run deadline.
62. Execution outcome, delivery outcome и result retrieval наблюдаются
    раздельно.""",
        """61. Per-attempt timeout/retry budget отделён от total run deadline.
62. Execution outcome, delivery outcome и result retrieval наблюдаются
    раздельно.
63. `v0.6` различает workflow DAG крупных задач и local task DAG одной задачи.
64. Planner/LLM определяет смысл и dependencies; scheduler обеспечивает жёсткое,
    идемпотентное и ресурсно ограниченное исполнение.
65. Agent Executor по возможности получает одну ясно описанную ответственность,
    bounded inputs и проверяемый output contract.
66. Результаты между tasks передаются как structured summaries и exact/RAG refs,
    а не как полный producer context.
67. Task lifecycle status, AgentActivity и domain task type являются разными
    осями состояния.
68. Skills загружаются по необходимости; вся библиотека не помещается в system
    prompt или visible context.
69. Skill декларирует required capabilities, но не выдаёт себе разрешения и не
    отменяет runtime/system policy.
70. MCP servers и skills используют совместимые scopes: `builtin`, `instance`,
    `user`, `session`.
71. `user` scope становится полноценно enforced только после Identity и
    Authorization layer `v0.8`.
72. Account, Identity, AuthSession, Conversation, AgentRun, TaskRun и AgentCycle
    не должны смешиваться в одну сущность `session`.
73. Security audit является release gate/hardening process, а не доказательством
    абсолютной безопасности или обычной product feature.
74. Local и self-hosted deployment остаются first-class даже после появления
    accounts и потенциального managed service.""",
        "principles extension",
    )

    if text == original:
        raise RuntimeError("design document was not changed")

    PATH.write_text(text, encoding="utf-8")
    print(f"Updated {PATH}: {len(original)} -> {len(text)} characters")


if __name__ == "__main__":
    main()
