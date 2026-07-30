---
id: design.v0.4.batch-workflows.draft-control-foundation
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-30
---

# BW-15 — Explicit draft-control foundation

## 1. Уточнение domain model

Пустой explicit collection и `InputBatchDraft` являются разными объектами.

```text
InputCollectionRecord
→ пользователь включил explicit collection mode
→ пакет может быть ещё пустым
→ authoritative exact scope и commit policy уже существуют

InputBatchDraft
→ появился хотя бы один реальный ingress event
→ сохраняется прежний инвариант source_event_ids != []
→ attachments/text/semantic parts проходят обычный durable ingress
```

Такое разделение не ослабляет транспортные и idempotency-инварианты существующего
`InputBatchDraft` ради поддержки пустого `/collect`.

После первого принятого события collection связывается с draft:

```text
InputCollectionRecord.bound_input_batch_id
→ exact InputBatchDraft.input_batch_id
```

Связь однонаправленная и неизменяемая: active collection нельзя перепривязать к
другому batch.

## 2. Persisted policy

`InputCollectionRecord` хранит server-owned policy:

```text
assembly_mode = explicit
commit_policy = explicit
```

AUTO policy существующего transport draft не копируется и не переписывается.
`/collect` поверх совместимого files-first draft создаёт explicit collection и
привязывает его к существующему batch. До Telegram wiring transport timer должен
быть остановлен adapter/runtime coordination; этот шаг принадлежит BW-P2B.

## 3. Exact scope

`InputDraftScope` включает:

```text
session_id
client_type
client_instance_id
conversation_id
optional thread_id
principal_id
```

`client_instance_id` проверяется по authoritative первому `ClientIngressEvent`, а
не выводится из изменяемого display metadata или filename.

На один exact scope допускается не более одной active collection в состояниях:

```text
collecting
commit_requested
```

## 4. Durable state

```text
COLLECTING
→ COMMIT_REQUESTED
→ COMMITTED

COLLECTING | COMMIT_REQUESTED
→ CANCELLED | ABANDONED | FAILED
```

Terminal record освобождает scope index, но остаётся audit evidence.

## 5. InputDraftControlService

Transport-neutral service предоставляет:

```python
start_collection(scope, response_route, locale, idempotency_key)
inspect(scope)
bind_batch(scope, input_batch_id)
commit(scope, idempotency_key)
cancel(scope, idempotency_key)
```

Client adapters не изменяют JSON/filesystem records напрямую.

### start_collection

- создаёт пустой explicit collection;
- повтор с тем же idempotency key возвращает сохранённый результат;
- повтор с новым key возвращает `already_active`;
- один совместимый AUTO files-first draft связывается с collection;
- несколько совместимых drafts дают explicit conflict.

### commit

`/send` не требует отдельной текстовой инструкции:

```text
bound files-only draft
→ valid explicit commit
```

Пустой unbound collection возвращает `empty` и остаётся active.

Если attachment ещё загружается:

```text
state = commit_requested
commit_requested_at persisted
```

Повторный commit после terminal upload state завершает immutable
`CommittedInputBatch`.

### cancel

- unbound collection становится `cancelled`;
- bound draft переводится в exact `InputBatchDraftState.CANCELLED`;
- соседние drafts того же session, но другого principal/client instance не
  затрагиваются;
- agent cycle не запускается.

## 6. Control idempotency

Action index хранит fingerprint:

```text
exact scope + action
```

Один idempotency key нельзя повторно использовать с другим action или scope.
Crash между domain transition и action-result write не нарушает semantics:
start, commit и cancel повторно разрешаются по durable collection/batch state.

## 7. Composition

`IngressServices` теперь включает:

```text
collection_store
 draft_control_service
```

Telegram/Web/CLI wiring использует один и тот же service.

## 8. Текущая граница

BW-P2A не добавляет Telegram-команды и не изменяет AUTO routing. Следующий slice:

```text
shared HTTP control routes
→ Telegram /collect /send /cancel
→ aliases /batch /done
→ routing новых events в active explicit collection
→ остановка automatic media-group commit после promotion
```

Новых `.env` или `mcp.config` keys в BW-P2A нет.
