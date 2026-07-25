---
id: design.v0.4.semantic-interaction-implementation
version: v0.4
spec_status: accepted
implementation_status: partial
---
# v0.4 — Реализация и архитектурные границы semantic interaction

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-21. Порядок реализации

```text
1. Capability registry models and validation
2. Common localization contracts/catalogs ru/en
3. ResponseAnchor and structured presentation acknowledgements
4. InputBatch presentation coordinator
5. Input artifact manifest and deliverable-purpose rules
6. OutputPart and OutputBatch domain/store
7. Ordered final AgentResult → OutputBatch assembly
8. ClientOutputRenderer registry and deterministic fallbacks
9. Telegram ordered document grouping and aggregate receipts
10. Client-facing done after output completion
11. Telegram semantic input resolver foundation
12. Observability, recovery and end-to-end tests
13. Documentation/readme stabilization
```

Каждый шаг должен сохранять старый compatibility path через adapter, но не
создавать вторую независимую state machine.

---

## AF-22. Не входит в текущий code scope

```text
CycleInbox и active-cycle user additions
side-query LLM lane
provider-specific ContextComposer
interactive questions during RUNNING
automatic classification question/addition
background transcription/OCR/video analysis
semantic RAG over media
PostgreSQL migration
distributed delivery workers
Redis/arq queues
object storage
antivirus/media sandbox
full Web UI components
all Telegram native media renderers at once
```

Для не реализованных media types вводятся contracts/registry extension points,
но adapter может временно возвращать typed unsupported capability result.

---

## AF-23. Итоговая архитектурная граница

После `v0.4-file-artifacts-advanced`:

```text
v0.4-file-artifacts
→ exact content/artifact/version/delivery foundation

v0.4-file-artifacts-advanced
→ client capability, localization, semantic media foundation,
  response anchor, presentation и ordered OutputBatch delivery

v0.4-input-runtime
→ active-cycle interaction, CycleInbox, safe checkpoints,
  ContextComposer и interactive/side-query semantics

v0.5
→ PostgreSQL, extraction, chunks, embeddings и RAG

v0.6
→ workers, queues, distributed outbox и orchestration
```

Главный invariant:

```text
transport-specific детали не протекают в artifact domain или LLM protocol,
а future processing не заставляет менять stable semantic input/output contracts.
```
