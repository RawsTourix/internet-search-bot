---
id: design.v0.4.batch-workflows.presentation-controls
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-5–BW-6 — Collection presentation and user controls

## BW-5. Collection presentation relocation

### BW-5.1. Scope

Relocation относится только к ещё активному `InputBatchDraft`/collection. Её цель —
переместить актуальный snapshot состава пакета ниже новых user parts, пока пакет
продолжает собираться.

Relocation не является механизмом выбора AgentCycle progress target после `/send`.
Collection snapshot и execution status имеют разные роли и lifecycle.

### BW-5.2. Persisted generation model

`InputBatchPresentationRef` schema v2 хранит:

```text
presentation_generation: int
client_message_id: str | None
anchor_source_message_id: str | None
superseded_handles: list[SupersededPresentationHandle]
pending_relocation_token_hash: str | None
pending_relocation_generation: int | None
pending_anchor_source_message_id: str | None
```

```python
class SupersededPresentationHandle(BaseModel):
    client_message_id: str
    generation: int
    superseded_at: datetime
    deletion_state: Literal[
        "not_requested",
        "deleted",
        "failed",
        "unknown",
    ]
```

Только `client_message_id` текущего `presentation_generation` является writable.
Superseded handles сохраняются как audit evidence и после restart не возвращаются
в active state.

Старые schema-v1 records читаются structural upgrade-слоем:

```text
bound schema v1 → generation 1
reserved schema v1 → generation 0
superseded_handles → []
```

### BW-5.3. Trigger

Relocation выполняется, если одновременно истинно:

```text
batch state = collecting
+ presentation state = bound
+ pending relocation отсутствует
+ принят новый user-visible response anchor
+ anchor расположен после active status в client order
+ adapter умеет безопасно создать новый handle
```

В текущей v0.4-реализации client-order trigger включён только для Telegram:

```text
client_binding_id starts with telegram:
+ active client_message_id является integer
+ anchor client_message_id является integer
+ anchor ID > active status ID
```

Webhook processing time, filename, текст инструкции и смысл сообщения не
используются. Duplicate/internal transport events сами по себе relocation не
вызывают.

### BW-5.4. Безопасный active-collection протокол

```text
1. Server reserve relocation generation N+1 и выдаёт одноразовый token.
2. Telegram отправляет новый collection snapshot после последнего user message.
3. Telegram получает new client_message_id.
4. Atomic durable bind:
   - generation N+1 становится active;
   - new ID становится client_message_id;
   - old generation переносится в superseded_handles.
5. Только после успешного bind прекращаются edits old generation.
6. Telegram best-effort удаляет old active-collection snapshot.
7. deletion receipt сохраняет deleted / failed / unknown.
```

Если создание нового snapshot не удалось, old handle остаётся active.

Если новый snapshot создан, но durable bind не удался:

```text
old handle остаётся active
new unbound message удаляется best-effort
```

Если bind выполнен, но удалить old snapshot не удалось:

```text
new handle остаётся authoritative
old message не изменяется
old generation никогда больше не редактируется
```

Запрещено:

- сначала удалять old message;
- редактировать old message текстом «перенесено ниже» после failed deletion;
- считать relocation успешным без durable bind;
- откатываться к superseded generation после restart;
- принимать stale `expected_generation`;
- использовать relocation для передачи authority execution status.

### BW-5.5. Concurrency и idempotency

Relocation использует generation compare-and-set:

```text
reserve expected_generation=N
→ pending_generation=N+1
→ bind допускается только для N и exact pending token
```

Stale caller не может перезаписать новый active handle. Пока один transport caller
владеет pending relocation, последующие ingress updates получают `SILENT`, а не
создают параллельное поколение и не редактируют старое сообщение.

После bind новая user part может инициировать следующее поколение обычным путём.
Correctness не зависит от debounce/coalescing timer.

### BW-5.6. HTTP и adapter boundary

Shared authenticated routes:

```text
POST /internal/input-presentations/{presentation_id}/relocate
POST /internal/input-presentations/{presentation_id}/superseded-deletion
```

Authority проверяет:

- transport scope API key;
- session ownership exact `InputBatchDraft`;
- presentation token;
- expected generation.

Telegram adapter выполняет transport create/delete, а shared store владеет
collection presentation state machine. Другие adapters смогут использовать тот же
протокол, когда объявят эквивалентный ordered-message capability.

### BW-5.7. Terminal collection behavior

После `/send` или `/cancel` relocation больше не резервируется.

Текущий active collection snapshot:

- перестаёт принимать progress updates;
- остаётся в Telegram history;
- best-effort редактируется в terminal summary с final file/text counts;
- не удаляется ради запуска AgentCycle;
- не становится execution progress target.

Ошибка terminal edit не меняет authoritative commit/cancel state.

## BW-5A. Execution presentation

### BW-5A.1. Отдельный status под `/send`

`/send` создаёт processing status непосредственно под командой. Этот status
принадлежит exact `/run` invocation и получает:

```text
cycle_started / cycle_resumed
tool progress
final processing
result_ready
terminal delivery state
```

### BW-5A.2. Non-persisted run overlay

`CommittedInputBatch.response_route` остаётся durable provenance исходного пакета.
Для запуска Telegram передаёт `/run` отдельный `progress_metadata` overlay с exact
processing message ID.

Gateway применяет overlay только к in-memory batch copy для callback factory.
Durable InputBatch не изменяется. Redirect registry между presentation generations
не используется и удалён.

## BW-6. User-friendly controls

### BW-6.1. Единственные канонические команды

```text
/collect — начать или показать сбор пакета
/send    — завершить сбор и запустить обработку
/cancel  — отменить текущий пакет
```

`/batch` и `/done` не являются aliases и не регистрируются. Одинаковая функция не
должна иметь несколько названий без отдельной пользовательской семантики.

Команды присутствуют в Telegram command menu и обрабатываются до legacy command
bridge. После handler применяется `ApplicationHandlerStop`, поэтому command text
не становится `InputBatch.text_parts`.

### BW-6.2. Локализованные состояния

#### Collection started

```text
📦 Режим сбора включён.

Все следующие сообщения и файлы войдут в один пакет.

Файлы: 0
Сообщения: 0

/send — отправить пакет
/cancel — отменить
```

#### Collecting

```text
📦 Пакет собирается.

Файлы: {file_count}
Сообщения: {text_part_count}

/send — отправить пакет
/cancel — отменить
```

#### Submitted snapshot

```text
📦 Пакет передан в обработку.

Файлы: {file_count}
Сообщения: {text_part_count}
```

#### Cancelled snapshot

```text
📦 Сбор пакета отменён.

Файлы: {file_count}
Сообщения: {text_part_count}
```

#### Empty send

```text
Пакет пока пуст. Отправьте сообщение или файл либо используйте /cancel.
```

Все строки находятся в ru/en localization catalogs, а не внутри Telegram handler.

### BW-6.3. Command semantics

| Команда | Нет draft | AUTO draft | EXPLICIT draft |
|---|---|---|---|
| `/collect` | Создать EXPLICIT + fresh-task boundary | Upgrade AUTO → EXPLICIT + boundary | Idempotent inspect |
| `/send` | No active draft | Explicit commit intent | Commit + separate run |
| `/cancel` | Idempotent no-op | Cancel | Cancel |
| `/status` | Runtime status | Runtime + draft summary | Runtime + draft summary |
| `/reset` | Clear dialog context | Cancel + clear context | Cancel + clear context |

`/send` и `/cancel` являются control actions и не входят в `text_parts`.

### BW-6.4. Ordering boundary

Telegram updates enqueue-ятся в один FIFO dispatcher exact conversation/thread до
создания handler task. Это относится к commands, text, attachments и album
completion. Разные sessions остаются параллельными.

### BW-6.5. Permission and scope

В private chat sender совпадает с principal scope.

В group/topic command действует только на draft:

- того же client instance;
- того же conversation/thread;
- того же sender, если collaborative collection не включена явно.

Другой участник не может commit/cancel чужой draft только потому, что знает о его
существовании.

## Validation

CI head `5d9edb4b1e3eafb7b3ad58deab920e147d5bb8e0`:

```text
compile: success
artifact suite: 238 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Regressions покрывают active-collection relocation, persistent terminal snapshot,
run-scoped progress target, durable route immutability, FIFO dispatcher, fresh-task
boundary и canonical `/collect`, `/send`, `/cancel` only.

Новых `.env` или `mcp.config` keys refactor не добавляет.
