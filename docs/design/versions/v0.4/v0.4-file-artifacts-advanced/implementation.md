---
id: design.v0.4.semantic-interaction-implementation
version: v0.4
spec_status: accepted
implementation_status: implemented
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

### AF-21.1. Фактическая раскладка кода

| Контур | Реализация |
|---|---|
| Capability contracts/store | `src/interaction/capabilities.py`, `capability_store.py` |
| Localization | `src/localization/`, каталоги `ru.json` и `en.json` |
| Anchor/presentation | `src/interaction/anchors.py`, `presentation*.py` |
| Semantic input | `src/interaction/parts.py`, `src/adapters/telegram_resolvers.py` |
| Ingress schema upgrades | `src/ingress/upgrades.py` |
| Artifact manifest/order | `src/artifacts/runtime.py`, `delivery.py` |
| Output domain/store | `src/interaction/output_models.py`, `output_store.py` |
| Assembly/rendering | `src/interaction/output_service.py`, `rendering.py` |
| API/Telegram delivery | `src/api/artifact_routes.py`, `src/servers/telegram/artifact_bridge.py` |

Durable lifecycle:

```text
InputBatchDraft
→ CommittedInputBatch
→ agent result_ready
→ OutputBatch.ready
→ OutputBatch.delivering
→ delivered | partially_delivered | failed
```

`unknown` transport receipt сохраняется как terminal failure с точной
part-level причиной и не порождает автоматический resend. Устаревший
`delivering` claim после restart консервативно reconciles в `unknown`.

Presentation lifecycle:

```text
reserved → bound → closed
                   failed
                   expired
```

Публичный presentation token возвращается только при первой reservation; на
диске сохраняется только hash. Grouped Telegram input привязывает уже созданное
status message к единственному presentation handle.

### AF-21.2. Совместимость

- persisted ingress schema `v1` явно обновляется до `v2`;
- старые stored attachment records не получают выдуманные lineage/version;
- старые committed batches без capability snapshot получают один
  детерминированный legacy snapshot до запуска агента;
- старые конфиги без новых root sections используют typed defaults;
- text-only `call_agent` остаётся compatibility entrypoint;
- route authority не зависит от response anchor и не меняется при join.

### AF-21.3. Проверка

Основные новые тесты:

```text
tests/test_advanced_interaction_runtime.py
tests/test_telegram_semantic_resolvers.py
```

Они покрывают strict capabilities, snapshot immutability/dedup, ru/en
pluralization, anchor priority, token hashing, output ordering/idempotency,
aggregate receipts, stale-claim reconciliation, schema upgrades и
transport-only semantic normalization.

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
