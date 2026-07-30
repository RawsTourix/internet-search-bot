---
id: design.v0.4.batch-workflows.presentation-controls
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-5–BW-6 — Presentation relocation and user controls

## BW-5. Durable presentation relocation

### BW-5.1. Проблема

Один стабильный status/progress message удобен для редактирования, restart recovery
и terminal fallback. Но если пользователь добавил новое сообщение в тот же
`InputBatchDraft`, прежний status остаётся выше новой части и перестаёт быть
актуальным центром workflow.

Удалять старое сообщение до создания и durable bind нового нельзя: transport
ошибка оставит batch без presentation handle.

### BW-5.2. Persisted generation model

`InputBatchPresentationRef` schema v2 хранит:

```text
presentation_generation: int
client_message_id: str | None          # active compatibility field
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

### BW-5.4. Безопасный протокол

```text
1. Server reserve relocation generation N+1 и выдаёт одноразовый token.
2. Telegram отправляет новый status после последнего user message.
3. Telegram получает new client_message_id.
4. Atomic durable bind:
   - generation N+1 становится active;
   - new ID становится client_message_id;
   - old generation переносится в superseded_handles.
5. Только после успешного bind прекращаются edits old generation.
6. Telegram best-effort удаляет old message.
7. deletion receipt сохраняет deleted / failed / unknown.
```

Если создание нового status не удалось, old handle остаётся active.

Если новый status создан, но durable bind не удался:

```text
old handle остаётся active
new unbound message удаляется best-effort
```

Если bind выполнен, но удалить old message не удалось:

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
- принимать stale `expected_generation`.

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
presentation state machine. Другие adapters смогут использовать тот же протокол,
когда объявят эквивалентный ordered-message capability.

### BW-5.7. Terminal behavior

После terminal state:

- relocation не резервируется;
- current active handle остаётся единственным writable status handle;
- superseded handles не меняются, кроме idempotent deletion receipt;
- final answer и OutputBatch delivery используют существующую terminal policy.

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

### BW-6.2. Локализованные сообщения

#### Collection started

```text
📦 Режим сбора включён.

Все следующие сообщения и файлы войдут в один пакет.
Сообщений: 0
Файлов: 0

/send — отправить пакет
/cancel — отменить
```

#### Collecting

```text
📦 Пакет собирается.

Сообщений: {text_count}
Файлов: {artifact_count}
Загрузка: {stored_count}/{attachment_count}

/send — отправить
/cancel — отменить
```

#### Empty send

```text
Пакет пока пуст. Отправьте сообщение или файл либо используйте /cancel.
```

#### Cancelled

```text
Сбор пакета отменён. Агент не запускался.
```

#### No active draft

```text
Сейчас нет собираемого пакета.
```

Все строки находятся в ru/en localization catalogs, а не внутри Telegram handler.

### BW-6.3. Command semantics

| Команда | Нет draft | AUTO draft | EXPLICIT draft |
|---|---|---|---|
| `/collect` | Создать EXPLICIT | Upgrade AUTO → EXPLICIT | Idempotent inspect |
| `/send` | No active draft | Explicit commit intent | Commit + separate run |
| `/cancel` | Idempotent no-op | Cancel | Cancel |
| `/status` | Runtime status | Runtime + draft summary | Runtime + draft summary |
| `/reset` | Clear dialog context | Cancel + clear context | Cancel + clear context |

`/send` и `/cancel` являются control actions и не входят в `text_parts`.

### BW-6.4. Permission and scope

В private chat sender совпадает с principal scope.

В group/topic command действует только на draft:

- того же client instance;
- того же conversation/thread;
- того же sender, если collaborative collection не включена явно.

Другой участник не может commit/cancel чужой draft только потому, что знает о его
существовании.

## Validation

CI head `a9e82a3c5bcef2b1d694d19642200605f0726356`:

```text
compile: success
artifact suite: 219 tests, OK
storage suite: success
plans suite: success
planning suite: success
API suite: success
```

Regressions покрывают:

- schema-v1 read upgrade;
- initial bind generation 1;
- reserve/bind generation 2;
- superseded audit record;
- stale generation rejection;
- pending relocation suppression;
- create → bind → delete order;
- bind failure без удаления active old handle;
- failed/unknown old deletion без rollback;
- canonical `/collect`, `/send`, `/cancel` only.

Новых `.env` или `mcp.config` keys BW-P3 не добавляет.
