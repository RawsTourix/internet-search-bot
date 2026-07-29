---
id: design.v0.4.batch-workflows.forwarded-sequencing
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-29
---

# BW-2A — Forwarded input sequencing hardening

## Назначение

Telegram может доставить выбранный пользователем набор пересылаемых сообщений
как несколько независимых updates. Даже если их `message_id` идут по порядку,
webhook runtime обрабатывает updates конкурентно. Поэтому пересланный text-only
update способен войти в Gateway раньше более ранних файлов или media group.

Этот patch закрывает transport race, не вводя задержку для обычного text-only
input и не анализируя смысл пользовательского текста.

## Обнаруженный сценарий

Live workflow содержал:

```text
2143 — forwarded report_rules.md
2144 — forwarded customers.csv
2145 — forwarded orders.csv
2146 — forwarded text instruction
```

Из-за конкурентной обработки инструкция первой попала в shared ingress как
atomic batch, а файлы сформировали отдельный media-group batch. Дополнительно
внутренний второй проход через idempotent stores ошибочно превратил первый
atomic submit в `duplicate=True`.

Следствие:

```text
instruction → committed atomic batch marked duplicate
files       → separate batch with text_part_count=0
agent       → asks user what to do with files
```

## Главные invariants

### Forwarding — provenance, не media type

`filters.FORWARDED` не является самостоятельным attachment class.

```text
forwarded text
→ normal text handler
→ TextInputPart + ForwardedMessageInputPart

forwarded document/photo/audio/etc.
→ corresponding media handler
→ media semantic part + ForwardedMessageInputPart
```

Forward provenance сохраняется независимо от маршрута обработки.

### Обычный текст остаётся немедленным

```text
non-forwarded text-only
→ no waiting
→ ordinary AUTO atomic input when no open compatible draft exists
```

Patch не превращает все текстовые запросы в debounce workflow и не угадывает
будущие вложения.

### Bounded wait только для forwarded text race

Forwarded text-only update без `source_group_id` может кратко ожидать появления
ровно одного active Telegram album в exact scope:

```text
client_instance_id
+ conversation_id
+ optional thread_id
```

Wait применяется только когда envelope уже содержит authoritative
`ForwardedMessageInputPart`. Содержимое текста, filename и пользовательское
намерение не анализируются.

```text
one album appears during wait
→ forwarded text receives exact source_group_id
→ shared ingress joins it to that group

no album appears
→ wait expires
→ forwarded text proceeds as ordinary atomic input

two active albums
→ explicit ambiguity error
```

Default transport parameter:

```env
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS="1.5"
```

Он должен быть неотрицательным и не превышать
`TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS`. Параметр и default обязаны быть
отражены в `.env.example` в том же patch.

### Original idempotency result

Reservation под scope lock является authoritative первым наблюдением
idempotency:

```text
first event reservation
→ duplicate_event=False
→ duplicate_batch=False
→ final InputSubmissionResult.duplicate=False
```

Повторный внутренний проход через `save_if_absent`/`create_for_event` после
выхода из короткой critical section не является transport retry и не может
изменить внешний результат на `duplicate=True`.

Настоящий повтор того же idempotency key:

```text
same event submitted again
→ final duplicate=True
→ committed batch reused
→ second AgentCycle не запускается
```

Текущий filesystem patch нормализует внешний result по данным authoritative
reservation. При последующей modularization предпочтительно разделить base
service на явные фазы `persist_and_reserve` и `ingest_reserved_event`, полностью
устранив внутренний повторный store pass без изменения публичного контракта.

## Граница ответственности

```text
Telegram handler routing
→ forwarded text is text, forwarded media is media

InstanceScopedTelegramArtifactGatewayClient
→ bounded wait and exact source_group_id assignment

UnifiedArtifactIngressService
→ scope serialization, durable reservation and original idempotency semantics

InputDraftControlService (next stage)
→ explicit /collect, /send and /cancel
```

Это не заменяет explicit collection mode. Для произвольного набора forwarded
text/media, особенно когда порядок или временной интервал неочевидны, полностью
детерминированный workflow остаётся:

```text
/collect
→ forward or send any supported messages/files
→ /send
```

## Acceptance criteria

### Atomic idempotency

```text
new atomic text
→ committed
→ duplicate=False

same update retry
→ same input_batch_id
→ duplicate=True
```

### Group member idempotency

```text
first member of new media group
→ collecting
→ duplicate=False
```

### Forwarded overtaking race

```text
forwarded text task starts first
→ no Gateway request during bounded wait
→ earlier forwarded album registers in exact scope
→ text receives album source_group_id
→ both responses reference one input_batch_id
```

### Ordinary text latency

```text
non-forwarded text with no active group
→ submitted immediately
→ forwarded wait window is not applied
```

### Live Telegram regression

Forward three files and one instruction as one user selection and confirm:

```text
telegram_forwarded_text_waiting_for_media_group
telegram_text_bound_to_active_media_group ... forwarded=True

one committed InputBatch
artifact_count=3
text_part_count=1
```

Must not occur:

```text
separate instruction batch
files-only AgentCycle
duplicate=True on first atomic reservation
agent asks for a task already present in the forwarded instruction
```

## Реализация и проверки

Основной код:

```text
src/ingress/unified_service.py
src/servers/telegram/scoped_artifact_bridge.py
src/servers/telegram/app.py
src/servers/telegram/config.py
.env.example
```

Regression tests:

```text
tests/test_artifact_forwarded_batch_regressions.py
```

Тематический CI после patch:

```text
artifact suite: 177 tests, success
storage suite: 41 tests, success
plans suite: 45 tests, success
planning suite: 19 tests, success
api suite: success
compile: success
```

`implementation_status` переводится в `implemented` после live Telegram
повторения исходного mixed-forward workflow.
