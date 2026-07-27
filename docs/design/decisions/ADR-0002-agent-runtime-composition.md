---
id: design.adr.0002
status: accepted
date: 2026-07-27
affects:
  - design.dependency-rules
  - design.principles
  - design.v0.4.runtime-modularization
  - design.v0.7.implementation-plan
---

# ADR-0002: AgentRuntime и композиция расширений

## Контекст

Текущая система исторически выросла вокруг `MCPClient`. Artifacts, planning,
finalization и delivery подключались через subclasses/mixins и переопределение
protected hooks. Этот путь позволил быстро расширить v0.4, но с появлением RAG,
skills, authorization и sandbox приведёт к хрупкому MRO, скрытому ownership и
сложному тестированию combinations.

## Рассмотренные варианты

1. Продолжать добавлять production subclasses/mixins.
2. Создать отдельный agent loop для каждой комбинации функций.
3. Выделить `AgentRuntime` и подключать возможности через явные providers,
   policies, registries и lifecycle hooks.

## Решение

Выбран вариант 3.

`AgentRuntime` владеет agent loop и зависит от ports. MCP является tool backend,
а не владельцем agent/application state.

Расширения используют:

```text
ToolProvider
RuntimeProjectionProvider
ActionGuard
EvidenceContributor
LifecycleHook
CapabilityPolicy
```

Порядок extension points explicit/deterministic и покрывается tests.
Compatibility subclasses допускаются только как временная migration boundary и
не получают новые ответственности.

## Последствия

Положительные:

- skills и future features комбинируются без новых agent-loop classes;
- легче изолировать ownership и testing;
- AgentRuntime можно запускать in-process, worker или service;
- LLM/MCP/persistence/execution adapters заменяемы.

Отрицательные:

- требуется спроектировать extension contracts и порядок hooks;
- migration старого класса выполняется несколькими безопасными патчами;
- возможен временный слой adapters/re-exports.

## Миграция канонической спецификации

Решение отражено в `v0.4-runtime-modularization`, `dependency-rules.md`,
`principles.md` и v0.7 skill runtime integration.