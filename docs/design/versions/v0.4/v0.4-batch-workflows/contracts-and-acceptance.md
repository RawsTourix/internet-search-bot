---
id: design.v0.4.batch-workflows.contracts-acceptance
version: v0.4
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-07-29
---

# BW-10–BW-12 — Contracts, migration and acceptance

## BW-10. Architecture and ownership

### BW-10.1. Shared domain services

```text
InputDraftControlService
→ start/inspect/commit/cancel explicit and compatible AUTO drafts

InputBatchAssembler
→ append durable ingress events and apply assembly/commit policy

InputPresentationCoordinator
→ render, relocate, bind and finalize durable presentation

ArtifactCatalogService
→ authorized scoped listing/search/get and activation

CapabilityOutputRenderer
→ stable semantic partition of OutputParts

ClientOutputPlanExecutor
→ execute exact transport operations and receipts
```

### BW-10.2. Client adapters

Telegram/Web/CLI adapters own only:

- normalization of client events/actions;
- client-specific ordering/group identifiers;
- rendering native controls;
- transport calls;
- conversion of native receipts into common records.

Adapters do not own:

- authoritative draft state;
- exact artifact authorization;
- commit/cancel semantics;
- output part ordering;
- cycle admission;
- retention/deletion policy.

### BW-10.3. API boundary

Transport-neutral control endpoints/actions:

```text
POST /input-drafts/collect
GET  /input-drafts/active
POST /input-drafts/{id}/commit
POST /input-drafts/{id}/cancel
```

Exact route shape may follow current API conventions, but contracts require:

- authenticated exact client/session/principal scope;
- idempotency key for mutations;
- optional expected draft ID/generation;
- no client-owned authoritative status;
- structured result with presentation intent.

Example result:

```json
{
  "input_batch_id": "ibat_...",
  "state": "collecting",
  "assembly_mode": "explicit",
  "commit_policy": "explicit",
  "text_part_count": 2,
  "attachment_count": 3,
  "stored_attachment_count": 3,
  "control_result": "collection_active"
}
```

### BW-10.4. Command routing

Slash commands and callbacks normalize into `SessionControlAction`:

```python
class InputDraftControlKind(str, Enum):
    START_COLLECTION = "start_collection"
    COMMIT_DRAFT = "commit_draft"
    CANCEL_DRAFT = "cancel_draft"
    INSPECT_DRAFT = "inspect_draft"
```

Они не проходят через LLM и не становятся ordinary `ClientIngressEvent`
`text_parts`.

### BW-10.5. Configuration ownership

Shared behavior хранится в `mcp.config`, а не в `.env`:

```text
input_batch_workflows:
  enabled
  auto_attachment_requires_commit_intent
  logical_quiet_timeout_seconds
  auto_attachment_maximum_wait_seconds
  explicit_collection_idle_timeout_seconds
  max_active_explicit_drafts_per_scope

input_presentation:
  relocation_enabled
  relocation_coalesce_seconds
  delete_superseded_messages
```

Transport credentials, bot instance and deployment URLs остаются в `.env`.

Эти keys являются целевым contract. Они не считаются runtime-supported, пока
implementation patch одновременно не обновит:

```text
src/api/mcp.config.example
.env.example, если появляется transport env key
config validation models
documentation/release notes
tests for defaults and invalid values
```

Нельзя добавлять скрытый parameter только в коде.

### BW-10.6. Default policy

Целевые безопасные defaults:

```text
batch workflows enabled                    = true
auto attachment requires commit intent     = true
logical quiet timeout                      = test-derived bounded value
explicit collection idle timeout           = bounded, much longer than AUTO
max active explicit drafts per exact scope = 1
presentation relocation                    = true
delete superseded messages                 = true, best effort only
```

Конкретные durations фиксируются implementation tests и examples. LLM их не
вычисляет и не передаёт.

## BW-11. Persistence, migration and observability

### BW-11.1. Schema evolution

Новые optional/defaulted поля draft metadata:

```text
assembly_mode = auto
commit_policy = automatic
commit_requested_at = null
explicit_collection_started_at = null
explicit_idle_deadline = null
presentation_generation = 0
```

Filesystem reader применяет defaults к старым records. Existing immutable
committed batches не переписываются.

### BW-11.2. Existing open drafts

На первом startup после migration:

- старые AUTO ready drafts обрабатываются существующей recovery policy;
- старые files-only drafts без explicit intent не запускаются автоматически;
- conflicting/zombie drafts переходят в `ABANDONED` с audit code;
- новый EXPLICIT state не выводится из filename, text content или возраста.

### BW-11.3. Presentation migration

Старый presentation record без generation:

```text
usable active message ID → generation=1
нет usable handle        → generation=0, create on next presentation action
```

Старые messages не удаляются массово при startup.

### BW-11.4. Observability

Обязательные structured events:

```text
input_collection_started
input_collection_upgraded_from_auto
input_collection_part_appended
input_collection_commit_requested
input_collection_committed
input_collection_cancelled
input_collection_abandoned
input_collection_conflict

input_presentation_relocation_started
input_presentation_relocation_bound
input_presentation_old_delete_succeeded
input_presentation_old_delete_failed

output_group_planned
telegram_media_group_attach_mapping_built
telegram_media_group_delivered
telegram_media_group_bad_request
telegram_media_group_fallback

artifact_catalog_listed
artifact_activated
historical_artifact_selected
```

Логи не содержат file bytes, secrets, full user text или bot token.

### BW-11.5. Metrics

Минимальные counters/histograms:

```text
active_drafts{mode,client}
draft_duration_seconds{mode,result}
draft_parts_total{kind}
presentation_relocations_total{result}
media_groups_total{class,result}
media_group_items{class}
artifact_activations_total{scope,reason}
```

### BW-11.6. Security

- Control action проверяет exact principal scope.
- Callback tokens server-issued, bounded и expiring.
- Historical catalog соблюдает authorization before metadata disclosure.
- Group upload использует exact claimed delivery bytes.
- File handles/URLs/attach names не попадают в LLM context.
- `/cancel` не удаляет immutable history без retention workflow.
- `/send` не принимает client-supplied artifact IDs вне active draft authority.

## BW-12. Acceptance criteria

### BW-12.1. AUTO input

1. Обычный text без open draft запускается без artificial file-wait delay.
2. File/album first открывает один AUTO draft и не запускает files-only cycle.
3. Text после files входит в тот же batch.
4. Несколько text parts после files сохраняют порядок и входят в один batch.
5. Новый file между инструкциями входит в тот же open batch и сбрасывает quiet.
6. Caption считается text part.
7. Text first, files later создают два batches без reverse guessing.
8. Maximum deadline files-only draft приводит к `ABANDONED`, не к agent run.

### BW-12.2. EXPLICIT input

9. `/collect` создаёт пустой EXPLICIT draft.
10. `/collect` повышает compatible AUTO draft без потери частей.
11. Повторный `/collect` идемпотентен.
12. Text first + files + text в EXPLICIT mode дают один batch.
13. `/send` коммитит text-only draft.
14. `/send` коммитит files-only draft.
15. `/send` коммитит mixed draft.
16. `/send` для пустого draft не запускает agent.
17. `/send` во время uploads сохраняет commit intent и ждёт terminal states.
18. `/cancel` не запускает agent и освобождает active indexes.
19. Другой sender в group не может commit/cancel draft владельца.
20. Один exact scope не получает второй explicit draft.

### BW-12.3. Presentation

21. Новая user part после status создаёт новый status ниже неё.
22. Новый handle durable-bound до удаления старого.
23. Успешное удаление старого фиксируется.
24. Failed deletion оставляет старое сообщение неизменным и unwritable.
25. Late progress generation не редактирует superseded handle.
26. Restart восстанавливает current generation.
27. Relocation/send/bind failure не оставляет batch без authoritative handle.

### BW-12.4. Output grouping

28. Два adjacent documents формируют одну group.
29. Один document отправляется отдельно.
30. Eleven documents partition into 10 + 1.
31. Text boundary не пересекается grouping.
32. Semantic photo/video run формирует visual album при capability support.
33. Generic image artifact остаётся document без visual intent.
34. OutputPart order сохраняется в plan/operations/receipts.
35. Raw file/bytes InputMedia создаёт unique `attach://` mapping.
36. Multipart fields совпадают с JSON attach names.
37. Live two-document Telegram group успешно доставляется native.
38. Confirmed group BadRequest даёт ordered individual fallback.
39. Timeout after send start остаётся UNKNOWN без duplicate fallback.

### BW-12.5. Artifact access

40. Current manifest bounded и не содержит всю историю.
41. `artifact_list(scope=current)` возвращает active set.
42. `scope=session/workspace` требует pagination и authorization.
43. Catalog result активирует exact artifact current cycle.
44. Historical exact artifact после activation можно прочитать/отправить.
45. Guessed unauthorized artifact ID не раскрывает metadata.
46. Compaction сохраняет bounded activation refs, но не content.
47. v0.5 semantic retrieval может активировать exact artifact без изменения
    delivery contract.

### BW-12.6. Recovery and integration

48. Restart сохраняет fully durable EXPLICIT draft.
49. Expired explicit draft становится ABANDONED.
50. Commit-requested draft завершается идемпотентно после restart.
51. Existing committed InputBatch не изменяется migration.
52. До `v0.4-input-runtime` active-session admission использует current behavior.
53. После подключения CycleInbox тот же committed batch enqueue-ится без изменения
    client/draft contracts.
54. Web и CLI contract tests используют тот же `InputDraftControlService` без
    Telegram-specific fields.

## Release gate

Обновление получает `implemented`, когда:

- thematic suites и полный Windows suite успешны;
- `.env.example`/`mcp.config.example` синхронизированы с runtime keys;
- Telegram live matrix покрывает AUTO, EXPLICIT, relocation и native groups;
- no files-only accidental cycle;
- no historical artifact hard prohibition;
- no hidden config parameters;
- documentation status соответствует фактическому code/test evidence.
