---
id: design.v0.4.batch-workflows.explicit-control-plane
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-16 — Explicit collection control plane

## 1. Реализованная граница

BW-P2B завершает transport-neutral explicit collection workflow поверх
`InputCollectionRecord` и `InputBatchDraft`:

```text
/collect | /batch
→ active InputCollectionRecord exact scope
→ новые transport events входят в один explicit InputBatchDraft
→ /send | /done durable-коммитит batch
→ отдельный /run запускает AgentCycle

/cancel
→ terminal collection + exact draft cancellation
→ AgentCycle не запускается
```

Обычный AUTO input не изменён. Text-only input без active collection остаётся
немедленным, files-first AUTO draft сохраняет прежнюю transport grouping policy.

## 2. Persisted explicit grouping

Public/domain policy остаётся явной:

```text
assembly_mode = explicit
commit_policy = explicit
```

Explicit draft не получает `quiet_deadline`, `sealing_deadline` или
`maximum_deadline`. Transport debounce и startup recovery не имеют права
автоматически его коммитить.

Текущая schema-v2 реализация временно использует ранее зарезервированный и не
использовавшийся persisted slot `InputGroupingMode.IMMEDIATE_TEXT` как внутренний
маркер explicit collection. Это compatibility detail, не попадающая в public
contracts. Перед integration/release gate требуется отдельная structural migration
на именованный persisted mode `EXPLICIT_COLLECTION` с чтением промежуточных
records и regression test старого JSON.

## 3. Shared ingress admission

`ExplicitCollectionIngressService` перед каждым submit строит exact
`InputDraftScope`:

```text
session_id
client_type
client_instance_id
conversation_id
optional thread_id
principal_id
```

Если scope имеет active collection в состоянии `COLLECTING`, service:

1. очищает возможный client-supplied server metadata key;
2. добавляет authoritative collection ID только после server-side lookup;
3. маршрутизирует event в explicit grouping key;
4. после reservation связывает collection с exact `input_batch_id`;
5. при failure переводит collection в terminal `FAILED`.

Первый реальный event создаёт draft. Следующие text/file/semantic events входят в
тот же batch. Attachment events проходят обычный streaming ingestion path;
text-only event может использовать exact append path.

Presentation params содержат transport-neutral admission signal:

```json
{
  "assembly_mode": "explicit",
  "commit_policy": "explicit",
  "auto_commit_allowed": false,
  "collection_id": "icol_..."
}
```

Client adapter не определяет explicit mode по локальному Telegram state или
filename.

## 4. Crash-safe promotion и recovery

`ExplicitInputDraftControlService`:

- promotion AUTO files-first draft выполняет до сохранения idempotent action result;
- очищает transport deadlines и переносит draft на collection grouping key;
- восстанавливает bind после crash между draft mutation и collection bind;
- при startup reconciles active collections до generic ingress recovery;
- сохраняет open explicit draft только при наличии authoritative active collection;
- orphan explicit draft переводится в `ABANDONED` с audit evidence;
- terminal draft синхронизирует terminal state collection.

Explicit draft принимает только commit reason:

```text
explicit_collection_commit
```

Transport commit reason получает conflict и не публикует batch.

## 5. Authenticated HTTP control API

Shared Gateway router:

```text
POST /internal/input-collections/start
POST /internal/input-collections/inspect
POST /internal/input-collections/send
POST /internal/input-collections/cancel
```

Request scope не является authority. API key должен быть авторизован одновременно
для:

- requested `client_type`;
- exact `client_instance_id` либо разрешённого wildcard;
- response route того же conversation/thread для `start`.

Mutating actions используют explicit idempotency key. Domain conflicts возвращают
409, missing objects — 404, storage/integrity failures — 503.

`/send` выполняет durable commit, но не запускает AgentCycle внутри control route.

## 6. Telegram adapter

Canonical Telegram composition регистрирует команды с priority group `-1`:

```text
/collect, /batch
/send, /done
/cancel
```

После command handler выбрасывает `ApplicationHandlerStop`, поэтому legacy generic
command bridge не превращает command text в `InputBatch.text_parts`.

`ExplicitCollectionTelegramGatewayClient`:

- вызывает shared HTTP controls с exact bot instance/chat/thread/principal scope;
- запоминает explicit batch только по authoritative presentation params;
- перехватывает прежний transport `commit_and_run` и возвращает
  `input_collection_pending` без commit/run;
- `/send` durable-коммитит collection, затем command handler отдельно вызывает
  `run_committed`;
- `/cancel` очищает local grouping handle и не запускает agent;
- команды и статусы локализованы для RU/EN.

## 7. Validation evidence

CI head `4fef809546a64c20b19308cada110b285d5a1a17`:

```text
compile: success
artifact suite: 210 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Focused regressions покрывают:

- text-only, files-only и mixed explicit collection;
- promotion files-first AUTO draft;
- отсутствие quiet/deadline semantics;
- transport auto-commit rejection;
- startup preservation и orphan abandonment;
- client spoofing server-owned collection metadata;
- HTTP transport/client-instance authority;
- Telegram suppression преждевременного commit/run;
- `/collect`, `/send`, `/cancel` и aliases;
- `/send → durable commit → explicit run`;
- command isolation через `ApplicationHandlerStop`.

Live Telegram gate для команд ещё не выполнен и остаётся частью BW-P5.

## 8. Следующая граница

Следующий feature slice:

```text
BW-P3 — presentation generations and safe relocation
```

BW-P2B не реализует relocation status message, artifact access scopes или
`CycleInbox`.