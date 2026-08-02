---
id: design.overview
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.10

## Назначение

Документ фиксирует развитие памяти, рабочего пространства и runtime ИИ-агента
после перехода на JSON-протокол, динамические MCP-инструменты и разделение
контекста.

Главная цель:

```text
Агент должен выполнять длинные и составные задачи,
не терять рабочее состояние при WAITING_USER или restart,
не засорять LLM-контекст завершёнными tool results,
работать с durable файлами и памятью,
а затем безопасно масштабироваться до workers, skills,
multi-user режима и изолированного execution plane.
```

Текущий продукт развивается как Service Application. Self-hosted и managed
являются hosting modes этого приложения. Переиспользуемый `AgentRuntime` должен
оставаться независимым от server shell, чтобы после стабилизации ядра было
возможно отдельно спроектировать Future Local Agent Application без fork или
rewrite agent loop.

Каноническая модель:
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).

## Этапы развития

- `v0.3` — agent loop baseline, memory/runtime separation, progress, MCP lifecycle
  и final processing;
- `v0.4` — agent workspace, stores/refs, compaction, artifacts, optional DAG,
  semantic input/output, `CycleInbox`, reusable AgentRuntime, Service Application
  composition и MCP registry foundation;
- `v0.5` — PostgreSQL, lazy extraction, pgvector, RAG и durable persistence;
- `v0.6` — `AgentRun`, `TaskRun`, execution modes, Redis/arq, workers, workflow
  scheduler и service boundaries;
- `v0.7` — подключаемые skills и extension/capability platform;
- `v0.8` — accounts, linked identities, conversations, ownership,
  authorization, quotas и multi-user Service Application workspace;
- `v0.9` — single-node sandbox runtime через `ExecutionBackend`;
- `v0.10` — distributed runner fleet, placement, leases, fencing и remote
  workspace.

Future Local Agent Application не имеет назначенного номера версии. Его
packaging, permission model и local configuration проектируются отдельно после
стабилизации AgentRuntime.

## Сквозная runtime-модель

```text
Account / Principal               начиная с v0.8
└── Conversation / Workspace
    └── AgentRun                  начиная с v0.6
        └── TaskRun               начиная с v0.6
            ├── AgentCycle        развитие baseline v0.3–v0.4
            └── ExecutionAttempt  начиная с v0.9
```

Эти сущности не объединяются в универсальную `session`.

Application profile, hosting mode, topology, environment и execution backend
также остаются разными архитектурными осями.

## Основные архитектурные линии

Документация описывает:

- result и cycle compaction;
- durable content/artifact identity и immutable versions;
- atomic `InputBatch`, `CycleInbox` и safe checkpoints;
- exact retrieval и rebuildable RAG indexes;
- local task DAG и workflow DAG;
- structured task handoff вместо передачи полного parent trace;
- reusable AgentRuntime и profile-specific composition roots;
- operator configuration отдельно от per-user settings;
- MCP transport support отдельно от application admission policy;
- composition-based skills и capability enforcement;
- control plane / execution plane separation;
- single-node sandbox и последующий distributed execution backend.

## Статус будущих версий

`v0.5`–`v0.10` являются проектными спецификациями. Их README задают
канонический порядок именованных updates, а `implementation-plan.md` — scope,
зависимости, последовательность и release gate.

Будущая спецификация ограничивает направление развития, но не считается
описанием текущего поведения без подтверждения кодом и тестами.

Общий путь физической декомпозиции находится в
[`architecture-evolution.md`](architecture-evolution.md), а application/hosting
границы — в
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).
