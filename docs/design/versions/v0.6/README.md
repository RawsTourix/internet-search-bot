---
id: design.v0.6.index
version: v0.6
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-25
---

# v0.6 — Distributed runtime

Версия вводит workers, durable queues, workflow orchestration, независимый
AgentRun lifecycle и разделение execution, delivery и result retrieval.

## Читать

- [`distributed-runtime.md`](distributed-runtime.md) — полная спецификация
  сервисов, workers, scheduler, outbox и observability.

## Зависимости

Сначала прочитайте:

- [`../v0.5/README.md`](../v0.5/README.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-file-artifacts-advanced/output-delivery.md`](../v0.4/v0.4-file-artifacts-advanced/output-delivery.md).

Skills Library использует orchestration boundaries v0.6 и описана в
[`../v0.7/README.md`](../v0.7/README.md).
