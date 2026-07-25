---
id: design.v0.5.index
version: v0.5
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-25
---

# v0.5 — PostgreSQL и RAG

Версия переносит durable metadata в PostgreSQL и добавляет lazy extraction,
chunking, embeddings и provenance-aware retrieval без обязательного перехода
к микросервисам.

## Читать

- [`postgresql-and-rag.md`](postgresql-and-rag.md) — полная спецификация v0.5.

## Обязательный предыдущий контекст

- [`../v0.4/v0.4-storage-foundation.md`](../v0.4/v0.4-storage-foundation.md);
- [`../v0.4/v0.4-file-artifacts.md`](../v0.4/v0.4-file-artifacts.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-input-runtime.md`](../v0.4/v0.4-input-runtime.md).

`v0.5` заменяет filesystem backend реализациями совместимых интерфейсов, но не
меняет базовые domain boundaries v0.4.

Distributed workers и queues относятся к
[`../v0.6/README.md`](../v0.6/README.md).
