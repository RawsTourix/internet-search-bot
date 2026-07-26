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
| Output domain/store/recovery | `src/interaction/output_models.py`, `output_store.py`, `output_outbox.py`, `output_claim.py` |
| Assembly/rendering | `src/interaction/output_service.py`, `rendering.py` |
| Atomic aggregate completion и evidence | `src/interaction/output_completion.py`, `output_evidence.py` |
| Telegram plan execution | `src/servers/telegram/output_plan_executor.py`, `scoped_output_executor.py` |
| Telegram READY outbox | `src/servers/telegram/ready_outbox.py`, `scoped_ready_outbox.py` |
| Exact client-instance transport bridge | `src/servers/telegram/scoped_artifact_bridge.py` |
| Canonical Telegram composition | `src/servers/telegram/app.py`, `src/servers/telegram/__main__.py` |
| API/transport bridge | `src/api/artifact_routes.py`, `artifact_transport.py`, `output_outbox_routes.py`, `legacy_delivery_guard.py` |

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

Каждый `OutputPartReceipt` сохраняет exact `part_id`, `index`, `required`,
transport message IDs и outcome. Для artifact отдельно сохраняется
`artifact_content_state`, поэтому доставка текстового fallback не выдаётся за
доставку байтов, а неудачная дополнительная подпись не отменяет уже
подтверждённый media upload. Подтверждённая доставка части многочастного текста
представляется как `partially_delivered`, а не как полный failure.

Aggregate state определяется по обязательным parts; optional failure не
отменяет подтверждённую доставку всех required parts. Любой `unknown`, включая
optional part, остаётся видимым на уровне всего OutputBatch и требует
reconciliation. External transport receipt проходит strict evidence policy до
любых durable mutations.

`TelegramOutputPlanExecutor` выполняет groups по `group.index`, вызывает native
Telegram methods и создаёт exact outcome для каждого part. `UNSUPPORTED` и
отсутствующий outcome не считаются delivered. Финальный Markdown проходит
через общий Telegram formatting path с HTML-rendering и plain-text fallback.
Для документов:

```text
1 → send_document
2..10 → send_media_group
11 → send_media_group(10), затем send_document(1)
```

После начала non-idempotent send timeout/network/mismatched receipt становится
`unknown`. `OutputDeliveryCompletionService` валидирует authority и атомарно
обновляет delivery records, attempt receipt и OutputBatch; filesystem failure
откатывает все три слоя. Публичный `output_store.reconcile_unknown()` в
application composition делегирует тому же composite service, поэтому explicit
reconciliation согласованно меняет и OutputBatch, и связанные artifact delivery
records.

### AF-21.2. READY outbox и crash boundary

Безопасный process-local recovery разделяет состояния:

```text
READY
→ transport ещё не начинался
→ bounded automatic claim допустим

DELIVERING
→ transport outcome может быть ambiguous
→ stale recovery в UNKNOWN

UNKNOWN / PARTIALLY_DELIVERED / FAILED / DELIVERED
→ automatic resend запрещён
```

`ReadyOutputOutboxService` публикует только достаточно старые `FINAL + READY`
точного `client_type + client_instance_id`. Worker не повторяет agent cycle:
он claim-ит immutable OutputBatch, исполняет уже committed plan и сохраняет exact
receipt.

Claim имеет отдельный `oclm_*` idempotency key. Потерянный HTTP-ответ после
успешной записи claim повторяется с тем же key и возвращает исходный
`attempt_id`; другой key не может присоединиться к активному attempt. State и
claim-request index записываются под одним process-local lock с rollback.

Artifact bytes для Telegram доступны только через:

```text
claimed FINAL OutputBatch
+ exact session_id
+ exact client_type
+ exact client_instance_id
+ exact delivery_id member
→ scoped content stream
```

Legacy `/internal/deliveries/{id}/content` сохранён для других compatibility
transports, но Telegram на Gateway получает conflict и обязан использовать
instance-scoped outbox route.

Каноническая точка запуска полного Telegram runtime:

```bash
python -m src.servers.telegram
```

или:

```bash
uvicorn src.servers.telegram.app:app --host 0.0.0.0 --port 8001
```

Низкоуровневый `telegram_server:app` остаётся compatibility webhook entrypoint,
но не является полным production composition root.

### AF-21.3. Совместимость и v1 → v2

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
- persisted receipts без нового `required`/`artifact_content_state` читаются с
  безопасной backward-compatible inference;
- route authority не зависит от response anchor и не меняется при join.

### AF-21.4. Проверка

Целевые test modules включают:

```text
tests/test_input_presentation_lifecycle.py
tests/test_telegram_output_plan_executor.py
tests/test_output_delivery_recovery.py
tests/test_output_receipt_semantics.py
tests/test_artifact_content_delivery_evidence.py
tests/test_output_evidence_policy.py
tests/test_output_claim_idempotency.py
tests/test_ready_output_outbox.py
tests/test_output_outbox_api_authority.py
tests/test_output_outbox_delivery_content.py
tests/test_telegram_ready_outbox_claim_retry.py
tests/test_telegram_scoped_output_execution.py
tests/test_legacy_telegram_delivery_guard.py
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
- native location/contact/image, mixed ordering и independence от legacy order;
- 11-document split, pre/post-send failures и отсутствие ambiguous resend;
- required/optional semantics, partial text и artifact content evidence;
- Markdown → Telegram HTML и deterministic plain-text fallback;
- atomic receipt completion/rollback и artifact-aware reconciliation;
- final-only READY projection и exact client-instance authority;
- idempotent claim replay и rollback при claim-index failure;
- scoped byte streaming только для exact manifest member;
- запрет legacy Telegram content bypass;
- caption exactly once, cumulative semantic limits и semantic ID collision;
- capability snapshot tampering/index/symlink rejection;
- incoming reply provenance отдельно от typed response anchor override;
- RU/EN rendering и structural `v1 → v2`.

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
  response anchor, presentation, ordered OutputBatch delivery,
  process-local final READY outbox и exact transport receipts

v0.4-input-runtime
→ active-cycle interaction, CycleInbox, safe checkpoints,
  ContextComposer и interactive/side-query semantics

v0.5
→ PostgreSQL, extraction, chunks, embeddings и RAG

v0.6
→ workers, queues, distributed outbox, leases и orchestration
```

Главный invariant:

```text
transport-specific детали не протекают в artifact domain или LLM protocol,
а future processing не заставляет менять stable semantic input/output contracts.
```
