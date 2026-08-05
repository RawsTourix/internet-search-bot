---
id: design.v0.5.index
version: v0.5
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-05
---

# v0.5 — PostgreSQL и RAG

Версия переносит durable metadata/runtime state в PostgreSQL и добавляет lazy
extraction, chunking, embeddings и provenance-aware retrieval без обязательного
перехода к микросервисам.

## Читать

1. [`postgresql-and-rag.md`](postgresql-and-rag.md) — архитектурный overview,
   data model и retrieval contracts.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные
   именованные updates, acceptance и preparation для v0.6.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.5.1-postgresql-foundation` | SQLAlchemy/Alembic и transaction foundation |
| 2 | `v0.5.2-repository-backends` | PostgreSQL implementations v0.4 ports |
| 3 | `v0.5.3-durable-runtime-state` | Durable session/cycle/workspace recovery |
| 4 | `v0.5.4-lazy-content-processing` | Versioned extraction и structured chunks |
| 5 | `v0.5.5-rag-and-memory-tools` | Keyword/semantic/hybrid retrieval |
| 6 | `v0.5.6-migration-and-recovery` | Filesystem import и recovery strategy |
| 7 | `v0.5.7-persistence-stabilization` | Performance, consistency и v0.6 readiness |

При начале реализации крупный update может быть развёрнут из раздела
`implementation-plan.md` в отдельный файл/папку без изменения идентификатора.

## Обязательный предыдущий контекст

- [`../v0.4/v0.4-storage-foundation.md`](../v0.4/v0.4-storage-foundation.md);
- [`../v0.4/v0.4-file-artifacts.md`](../v0.4/v0.4-file-artifacts.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-input-runtime/README.md`](../v0.4/v0.4-input-runtime/README.md);
- [`../v0.4/v0.4-input-runtime/domain-models-and-state-machines.md`](../v0.4/v0.4-input-runtime/domain-models-and-state-machines.md);
- [`../v0.4/v0.4-input-runtime/finalization-and-recovery.md`](../v0.4/v0.4-input-runtime/finalization-and-recovery.md);
- [`../v0.4/v0.4-runtime-modularization/README.md`](../v0.4/v0.4-runtime-modularization/README.md).

`v0.5` заменяет filesystem backend совместимыми implementations и добавляет
retrieval, но не меняет базовые domain boundaries v0.4.

Особенно сохраняются IDs/transitions input runtime:

```text
InputAdmissionRecord
CycleInboxItem
SessionControlCommand
ActiveCycleSnapshot
CycleContextRevision
AgentEmission
CycleFinalizationRecord
```

PostgreSQL может объединить несколько filesystem records в transactional
aggregate/table layout, но не меняет semantic state machines, sequence/watermark
contract и idempotency relations.

Distributed workers и queues относятся к
[`../v0.6/README.md`](../v0.6/README.md). Общий persistence gate находится в
[`../../release-gates.md`](../../release-gates.md).
