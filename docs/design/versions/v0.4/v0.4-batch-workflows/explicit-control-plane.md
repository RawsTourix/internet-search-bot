---
id: design.v0.4.batch-workflows.explicit-control-plane
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-01
---

# BW-16 — Explicit collection control plane

## 1. Каноническая граница

```text
/collect
→ active InputCollectionRecord exact scope
→ новые transport events входят в один explicit InputBatchDraft
→ /send закрывает admission и durable-коммитит batch
→ отдельный /run запускает AgentCycle

/cancel
→ terminal collection + exact draft cancellation
→ AgentCycle не запускается
```

Обычный AUTO input не изменён. Text-only input без active collection остаётся
немедленным, files-first AUTO draft сохраняет transport grouping policy до
возможного `/collect` promotion.

## 2. Две разные presentation-роли

Collection и AgentCycle не делят один Telegram status handle.

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
→ завершается через terminal delivery policy
```

### 2.1. Один authoritative collection presentation

Каждое обновление collection может зарезервировать новое presentation generation.
Telegram adapter соблюдает порядок:

```text
создать новый status
→ durable bind нового generation
→ остановить edits superseded status
→ удалить старое Telegram-сообщение best-effort
→ сохранить deletion receipt: deleted | failed | unknown
```

Quiet callback старого альбома не редактирует сохранённый в нём старый handle.
`ExplicitCollectionTelegramGatewayClient` возвращает exact
`presentation_message_id`, а adapter перенаправляет pending snapshot на текущий
authoritative handle.

После `/send` snapshot становится:

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

Ошибка редактирования historical snapshot не откатывает commit/cancel.

### 2.2. Execution-scoped progress overlay

`CommittedInputBatch.response_route` хранит durable reply provenance исходного
пакета. Run status создаётся позже, поэтому `/run` получает неперсистентный
`progress_metadata` overlay. Durable `CommittedInputBatch` не мутируется.

## 3. Persisted explicit grouping

Canonical persisted marker:

```text
InputGroupingMode.EXPLICIT_COLLECTION = "explicit_collection"
```

Explicit draft не получает transport quiet/deadline auto-commit semantics.
Rollout-era `grouping_mode="immediate_text"` мигрирует при reconciliation без
изменения batch ID, collection ID, частей или state.

## 4. Shared ingress admission

Exact scope:

```text
session_id
client_type
client_instance_id
conversation_id
optional thread_id
principal_id
```

При active `COLLECTING` collection service:

1. выполняет server-side exact-scope lookup;
2. reconciles terminal/expired state;
3. обновляет durable `updated_at`;
4. добавляет authoritative collection ID;
5. маршрутизирует event в один explicit grouping key;
6. связывает collection с exact `input_batch_id`.

Text/file/semantic events входят в один draft. Attachment events сохраняют
streaming ingestion pipeline.

### 4.1. Rejection одного события не ломает collection

Client/admission rejection относится к конкретному событию:

```text
attachment/text/byte limit
invalid event
conflict
→ rejected event
→ ранее сохранённые части остаются
→ collection остаётся COLLECTING
→ /send или /cancel продолжают работать
```

Collection переходит в `FAILED` только когда authoritative ingress result сам
получил terminal failed state, а не при любом exception вокруг отдельного member.
Это предотвращает опасный fallback:

```text
explicit member rejected
→ collection FAILED
→ остальные Telegram album members становятся AUTO media_group
→ unintended auto-run
```

### 4.2. `/send` — закрытая admission boundary

`mark_commit_requested()` выполняется до commit. После этого состояние
`COMMIT_REQUESTED` больше не принимает новые события.

Поздний Telegram update:

```text
COMMIT_REQUESTED / terminal collection
→ не попадает в AUTO/media_group
→ не создаёт второй InputBatch
→ не запускает второй AgentCycle
```

Если uploads, уже принятые до `/send`, ещё выполняются, commit ждёт их terminal
state. События, admitted после команды, не добавляются к package.

## 5. Несколько media groups и late-member tombstones

Один explicit InputBatch может включать несколько Telegram albums:

```text
input_batch_id → set[group_key]
```

Каждый quiet callback закрывает собственный exact `group_key`, независимо от
порядка завершения callbacks. `/send` и `/cancel` закрывают все оставшиеся группы.

После terminal callback album ID сохраняется в bounded transport tombstone.
Telegram adapter проверяет tombstone **до** создания status и HTTP ingress call.
Поздний member поэтому завершается молча и не может создать AUTO batch.

## 6. Idle expiry и restart recovery

Telegram session определяется chat/thread и остаётся той же после перезапуска
процесса. Поэтому restart сам по себе не означает новую session и не должен
физически удалять audit state.

При этом explicit collection не может резервировать scope бесконечно. Настройка:

```json
{
  "ingress": {
    "explicit_collection_idle_timeout_seconds": 3600
  }
}
```

Каждое успешно принятое событие и повторный `/collect` обновляют `updated_at`.
При превышении idle TTL startup/inspect/start reconciliation выполняет:

```text
active collection
+ bound open draft
→ draft = ABANDONED(explicit_collection_idle_timeout)
→ collection = ABANDONED(explicit_collection_idle_timeout)
→ scope index освобождён
→ следующий /collect создаёт новый collection
```

Файлы и metadata не удаляются немедленно: они остаются audit evidence и
очищаются общей retention policy.

`commit_requested_at` восстанавливается и завершается идемпотентно. Unknown
in-flight attachment не считается stored.

## 7. Один FIFO admission lane на Telegram session

Serialization выполняется один раз на входе `Application.process_update`:

```text
process_update(update)
→ synchronous enqueue по exact conversation/thread session key
→ один FIFO worker выполняет updates последовательно
```

В ту же lane входит internal media-group completion callback.

Инварианты:

- updates одной session admitted последовательно;
- разные sessions работают параллельно;
- callback не обгоняет `/send` или `/cancel`;
- durable collection state и transport tombstone остаются safety guards;
- dispatcher не является durable `CycleInbox`.

## 8. WAITING_USER continuation

`/collect` является упаковкой пользовательского продолжения, а не reset/new-task
boundary.

```text
suspended WAITING_USER cycle
+ /collect ... /send
→ продолжить тот же cycle
→ сохранить messages, working memory и artifact refs
→ добавить refs/text нового committed InputBatch
```

Добавления во время реально выполняющегося AgentCycle принадлежат будущему
`v0.4-input-runtime` / `CycleInbox`.

## 9. Authenticated HTTP API

```text
POST /internal/input-collections/start
POST /internal/input-collections/inspect
POST /internal/input-collections/send
POST /internal/input-collections/cancel
```

Request fields не предоставляют authority. API key проверяется для requested
transport и exact client instance. Mutations имеют idempotency key. `/send`
коммитит batch, но не запускает AgentCycle внутри control route.

## 10. `/status` diagnostics

`/status` показывает не только process counters, но и состояние exact session:

- stable session ID;
- runtime status, WAITING_USER, iterations и last error;
- active collection ID/state/InputBatch/counts;
- возраст collection и idle duration;
- количество открытых drafts по states;
- recoverable input presentations;
- recoverable output batches;
- включённость artifact lifecycle trace.

Каждый diagnostic subsection fail-soft: недоступный store не ломает всю команду.

## 11. Validation contracts

Regression suites покрывают:

- stale collection abandonment после restart;
- сохранение audit records и освобождение exact scope;
- event rejection на attachment limit без terminal collection failure;
- `/cancel` после rejection;
- exact callback group cleanup;
- late closed-album suppression до ingress/status;
- authoritative presentation redirect для pending callbacks;
- multi-group package counts и commit;
- terminal suppression после `/send` и `/cancel`.

Повторный live Telegram gate остаётся maintainer acceptance step.

## 12. Дальнейшая граница

Durable `CycleInbox`, additions в running cycle и restart-safe execution queue
остаются следующему `v0.4-input-runtime`. Idle TTL и explicit collection
reconciliation уже transport-neutral и не требуют изменения этой будущей
границы.
