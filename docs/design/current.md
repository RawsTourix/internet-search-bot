---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-07-25
---

# Текущий архитектурный baseline

Этот файл определяет, какие версии следует применять при анализе текущего
проекта. Статус конкретного обновления перед release-решением дополнительно
проверяется по коду и тестам.

## Baseline

`v0.3` описана как реализованная основа:

- JSON-протокол `AgentAction`;
- разделение dialog memory, LLM context и cycle trace;
- `pending_cycle` и resumable cycle;
- progress events;
- lifecycle-aware MCP Server Manager;
- final processing pipeline.

Канонический индекс: [`versions/v0.3/README.md`](versions/v0.3/README.md).

## Активное развитие

`v0.4` является принятой целевой архитектурой agent workspace. Исходная
документация содержит признаки частичной реализации отдельных частей, поэтому
для точного ответа «что уже работает» необходимо проверять код и тесты.

Канонический индекс: [`versions/v0.4/README.md`](versions/v0.4/README.md).

## Будущие версии

| Версия | Роль |
|---|---|
| `v0.5` | PostgreSQL, lazy indexing, embeddings и RAG |
| `v0.6` | workers, queues, distributed runtime и workflow orchestration |
| `v0.7` | предварительная Skills Library |
| `v0.8` | предварительная Identity & Multi-user Workspace |

Будущая версия не должна использоваться как описание текущего поведения, если
это явно не указано в соответствующей спецификации.

## Правило для анализа

Для вопроса о текущем поведении:

1. используйте v0.3 как подтверждённый документацией baseline;
2. проверьте затронутый код на наличие реализации v0.4;
3. используйте v0.5–v0.8 только как будущие архитектурные ограничения.
