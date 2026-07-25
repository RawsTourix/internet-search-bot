---
id: design.v0.4.semantic-interaction-contracts
version: v0.4
spec_status: accepted
implementation_status: implemented
---
# v0.4 — Semantic interaction contracts и acceptance criteria

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-17. Data models foundation

Минимальные новые domain contracts:

```text
ClientCapabilitySpec
ClientCapabilityRegistry
ClientCapabilitySnapshot

LocalizationMessage
LocalizationCatalog
LocalizationService

ClientResponseAnchor
InputBatchPresentationRef

InputPart union
OutputPart union

OutputBatch
OutputDeliveryPlan
OutputDeliveryAttempt
OutputDeliveryReceipt

ClientInputResolver
ClientOutputRenderer
```

Filesystem v0.4 layout может быть:

```text
storage/
  client_bindings/
  input_presentations/
  output_batches/
  output_attempts/
  localization_snapshots/   # optional metadata, not duplicate catalogs
```

Не обязательно создавать отдельный файл на каждое мелкое событие. Store layout
определяется нагрузочными тестами, но domain IDs/state transitions обязательны.

IDs:

```text
cbs_*   capability snapshot
anch_*  response anchor
iprs_*  input presentation
obat_*  output batch
opart_* output part
odat_*  output delivery attempt
```

Точные prefixes могут быть скорректированы перед реализацией, но должны быть
stable и не пересекаться с `art_*`, `cnt_*`, `dlv_*`, `ibat_*`.

---

## AF-18. Configuration categories

```text
client_capabilities:
  contract_version
  reject_unknown_features
  max_feature_count
  max_limit_count

localization:
  default_locale
  supported_locales
  fallback_locale
  fail_on_missing_required_key

input_presentation:
  enabled
  update_throttle_seconds
  max_updates_per_batch

output_runtime:
  enabled
  max_parts_per_batch
  max_total_artifacts
  max_delivery_groups
  delivery_claim_timeout_seconds

telegram_output:
  prefer_document_groups
  status_message_editing
```

Transport limits не дублируются в global config, если они уже объявлены
capability snapshot. Server config может задавать policy ceiling, но effective
limit вычисляется как минимум server policy и client-declared limit.

---

## AF-19. Observability

Новые trace/progress events:

```text
client_capabilities_resolved
client_capabilities_rejected

input_batch_presentation_created
input_batch_presentation_updated
input_batch_response_anchor_updated

output_batch_created
output_batch_ready
output_batch_delivery_started
output_batch_part_delivered
output_batch_part_failed
output_batch_part_unknown
output_batch_delivered
output_batch_partially_delivered
output_batch_failed
output_batch_unknown

client_output_fallback_selected
client_output_group_created
localization_rendered
localization_missing_key
```

Events содержат IDs, states, capability IDs, part indexes и bounded metadata.

Они не содержат:

- raw binary;
- full artifact text;
- auth tokens;
- transport download URLs;
- local paths;
- full voice/video payload;
- secret client metadata.

---

## AF-20. Acceptance criteria

### AF-20.1. Capability registry

```text
client declares known canonical capability IDs
→ accepted snapshot
→ semantics identical for all clients
```

```text
client declares unknown/invalid capability
→ reject or ignore according to explicit versioned policy
→ never reinterpret silently
```

```text
Telegram-specific caption limit
→ affects only Telegram renderer
→ Web/CLI output is not truncated by it
```

### AF-20.2. Localization

```text
same presentation event + ru/en
→ correct catalog key and typed params
→ deterministic fallback
```

```text
missing required key
→ diagnostic event
→ fallback locale or stable key
→ never empty message
```

```text
counts in status
→ morphology-light universal layout
→ no invalid plural/case agreement
```

### AF-20.3. Response anchor

```text
media group + later text instruction
→ one InputBatch
→ response anchor points to instruction message
```

```text
route remains same conversation/thread
→ anchor update does not mutate route authority
```

```text
user replies to an old message with a new instruction
→ old message is preserved as ClientReplyContext
→ current instruction is response anchor
```

### AF-20.4. Input presentation

```text
10 files + instruction joined to one open draft
→ one client status handle
→ updates are edited/throttled, not 11 new messages
```

```text
client lacks message editing
→ silent or throttled fallback
→ domain state unchanged
```

```text
atomic batch commits before status bind
→ presentation keeps pending terminal intent
→ late bind stores client_message_id
→ presentation closes
```

### AF-20.5. OutputBatch

```text
final text + four selected artifacts
→ one committed final OutputBatch
→ stable part indexes
```

```text
four compatible Telegram documents
→ one ordered document group
→ four part receipts
```

```text
selected order A,B,C,D
→ transport order A,B,C,D
→ receipt order A,B,C,D
```

```text
agent execution done, deliveries pending
→ result_ready/delivering status
→ no client-facing final done yet
```

```text
all required receipts successful
→ output_batch.delivered
→ final done status
```

```text
one part fails
→ partially_delivered/failed according to policy
→ exact failed part visible in receipt
```

```text
transport result becomes ambiguous after send started
→ exact part and OutputBatch become unknown
→ no automatic resend
→ explicit reconciliation remains possible
```

### AF-20.6. Semantic media

```text
Telegram voice message
→ exact bytes stored
→ VoiceInputPart + metadata
→ no automatic transcription in core
```

```text
location input
→ structured LocationInputPart
→ no fake binary artifact required
```

```text
agent emits LocationOutputPart
→ Telegram native location when supported
→ text fallback when unsupported
```

### AF-20.7. Artifact manifest

```text
10 input artifacts
→ bounded manifest with exact ID + filename + format + size
→ no raw content/local path
```

```text
cycle compaction
→ manifest rebuilt from authoritative store
→ no dependency on LLM summary
```

### AF-20.8. Deliverable purpose

```text
user requests four output files
→ all four purpose=deliverable
→ all four visible in final deliverable projection
```

```text
intermediate helper file
→ purpose=working
→ not delivered automatically
```

### AF-20.9. Recovery

```text
process restart after OutputBatch commit
→ batch/attempt state recovered
→ no repeat agent cycle
```

```text
stale delivering claim after restart
→ OutputBatch becomes unknown
→ no automatic resend
```

```text
filesystem failure between attempt receipt and OutputBatch state
→ delivery records, receipt and state roll back together
→ no half-committed completion
```

Filesystem v0.4 может гарантировать process-restart recovery в пределах текущей
outbox implementation. Полная distributed exactly-once delivery не заявляется.

---

