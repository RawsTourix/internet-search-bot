---
id: design.glossary
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-07-25
---

# Глоссарий

## Agent cycle

Один цикл работы агента от пользовательского запроса до терминального
результата. `WAITING_USER` приостанавливает cycle, но не завершает его.

## Agent run

Durable выполнение запроса на уровне будущего distributed runtime. Не равно
lifetime HTTP-соединения и не должно смешиваться с `AgentCycle`.

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

Durable очередь sealed `CommittedInputBatch`, ожидающих применения к agent
cycle на безопасной границе.

## Plan / DAG

Необязательный runtime-owned план сложной задачи. В v0.4 DAG является картой
работы, а не автоматическим scheduler.

## Progress event

Структурированное событие о ходе выполнения. Не содержит raw tool result,
секреты или большие payload.

## Exact retrieval и RAG

Exact store используется для authoritative текущего состояния. RAG помогает
находить релевантные данные, но не определяет current revision, lineage head
или полный актуальный plan.

## Canonical document

Единственный source of truth для одной темы и одной версии. README, roadmap,
ADR и historical-файлы могут ссылаться на него, но не переопределяют контракт.
