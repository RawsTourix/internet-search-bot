---
id: design.v0.4.batch-workflows.output-grouping
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-29
---

# BW-7–BW-8 — Output grouping and Telegram media upload

## BW-7. Stable semantic grouping

### BW-7.1. Source order is authoritative

`OutputBatch.parts[*].index` является единственным источником порядка.

Grouping не может:

- сортировать по filename, MIME, creation time или type;
- переносить совместимые parts через text/interactive boundary;
- объединять одинаковые типы из разных непрерывных участков;
- менять порядок частей внутри group.

Алгоритм работает одним проходом слева направо и строит группы только из
непрерывных совместимых частей.

Пример:

```text
0 Text
1 Document A
2 Document B
3 Photo C
4 Video D
5 Document E
6 Text
7 Document F
```

Результат:

```text
0 TEXT [0]
1 DOCUMENT_GROUP [1, 2]
2 VISUAL_GROUP [3, 4]
3 DOCUMENT [5]
4 TEXT [6]
5 DOCUMENT [7]
```

`A, B, E, F` нельзя объединить в один album: это изменит композицию ответа.

### BW-7.2. Delivery class

Renderer определяет transport-independent delivery class:

```python
class OutputDeliveryClass(str, Enum):
    TEXT = "text"
    DOCUMENT = "document"
    AUDIO = "audio"
    VISUAL_ALBUM = "visual_album"
    ANIMATION = "animation"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"
    INTERACTIVE = "interactive"
```

Приоритет определения:

```text
1. Exact semantic OutputPart subtype.
2. Server-owned semantic delivery intent attached to exact artifact_id.
3. Authoritative detected MIME/format metadata if policy однозначна.
4. Safe DOCUMENT fallback.
```

Filename extension не является самостоятельным source of truth.

`ArtifactOutputPart` с точными исходными bytes по умолчанию отправляется как
DOCUMENT. Даже image content не превращается автоматически в PHOTO, поскольку
Telegram может преобразовать изображение. Для gallery UX renderer создаёт
явный `PhotoOutputPart`/visual intent.

### BW-7.3. Compatibility matrix

| Delivery class | Groupable | Compatible with | Telegram operation |
|---|---:|---|---|
| DOCUMENT | Да | DOCUMENT | `sendMediaGroup(InputMediaDocument[])` |
| AUDIO | Да | AUDIO | `sendMediaGroup(InputMediaAudio[])` |
| VISUAL_ALBUM | Да | PHOTO/VIDEO и поддерживаемые visual media | `sendMediaGroup(...)` |
| TEXT | Нет | — | send/edit text |
| ANIMATION | Нет в текущем contract | — | send animation |
| VOICE | Нет | — | send voice |
| VIDEO_NOTE | Нет | — | send video note |
| STICKER | Нет | — | send sticker |
| LOCATION | Нет | — | send location |
| CONTACT | Нет | — | send contact |
| INTERACTIVE | Нет | — | capability-specific operation |

Exact compatibility определяется capability snapshot конкретного client
instance. Таблица задаёт максимальную семантическую возможность, а не обещает
поддержку старого Telegram/Web client.

### BW-7.4. Group size and partition

Telegram media group:

```text
minimum items = 2
maximum items = capability.output.group.<class>.max_items
```

Для стандартного Bot API renderer ожидает максимум 10, но runtime читает лимит
из capability snapshot.

Непрерывный compatible run разбивается стабильно:

```text
1 item       → single operation
2..max       → one group
max+1..2max  → first max, then remainder
remainder=1  → final single operation
```

### BW-7.5. Captions and text composition

Group caption может принадлежать только конкретному semantic part согласно
OutputPart model. Renderer не переносит произвольный final text внутрь caption
первого файла только ради уменьшения числа сообщений.

Если product policy явно разрешает group caption:

- выбирается exact designated caption part;
- длина и entities валидируются по client capability;
- исходный text part не дублируется отдельным сообщением;
- receipt сохраняет связь caption intent с client message ID.

В текущем v0.4 safe default — final text отдельным OutputPart перед файлами.

## BW-8. Telegram multipart upload

### BW-8.1. Почему group отличается от single document

`sendDocument` передаёт один upload parameter.

`sendMediaGroup` передаёт JSON-массив `media`. Бинарные bytes не могут находиться
в JSON, поэтому каждый media element использует URI:

```text
attach://<multipart_field_name>
```

Тот же HTTP multipart request обязан содержать file part с точно таким field
name.

Упрощённый wire contract:

```text
media=[
  {"type":"document","media":"attach://attachedA"},
  {"type":"document","media":"attach://attachedB"}
]

attachedA=@a.csv
attachedB=@b.json
```

`attach://...` — не внешний URL и не durable ID. Это локальная ссылка внутри
одного multipart request.

### BW-8.2. SDK ownership

`python-telegram-bot` создаёт корректный `InputFile(attach=True)` автоматически,
когда `InputMediaDocument` получает raw bytes или file object.

Предпочтительный streaming path:

```python
InputMediaDocument(
    media=spool,
    filename=filename,
)
```

Bounded eager path:

```python
InputMediaDocument(
    media=payload_bytes,
    filename=filename,
)
```

SDK:

```text
raw input
→ parse_file_input(..., attach=True)
→ unique InputFile.attach_name
→ JSON attach:// URI
→ matching multipart field
```

Runtime не должен заранее передавать `InputFile` без `attach=True`: уже готовый
`InputFile` SDK не перепарсит, и JSON может не получить matching `attach://`
mapping.

Низкоуровневый допустимый вариант:

```python
InputFile(
    spool,
    filename=filename,
    attach=True,
    read_file_handle=False,
)
```

Но он используется только при документированной необходимости. Safe default —
передать raw file handle/bytes в `InputMedia*` и оставить mapping SDK.

### BW-8.3. File lifetime

При streaming upload все file handles остаются открытыми до завершения
`send_media_group`.

```text
open exact claimed delivery bytes
→ validate size/hash/filename
→ build InputMedia items
→ await send_media_group
→ parse receipts
→ close handles
```

Нельзя создать `InputFile(read_file_handle=False)` внутри `with`, закрыть handle
и затем отправить request.

### BW-8.4. Exact bytes authority

Каждый media item открывается через OutputBatch-scoped claimed delivery route:

- exact `output_batch_id`;
- exact `delivery_id`;
- exact session/client instance authority;
- declared content length;
- SHA-256 verification;
- sanitized exact filename.

Agent Runtime не получает bot token, multipart field names, download URLs или
file handles.

### BW-8.5. Retry and fallback

Transport decision tree:

```text
build request before send fails
→ FAILED, safe retry/fallback allowed

Telegram confirmed BadRequest
→ known unsent
→ optional one corrected representation/reply retry
→ then stable individual fallback

TimedOut/NetworkError after send started
→ UNKNOWN
→ no automatic duplicate resend

response item count/order mismatch
→ UNKNOWN per affected part
```

Representation retry допускается только если он исправляет конкретный known
contract difference. После перехода на SDK-owned attach mapping eager retry не
должен оставаться бессодержательным повтором того же неправильного mapping.

Individual fallback:

- сохраняет part order;
- reply metadata используется только для первого confirmed send attempt;
- создаёт exact part receipts;
- не повторяет уже possibly delivered UNKNOWN group.

### BW-8.6. Receipts

Успешный group возвращает столько client messages, сколько parts было отправлено.
Для каждого part сохраняются:

```text
part_id
index
delivery_id
client_message_id
state=delivered
artifact_content_state=delivered
```

Несовпадение количества или порядка не считается успехом всего batch.

### BW-8.7. Smart planner tests

Минимальные regression scenarios:

1. Два documents → один document group.
2. Raw file handles создают уникальные non-empty `attach_uri`.
3. Multipart fields совпадают с `attach://` names.
4. Handles открыты во время request и закрыты после.
5. Document run из 11 частей → 10 + 1 без reorder.
6. Text между documents разделяет groups.
7. Photo/video adjacent run формирует visual group при capability support.
8. Generic image artifact остаётся document без semantic photo intent.
9. Confirmed BadRequest → safe ordered fallback.
10. Timeout after send start → UNKNOWN без fallback duplicate.

### BW-8.8. Official references

- Telegram Bot API, `sendMediaGroup` and `InputMedia*`:
  `https://core.telegram.org/bots/api`
- python-telegram-bot `InputFile`:
  `https://docs.python-telegram-bot.org/en/stable/telegram.inputfile.html`
- python-telegram-bot `InputMediaDocument`:
  `https://docs.python-telegram-bot.org/en/stable/telegram.inputmediadocument.html`
