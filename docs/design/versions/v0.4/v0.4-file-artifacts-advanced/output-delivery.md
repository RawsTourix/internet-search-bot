---
id: design.v0.4.output-delivery
version: v0.4
spec_status: accepted
implementation_status: implemented
---
# v0.4 — OutputBatch и client delivery

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-9. OutputBatch domain

### AF-9.1. InputBatch и OutputBatch не являются одним классом

Симметрия архитектурная:

```text
InputBatch
→ atomic logical user input

OutputBatch
→ ordered logical agent output and delivery lifecycle
```

У них разные state machines и invariants.

InputBatch invariant:

```text
agent cycle не запускается по неполному logical input
```

OutputBatch invariant:

```text
logical response сохраняет порядок, composition и delivery state
```

### AF-9.2. OutputBatch model

```python
class OutputBatch(BaseModel):
    output_batch_id: str
    session_id: str
    cycle_id: str
    sequence_number: int

    kind: Literal[
        "status",
        "progress",
        "intermediate",
        "interactive",
        "final",
    ]

    response_route: ClientResponseRoute
    response_anchor: ClientResponseAnchor | None
    locale: str
    capability_snapshot: ClientCapabilitySnapshot

    parts: list[OutputPart]

    state: Literal[
        "draft",
        "ready",
        "delivering",
        "partially_delivered",
        "delivered",
        "failed",
        "cancelled",
    ]

    created_at: datetime
    ready_at: datetime | None
    completed_at: datetime | None
```

`parts` имеют stable `part_id` и monotonic `index`.

### AF-9.3. Scope текущего обновления

В code scope `v0.4-file-artifacts-advanced` входят:

```text
status
progress
final
```

`intermediate` может быть представлен foundation contract, но полноценный
lifecycle допускается только без нарушения active-cycle protocol.

`interactive` полностью относится к `v0.4-input-runtime`.

### AF-9.4. OutputBatch assembly

```text
final AgentAction text
+ selected ArtifactDeliveryRefs
+ optional semantic output intents
→ ordered OutputParts
→ validate capability/fallback plan
→ commit OutputBatch
→ publish to ClientResponseOutbox
```

Agent result и selected delivery records остаются authoritative sources.

LLM final text не является source of truth delivery state.

### AF-9.5. Ordered delivery

Порядок, заданный `artifact_set_delivery`, сохраняется через:

```text
selection_index
→ OutputPart.index
→ DeliveryPlan item index
→ TransportOperation index
→ receipt index
```

Filesystem enumeration order, dictionary order и creation timestamps не могут
заменять explicit order.

---

## AF-10. Client delivery planning

### AF-10.1. Telegram document groups

Если capability snapshot содержит:

```text
output.artifact.document
output.group.document
```

renderer может сгруппировать совместимые artifacts.

Policy:

```text
1 document
→ send as one document

2..max_items compatible documents
→ one document group/album

more than max_items
→ stable ordered groups

incompatible media classes
→ partition into compatible groups
```

Transport limit берётся из capability snapshot, а не hardcoded business logic
OutputBatch.

### AF-10.2. Group delivery receipts

Одна transport group создаёт aggregate attempt, но сохраняет part-level
receipts:

```python
class OutputDeliveryReceipt(BaseModel):
    output_batch_id: str
    attempt_id: str
    state: Literal[
        "delivered",
        "partially_delivered",
        "failed",
        "unknown",
    ]
    part_receipts: list[OutputPartReceipt]
```

Для каждого part сохраняются:

- `part_id`;
- `delivery_id`, если это artifact;
- client message/media IDs;
- state;
- error category;
- delivered_at.

### AF-10.3. `cycle_done` и delivery completion

Разделяются состояния:

```text
Agent execution completed
→ result_ready

OutputBatch committed
→ output_ready

transport delivery active
→ delivering

required parts received successful receipts
→ delivered
```

Client-facing `done` публикуется только после required final OutputBatch
completion.

До этого допустимо:

```text
Результат подготовлен. Отправляю файлы…
```

Если доставлена только часть:

```text
output_batch.partially_delivered
```

а не ложное success-сообщение.

### AF-10.4. Unknown delivery

Неопределённый timeout после начала transport upload получает `unknown`.

Runtime не должен бесконечно автоматически повторять non-idempotent transport
operation.

Recovery policy использует:

- transport idempotency, если доступна;
- client message bindings;
- explicit user retry;
- bounded/manual reconciliation.

---

## AF-11. Response route, response anchor и presentation

### AF-11.1. Route и anchor — разные сущности

```text
response_route
→ куда доставлять: client instance, conversation, thread

response_anchor
→ на какое исходное client message/reply target ссылаться
```

Route не изменяется только потому, что к InputBatch добавилась новая часть.

Anchor может обновляться по deterministic policy.

### AF-11.2. Anchor selection policy

Приоритет:

```text
explicit user reply target / explicit instruction
→ latest meaningful text instruction
→ caption containing instruction
→ latest attachment event
→ first batch event
```

Meaningful text определяется transport metadata и part kind, а не свободным
LLM-classifier.

В сценарии:

```text
10-file media group
→ отдельное сообщение с полной инструкцией
```

финальный OutputBatch отвечает на сообщение-инструкцию.

### AF-11.3. Anchor immutability after output commit

После commit OutputBatch response anchor immutable.

Позднее correction/continuation создаёт новый input/output batch и не меняет
историческую доставку.

### AF-11.4. Один presentation status на InputBatch

На один открытый `input_batch_id` существует не более одного active
presentation handle для одного client binding.

```python
class InputBatchPresentationRef(BaseModel):
    input_batch_id: str
    client_binding_id: str
    presentation_id: str
    client_message_id: str | None
    state: str
    updated_at: datetime
```

Новое событие, присоединённое к draft:

- не создаёт обязательное новое client message;
- обновляет counts/state;
- вызывает structured presentation callback;
- renderer редактирует существующее сообщение, если capability разрешает;
- иначе использует silent acknowledgement или throttled update.

### AF-11.5. Structured acknowledgement

API возвращает:

```json
{
  "input_batch_id": "ibat_...",
  "state": "collecting",
  "ack_policy": "update_existing",
  "presentation_event": {
    "message_key": "input_batch.updated",
    "params": {
      "file_count": 10,
      "text_part_count": 1
    }
  }
}
```

API не должен возвращать готовую русскую/английскую фразу как единственный
semantic result.

---

