---
id: design.v0.4.batch-workflows.presentation-controls
version: v0.4
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-07-29
---

# BW-5–BW-6 — Presentation relocation and user controls

## BW-5. Durable presentation relocation

### BW-5.1. Проблема

Один стабильный status/progress message удобен для редактирования, restart
recovery и terminal fallback. Но если пользователь добавил новое сообщение в тот
же `InputBatchDraft`, прежний status остаётся выше новой части и визуально
перестаёт быть актуальным центром workflow.

Нельзя решать это удалением старого сообщения до создания нового: transport
ошибка оставит batch без presentation handle.

### BW-5.2. Presentation generation

Durable input presentation получает:

```text
presentation_generation: int
active_client_message_id: str | None
anchor_source_message_id: str | None
superseded_handles: list[SupersededPresentationHandle]
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

Только handle текущего `presentation_generation` является writable.

Progress event обязан содержать или резолвить authoritative generation. Event
для старого generation не редактирует superseded message.

### BW-5.3. Когда требуется relocation

Relocation выполняется, если одновременно истинно:

```text
batch не terminal
+ существует active presentation handle
+ в draft принят новый user-visible source message
+ новое source message расположено после текущего status в client order
+ transport capability поддерживает создание нового status handle
```

Для Telegram client order определяется integer `message_id` exact chat/thread.
Webhook processing time не заменяет client order.

File members одного Telegram album могут иметь несколько message IDs. Anchor
для relocation — максимальный accepted user message ID, относящийся к draft.

Internal retries, duplicates и transport-only events relocation не вызывают.

### BW-5.4. Безопасный протокол

```text
1. Render актуальный status для нового generation.
2. Отправить новое status message после последнего user message.
3. Получить подтверждённый new client_message_id.
4. Atomic durable bind:
   - generation += 1
   - active_client_message_id = new ID
   - old handle → superseded
5. Только после успешного bind best-effort удалить old message.
6. Если delete не удался:
   - сохранить deletion_state=failed/unknown;
   - оставить старое сообщение без изменений;
   - никогда больше его не редактировать.
```

Запрещено:

- сначала удалять старое сообщение;
- редактировать старое текстом «перенесено ниже» после failed deletion;
- считать relocation успешным без durable bind;
- возвращаться к старому handle после restart, если новый bind опубликован.

### BW-5.5. Concurrency

Relocation сериализуется по exact `input_batch_id`.

Если во время отправки нового status пришла ещё одна user part:

```text
relocation generation N in flight
→ принять part durably
→ после bind проверить latest anchor
→ при необходимости запланировать generation N+1
```

Coalescing допустим как оптимизация: несколько частей, пришедших в коротком
окне, могут вызвать один relocation к последнему anchor. Но correctness не
зависит от таймера coalescing.

### BW-5.6. Restart recovery

После restart:

- durable active handle остаётся authoritative;
- partially created, но не bound message не считается active;
- bound new handle с неуспешным delete старого остаётся active;
- superseded handles не восстанавливаются как writable;
- для restored explicit draft без usable active handle создаётся новый
  generation.

### BW-5.7. Terminal behavior

После terminal state:

- final status/answer использует current active handle либо OutputBatch policy;
- relocation больше не выполняется;
- superseded handles не меняются;
- terminal send timeout может редактировать только current active handle.

## BW-6. User-friendly controls

### BW-6.1. Канонические команды

```text
/collect — начать или продолжить сбор пакета
/send    — отправить собранный пакет агенту
/cancel  — отменить сбор пакета
```

Aliases:

```text
/batch → /collect
/done  → /send
```

Публичное меню Telegram и `/help` показывают основные команды. Aliases не
обязаны отображаться.

### BW-6.2. Inline controls

Если client capabilities поддерживают buttons, collection presentation содержит:

```text
[Отправить пакет] [Отменить]
```

Callback actions используют opaque server-issued action tokens и проходят тот же
`InputDraftControlService`, что slash-команды.

Callback data не содержит filesystem path, session secret или authoritative
state mutation payload.

### BW-6.3. Локализованные сообщения

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

#### AUTO attachment draft awaiting user

```text
📎 Файлы приняты: {artifact_count}.
Отправьте задачу для этого пакета или используйте /send для обработки без
отдельной инструкции.
```

#### Commit requested

```text
📦 Завершаю приём пакета…
Файлы: {artifact_count}
Сообщения: {text_count}
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

#### Conflict

```text
Уже собирается другой пакет в этом чате.
Завершите его командой /send или отмените командой /cancel.
```

Все строки находятся в ru/en localization catalogs, а не внутри Telegram
handler.

### BW-6.4. Command semantics

| Команда | Нет draft | AUTO draft | EXPLICIT draft |
|---|---|---|---|
| `/collect` | Создать EXPLICIT | Upgrade AUTO → EXPLICIT | Idempotent inspect |
| `/send` | No active draft | Explicit commit intent | Commit |
| `/cancel` | Idempotent no-op | Cancel | Cancel |
| `/status` | Runtime status | Runtime + draft summary | Runtime + draft summary |
| `/reset` | Clear dialog context | Cancel + clear context | Cancel + clear context |

`/send` и `/cancel` являются control actions и не входят в `text_parts`.

### BW-6.5. Permission and scope

В private chat sender совпадает с principal scope.

В group/topic command действует только на draft:

- того же client instance;
- того же conversation/thread;
- того же sender, если collaborative collection не включена явно.

Другой участник не может commit/cancel чужой draft только потому, что знает его
существование.

### BW-6.6. UX после `/reset`

Ответ обязан явно сообщать границу операции:

```text
✅ Контекст диалога очищен.
Незавершённых пакетов отменено: {count}.
Сохранённые файлы и история артефактов не удалены.
```

Если `count=0`, строка о пакетах может быть опущена, но сохранение workspace
должно оставаться понятным пользователю через `/help` и документацию.
