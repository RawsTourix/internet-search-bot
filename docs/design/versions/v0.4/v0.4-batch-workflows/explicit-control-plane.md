---
id: design.v0.4.batch-workflows.explicit-control-plane
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-16 — Explicit collection control plane

## 1. Реализованная граница

```text
/collect
→ active InputCollectionRecord exact scope
→ новые transport events входят в один explicit InputBatchDraft
→ /send durable-коммитит batch
→ отдельный /run запускает AgentCycle

/cancel
→ terminal collection + exact draft cancellation
→ AgentCycle не запускается
```

Обычный AUTO input не изменён. Text-only input без active collection остаётся
немедленным, files-first AUTO draft сохраняет transport grouping policy до
возможного `/collect` promotion.

## 2. Persisted explicit grouping

Public/domain policy:

```text
assembly_mode = explicit
commit_policy = explicit
```

Canonical persisted marker:

```text
InputGroupingMode.EXPLICIT_COLLECTION = "explicit_collection"
```

Explicit draft не получает `quiet_deadline`, `sealing_deadline` или
`maximum_deadline`. Transport debounce/recovery не auto-commit-ит его.

Rollout-era `grouping_mode="immediate_text"` остаётся читаемым. При startup,
inspect, send, cancel или bind reconciliation store:

```text
читает legacy draft
→ освобождает legacy group index
→ сохраняет тот же draft с explicit_collection
→ создаёт canonical group index
```

Batch ID, collection ID, source events, attachment/text parts и state не меняются.
Regression старого JSON входит в CI.

## 3. Shared ingress admission

`ExplicitCollectionIngressService` строит exact scope:

```text
session_id
client_type
client_instance_id
conversation_id
optional thread_id
principal_id
```

При active `COLLECTING` collection service:

1. удаляет client-supplied server metadata key;
2. добавляет authoritative collection ID после server-side lookup;
3. маршрутизирует event в canonical explicit grouping key;
4. связывает collection с exact `input_batch_id`;
5. при failure переводит collection в `FAILED`.

Text/file/semantic events входят в один draft. Attachment events сохраняют обычный
streaming ingestion pipeline.

Presentation params:

```json
{
  "assembly_mode": "explicit",
  "commit_policy": "explicit",
  "auto_commit_allowed": false,
  "collection_id": "icol_..."
}
```

## 4. Crash-safe promotion и recovery

`ExplicitInputDraftControlService`:

- promotion выполняет до сохранения idempotent action result;
- очищает transport deadlines;
- восстанавливает bind после crash;
- reconciles active collections до generic recovery;
- сохраняет open explicit draft только при authoritative active collection;
- orphan draft переводит в `ABANDONED`;
- terminal draft синхронизирует terminal collection state;
- legacy explicit marker переписывает в canonical mode.

Explicit draft принимает только:

```text
commit_reason = explicit_collection_commit
```

Transport commit reason получает conflict.

## 5. Authenticated HTTP control API

```text
POST /internal/input-collections/start
POST /internal/input-collections/inspect
POST /internal/input-collections/send
POST /internal/input-collections/cancel
```

Request fields не предоставляют authority. API key проверяется для requested
`client_type` и exact `client_instance_id`; start route обязан совпадать с exact
conversation/thread. Mutations имеют idempotency key.

`/send` durable-коммитит batch, но не запускает AgentCycle внутри control route.

## 6. Telegram adapter

Регистрируются только:

```text
/collect
/send
/cancel
```

`/batch` и `/done` отсутствуют. После handler применяется
`ApplicationHandlerStop`, поэтому command text не становится `InputBatch.text_parts`.

Adapter:

- вызывает shared controls с exact bot/chat/thread/principal scope;
- принимает explicit state только из authoritative presentation params;
- подавляет прежний transport `commit_and_run`;
- `/send` после durable commit отдельно вызывает `run_committed`;
- `/cancel` не запускает agent.

## 7. Validation

Итоговый CI head `c67e822a771a4a90de4bc25295ebd8a29717267d`:

```text
compile: success
artifact suite: 224 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Покрыты explicit text/files/mixed flows, promotion, recovery, spoof resistance,
HTTP authority, Telegram command isolation, canonical command set и legacy
`immediate_text → explicit_collection` rewrite.

Live Telegram gate остаётся maintainer acceptance step.

## 8. Дальнейшая граница

Presentation relocation и scoped artifact activation реализованы в BW-P3/BW-P4.
`CycleInbox` остаётся следующему `v0.4-input-runtime`.

Новых `.env` или `mcp.config` keys control plane и migration не добавляют.
