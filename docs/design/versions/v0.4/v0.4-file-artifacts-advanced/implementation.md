---
id: design.v0.4.semantic-interaction-implementation
version: v0.4
spec_status: accepted
implementation_status: implemented
---
# v0.4 — Реализация и архитектурные границы semantic interaction

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-21. Реализованный контур

Compatibility paths сохранены на границах adapter/API, но authoritative
state machines едины: Telegram text использует shared ingress, а Telegram
result delivery исполняет `OutputDeliveryPlan`, не legacy artifact projection.

### AF-21.1. Фактическая раскладка кода

| Контур | Реализация |
|---|---|
| Capability contracts и read-time integrity | `src/interaction/capabilities.py`, `capability_store.py` |
| Localization | `src/localization/service.py`, каталоги `ru.json`, `en.json` |
| Anchor, reply provenance, presentation | `src/interaction/anchors.py`, `presentation.py`, `presentation_store.py`, `presentation_service.py` |
| Semantic input/limits | `src/interaction/parts.py`, `src/ingress/models.py`, `semantic_limits.py`, `src/adapters/telegram_ingress.py` |
| Grouped ingress application commit | `src/ingress/service.py`, `grouping.py`, `coordinated_store.py` |
| Structural schema upgrades | `src/ingress/upgrades.py` |
| Artifact manifest/order | `src/artifacts/runtime.py`, `delivery.py` |
| Output domain/store/recovery | `src/interaction/output_models.py`, `output_store.py` |
| Assembly/rendering | `src/interaction/output_service.py`, `rendering.py` |
| Atomic aggregate completion | `src/interaction/output_completion.py` |
| Telegram plan execution | `src/servers/telegram/output_plan_executor.py` |
| API/transport bridge | `src/api/artifact_routes.py`, `artifact_transport.py`, `src/servers/telegram/artifact_bridge.py`, `telegram_server.py` |

Input/presentation lifecycle:

```text
InputBatchDraft → CommittedInputBatch

presentation:
reserved
→ bound
→ closed | failed | expired

committed-before-bind:
reserved + pending_terminal_state
→ late bind
→ closed | failed
```

Grouped commit выполняется application service: batch commit, terminal
presentation update и structured `ack_policy`/event/ref являются одним
workflow. Terminal bind не является silent no-op. Expired indexed reservation
reclaim создаёт новый handle и заменяет index.

Output lifecycle:

```text
OutputBatch.ready
→ delivering
→ delivered
 | partially_delivered
 | failed
 | unknown
```

`TelegramOutputPlanExecutor` выполняет groups по `group.index`, вызывает native
Telegram methods и создаёт exact outcome для каждого part. `UNSUPPORTED` и
отсутствующий outcome не считаются delivered. Для документов:

```text
1 → send_document
2..10 → send_media_group
11 → send_media_group(10), затем send_document(1)
```

После начала non-idempotent send timeout/network/mismatched receipt становится
`unknown`. `OutputDeliveryCompletionService` валидирует authority и атомарно
обновляет delivery records, attempt receipt и OutputBatch; filesystem failure
откатывает все три слоя.

Recovery:

- `ready` после restart остаётся claimable;
- stale `delivering` становится `unknown`;
- `unknown` не отправляется повторно автоматически;
- internal list/reconcile API поддерживает explicit reconciliation;
- `delivered` не запускает повторный agent cycle или transport delivery.

### AF-21.2. Совместимость и v1 → v2

Выбран вариант A — structural compatibility upgrade:

- persisted ingress `v1` читается как structural `v2` view;
- отсутствующие lineage, anchor, reply provenance и capability snapshot не
  изобретаются;
- runtime fallback явно помечается `legacy_derived=True`;
- immutable persisted `v1` record не переписывается без отдельной migration
  command;
- старые stored attachment records не получают выдуманные lineage/version;
- старые конфиги без новых root sections используют typed defaults;
- legacy `/message` остаётся для других compatibility clients, но обычный
  Telegram text проходит shared ingress;
- `AgentResult.artifacts` остаётся compatibility projection и не является
  authority Telegram-доставки;
- route authority не зависит от response anchor и не меняется при join.

### AF-21.3. Проверка

Целевые test modules:

```text
tests/test_input_presentation_lifecycle.py
tests/test_telegram_output_plan_executor.py
tests/test_output_delivery_recovery.py
tests/test_semantic_ingress_limits.py
tests/test_reply_provenance.py
tests/test_capability_snapshot_integrity.py
tests/test_unified_input_runtime_foundation.py
tests/test_artifact_ingress_grouping.py
tests/test_advanced_interaction_runtime.py
tests/test_telegram_semantic_resolvers.py
```

Они закрепляют:

- atomic late bind, strict terminal bind, grouped close и expired reclaim;
- 10-file group + instruction: один batch/presentation/create-ack, instruction
  anchor и terminal close;
- silent fallback клиента без `presentation.message_edit`;
- native location/contact/image, mixed ordering и independence от legacy
  artifact order;
- 11-document split, missing outcome, pre/post-send failures и отсутствие
  automatic resend;
- atomic receipt completion/rollback, restart recovery, separate `unknown` и
  explicit reconciliation;
- caption exactly once, cumulative semantic limits и semantic ID collision;
- capability snapshot tampering/index/symlink rejection;
- incoming reply provenance отдельно от typed response anchor override;
- RU/EN rendering, deterministic missing-key fallback и plural forms;
- structural `v1 → v2` с `legacy_derived=True`.

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
distributed/exactly-once Telegram delivery
```

Поддержанные capability-plan operations включают text/status/document,
document group, image, audio, voice, video, video note, animation, sticker,
location и contact. Не поддержанная capability получает deterministic
localized fallback либо typed unsupported outcome.

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
