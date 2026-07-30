---
id: design.v0.4.batch-workflows.progress-events
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-14 — Aggregate progress event projection

## BW-14.1. Проблема

Manager tool может выполнить одну атомарную операцию над несколькими файлами,
но прежний progress renderer подставлял в человекочитаемое сообщение только
первое имя:

```text
message: Файл выбран для отправки: 01-order-summary.md
filenames: [
  01-order-summary.md,
  02-customer-actions.csv,
  03-order-handoff.json
]
```

Structured data было корректным, но `message` неверно описывал фактический scope
операции. Transport, UI и пользователь не должны угадывать, относится событие к
одному элементу или ко всему batch.

## BW-14.2. Разделение evidence и presentation

Progress event имеет два слоя:

```text
structured data / cycle trace
→ authoritative evidence полного результата операции

message
→ bounded локализованная projection для пользователя
```

`message` не заменяет structured data, но обязан быть ему семантически
непротиворечивым.

Для aggregate artifact delivery event сохраняются полные bounded-by-domain
списки:

```text
delivery_ids
artifact_ids
filenames
states
requested_count
selected_count
cancelled_count
```

Дополнительно projection публикует:

```text
filename_count
filenames_preview
filenames_preview_count
filenames_omitted_count
```

## BW-14.3. Cardinality

Для одного файла используются singular templates:

```text
Выбираю файл для отправки…
Файл выбран для отправки: report.csv
Файл исключён из отправки: report.csv
```

Для нескольких файлов используются aggregate templates:

```text
Выбираю файлы для отправки (3)…
Для отправки выбраны файлы (3): a.md, b.csv, c.json
Из отправки исключены файлы (2): a.md, b.csv
```

Event type при этом не дробится по элементам:

```text
artifact_delivery_selected
artifact_delivery_cancelled
```

Один tool call создаёт одно aggregate domain progress event. UI при необходимости
может построить собственное представление по structured data.

## BW-14.4. Bounded filename preview

Человекочитаемое сообщение не перечисляет неограниченный batch.

Текущая projection policy:

```text
maximum preview filenames: 3
maximum characters per preview filename: 80
```

Пример:

```text
Для отправки выбраны файлы (5):
01-summary.md, 02-actions.csv, 03-handoff.json, … (+2)
```

Полный список продолжает храниться в `data.filenames` и cycle trace в пределах
общей progress-data sanitization policy. Preview не является authoritative
перечнем.

## BW-14.5. Localization и transport independence

RU/EN templates принадлежат artifact-domain progress catalog. Telegram, Web и
CLI не формируют собственную singular/plural семантику операции.

Transport может:

- показать `message` как готовую строку;
- использовать structured data для rich UI;
- скрыть internal/debug event;
- coalesce последовательные progress updates.

Transport не должен менять event scope, отбрасывать count либо выдавать первый
элемент за всю операцию.

## BW-14.6. Acceptance criteria

### Selection start

```text
artifact_set_delivery(artifact_ids=[a, b, c], selected=true)
→ Выбираю файлы для отправки (3)…
```

### Selection completion

```text
selected_count=3
filenames=[a, b, c]
→ artifact_delivery_selected
→ message перечисляет a, b, c
→ filename_count=3
→ structured filenames содержит все три имени
```

### Cancellation

```text
artifact_set_delivery(artifact_ids=[a, b], selected=false)
→ aggregate cancel start message
→ artifact_delivery_cancelled
→ plural completion message
```

### Large batch

```text
5 filenames
→ message показывает первые 3 и … (+2)
→ data.filenames сохраняет полный список
→ filenames_omitted_count=2
```

### Compatibility

```text
single file
→ прежняя конкретная singular формулировка

unknown/non-delivery progress event
→ прежний rendering path
```

## BW-14.7. Реализация

```text
src/artifacts/progress.py
src/mcp/artifact_delivery_progress.py
src/mcp/artifact_delivery_runtime.py
tests/test_artifact_delivery_progress_projection.py
```

`ArtifactDeliveryProgressMixin` является отдельным composition layer и не
смешивает delivery state machine, tool execution и presentation projection.

Новых параметров `.env` или `mcp.config` этот patch не добавляет.
