---
id: design.v0.4.runtime-modularization
version: v0.4
update: v0.4-runtime-modularization
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-07-27
---

# v0.4-runtime-modularization

## Назначение

Обновление выполняется после `v0.4-input-runtime` и завершает v0.4
архитектурным рефакторингом. Его цель — декомпозировать разросшийся
`src/mcp/mcp_client.py` и наследственную production composition без изменения
принятых runtime contracts и пользовательского поведения.

```text
текущий MCPClient orchestration core
→ compatibility facade
→ AgentRuntime + независимые components/ports
```

## Главный результат

- `AgentRuntime` владеет agent loop.
- MCP runtime является одним из tool backends.
- LLM transport, context management, finalization, events и session/cycle state
  имеют отдельные contracts.
- Planning, artifacts и будущие skills подключаются композиционно.
- Concrete adapters создаются в composition root.
- v0.5 может заменить persistence backend без переписывания agent loop.
- v0.6 может запускать тот же runtime в worker process.

## Non-goals

В обновление не входят:

- PostgreSQL и Alembic;
- Redis/arq и distributed workers;
- изменение `AgentAction` protocol;
- новый scheduler;
- полный rewrite текущей логики;
- изменение semantics compaction, planning, artifacts, delivery или
  `WAITING_USER`;
- физическое выделение микросервисов.

## Порядок чтения

1. [`implementation-sequence.md`](implementation-sequence.md) — пошаговый
   strangler-refactor, target boundaries и acceptance criteria.
2. [`../../../dependency-rules.md`](../../../dependency-rules.md) — допустимое
   направление зависимостей.
3. [`../../../release-gates.md`](../../../release-gates.md) — общий gate v0.4.

## Зависимости

- [`../v0.4-input-runtime.md`](../v0.4-input-runtime.md);
- [`../v0.4-file-artifacts-advanced/README.md`](../v0.4-file-artifacts-advanced/README.md);
- [`../v0.4-dag-planning.md`](../v0.4-dag-planning.md);
- [`../v0.4-cycle-compaction.md`](../v0.4-cycle-compaction.md).

## Migration strategy

Используется strangler-подход:

```text
старый public entrypoint
→ делегирует extracted component
→ characterization tests подтверждают parity
→ ownership state переносится из facade
→ compatibility method удаляется только после migration callers
```

Большой одномоментный rewrite `mcp_client.py` запрещён этой спецификацией.

## Release gate

Обновление завершено, когда основной runtime не зависит напрямую от client
adapters и concrete infrastructure, а полный v0.4 regression suite подтверждает
эквивалентность поведения до и после декомпозиции.