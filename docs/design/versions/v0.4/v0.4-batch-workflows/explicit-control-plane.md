---
id: design.v0.4.batch-workflows.explicit-control-plane
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-16 — Explicit collection control plane

## 1. Каноническая граница

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

## 2. Две разные presentation-роли

Collection и AgentCycle больше не делят один Telegram status handle.

```text
collection snapshot
→ показывает состав собираемого пакета
→ принадлежит InputCollection/InputBatchDraft
→ обновляется во время сбора
→ после /send или /cancel становится terminal snapshot
→ не удаляется ради запуска AgentCycle

run status
→ создаётся непосредственно под /send
→ принадлежит только одному /run invocation
→ принимает cycle/tool/final progress
→ завершается через обычную terminal delivery policy
```

Это разделение является transport-neutral правилом. Adapter может отображать роли
по-разному, но не должен превращать collection presentation в execution
presentation.

### 2.1. Terminal collection snapshot

После `/send` прежнее сообщение состава пакета остаётся в истории и редактируется в:

```text
📦 Пакет передан в обработку.

Файлы: N
Сообщения: M
```

После `/cancel`:

```text
📦 Сбор пакета отменён.

Файлы: N
Сообщения: M
```

Ошибка редактирования этого исторического snapshot не откатывает уже
authoritative commit/cancel. Удалять snapshot при terminal transition запрещено.

### 2.2. Execution-scoped progress overlay

`CommittedInputBatch.response_route` хранит durable reply provenance исходного
пакета. Он не обязан указывать на status, созданный позднее командой `/send`.

Поэтому `/run` принимает неперсистентный overlay:

```json
{
  "session_id": "telegram:conversation:...",
  "progress_locale": "ru",
  "progress_metadata": {
    "progress_callback_url": "...",
    "progress_target": {
      "chat_id": 123,
      "message_id": 456
    },
    "status_message_id": 456,
    "progress_request_id": "ibat_..."
  }
}
```

Gateway создаёт in-memory copy response route для callback factory. Durable
`CommittedInputBatch` не изменяется. Значит, correctness больше не зависит от
redirect chain между старыми presentation generations.

## 3. Persisted explicit grouping

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

## 4. Shared ingress admission

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

## 5. Crash-safe promotion и recovery

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

## 6. Один FIFO admission lane на Telegram session

Набор `asyncio.Lock` вокруг отдельных handlers обеспечивает mutual exclusion, но
не гарантирует порядок уже созданных asyncio tasks. Поэтому serialization
выполняется один раз на входе `Application.process_update`.

```text
process_update(update)
→ synchronous enqueue по exact conversation/thread session key
→ один FIFO worker выполняет updates последовательно
```

В ту же lane входит internal media-group completion callback.

Инварианты:

- порядок admission соответствует порядку вызовов `process_update`;
- внутри одной session одновременно не выполняются два ingress/run workflow;
- разные session продолжают работать параллельно;
- late album completion не обгоняет `/send` или `/cancel`;
- terminal explicit-batch tombstone остаётся последним safety guard;
- dispatcher не является durable `CycleInbox` и не переживает process restart.

## 7. Fresh task boundary

Обычное сообщение после `WAITING_USER` по-прежнему продолжает pending AgentCycle.
Явный `/collect` имеет другую семантику: пользователь открыл новый пакет задачи.

При успешном `STARTED` или `PROMOTED_AUTO_DRAFT`:

```text
pending WAITING_USER cycle
→ trace: pending_cycle_abandoned
→ session.pending_cycle = None
→ будущий /send запускает новый AgentCycle
```

`ALREADY_ACTIVE` не сбрасывает pending cycle повторно. Полноценное добавление новых
committed batches в активный цикл остаётся обязанностью `v0.4-input-runtime` и
`CycleInbox`.

## 8. Authenticated HTTP control API

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

## 9. Telegram adapter

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
- передаёт execution-scoped progress metadata exact run status;
- terminalizes collection snapshot без удаления;
- `/cancel` не запускает agent.

## 10. Validation

CI head `5d9edb4b1e3eafb7b3ad58deab920e147d5bb8e0`:

```text
compile: success
artifact suite: 238 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Новые regressions покрывают:

- execution-scoped progress overlay без мутации durable response route;
- exact progress target в Telegram `/run` request;
- persistent terminal collection snapshot;
- отсутствие snapshot mutation при empty `/send`;
- FIFO same-session ordering;
- concurrency разных Telegram sessions;
- nested same-lane callback без deadlock;
- fresh `/collect` boundary для `WAITING_USER`;
- no-op boundary при отсутствии pending cycle;
- late media-group suppression после `/send` и `/cancel`.

Повторный live Telegram gate остаётся maintainer acceptance step.

## 11. Дальнейшая граница

Presentation relocation внутри ещё активного collection и scoped artifact
activation реализованы в BW-P3/BW-P4. Durable `CycleInbox`, active-cycle additions
и restart-safe execution queue остаются следующему `v0.4-input-runtime`.

Новых `.env` или `mcp.config` keys этот refactor не добавляет.
