---
id: design.v0.4.ingress-reservation-hardening
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-28
---

# v0.4 — Durable ingress reservation hardening

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-24. Назначение

Этот патч закрепляет порядок операций между transport event, logical input
`InputBatchDraft` и attachment streaming.

Он не меняет принятую grouping policy и не добавляет обязанности
[`v0.4-input-runtime`](../v0.4-input-runtime.md). Патч устраняет process-local
гонку, при которой два относящихся к одной пользовательской задаче события могли
увидеть разное состояние открытых draft.

Канонический сквозной lifecycle уже определён в
[`v0.4-unified-input-artifact-architecture.md`](../v0.4-unified-input-artifact-architecture.md):

```text
ClientIngressEvent
→ grouping
→ InputBatchDraft
→ attachment streaming
→ CommittedInputBatch
```

Этот документ уточняет обязательную реализацию границы `grouping → draft` для
filesystem runtime.

## AF-24.1. Обнаруженный race-сценарий

Telegram media group поступает как несколько независимых transport events с
общим `media_group_id`. Отдельная инструкция пользователя поступает следующим
text event без `media_group_id`.

Проблемный порядок:

```text
file event enters shared ingress
→ grouping decision resolved
→ capability/event/draft persistence ещё не завершены

parallel text event enters shared ingress
→ open attachment draft ещё не виден
→ text ошибочно получает atomic grouping

file event finally creates media-group draft
→ transport запускает два независимых agent cycles
```

Воспроизводящий runtime trace содержал:

```text
InputBatch A: text_part_count=1, artifact_count=0
InputBatch B: text_part_count=0, artifact_count=10
```

Это не ошибка LLM и не самостоятельная Telegram business policy. Telegram лишь
создал раздельные события, а общий ingress допустил неатомарную последовательность
`read open drafts → persist/create draft`.

## AF-24.2. Главный invariant

Для одного authoritative input scope операция:

```text
resolve grouping
→ durably persist ingress event
→ create or join InputBatchDraft
```

является одной короткой reservation critical section.

До выхода из неё другой совместимый event не должен принимать grouping decision
по устаревшему списку открытых draft.

После выхода:

- `input_batch_id` и event binding уже durable;
- attachment slots уже зарегистрированы в draft;
- поздняя инструкция может увидеть и выбрать открытый attachment draft;
- тяжёлое чтение bytes, hashing, format detection и artifact creation выполняются
  без reservation lock;
- agent runtime по-прежнему получает только immutable `CommittedInputBatch`.

## AF-24.3. Scope reservation lock

Filesystem v0.4 использует process-local scoped lock. Ключ строится только из
стабильной authority:

```text
session_id
+ client_type
+ client_instance_id
+ sender.principal_id
```

Display name, filename, время обработки и transport download metadata не входят
в ключ.

Critical section включает:

```text
list compatible open drafts
resolve InputGroupingDecision
resolve immutable capability snapshot / locale
IngressEventStore.save_if_absent
create_for_event или append_event_to_batch
reset quiet deadline при exact join
```

Critical section не включает:

```text
remote attachment download
ContentStore.save_stream
hashing и format detection
ArtifactVersion creation
quiet/sealing wait
batch commit wait
agent execution
output delivery
```

Поэтому несколько файлов одного media group могут загружаться параллельно после
быстрой последовательной reservation, а независимые scopes не блокируют друг
друга.

## AF-24.4. Attachment provider contract

`AttachmentStreamProvider.open_stream()` возвращает lazy `AsyncIterator`.
Подготовка iterator может валидировать locator, но remote body не должен читаться
до начала iteration внутри `ArtifactIngressService`.

Это сохраняет порядок:

```text
transport locator accepted
→ event/draft reserved
→ iterator consumed
→ bytes stored
```

Если locator или stream завершается ошибкой после reservation, draft становится
`failed`; он не публикуется runtime как частичный committed batch.

Новый transport provider обязан сохранить эту lazy-streaming семантику либо
использовать отдельный explicit prepare/reserve API, не выполняя remote I/O до
reservation.

## AF-24.5. Граница ответственности

```text
Telegram/Web/CLI adapter
→ normalizes transport event and locator hints

Unified ingress service
→ owns grouping decision and durable draft reservation

Attachment provider
→ yields exact bytes from a closed origin

Artifact ingress
→ stores content and fills already reserved slots

Batch store
→ atomically publishes CommittedInputBatch
```

Transport server не исправляет гонку локальным `sleep`, увеличенным debounce или
неявным объединением соседних сообщений. Эти механизмы не являются источником
истины для logical input.

## AF-24.6. Idempotency и failure semantics

Reservation повторяет существующие гарантии:

- `IngressEventStore.save_if_absent` связывает idempotency key с одним `event_id`;
- повторная reservation того же event возвращает существующий draft/committed
  binding;
- exact join выполняется под store lock;
- true close/commit race может создать новый atomic continuation input;
- validation, authority и batch-limit conflict открытого draft не обходятся
  созданием скрытого atomic batch;
- provider failure закрывает зарезервированный draft как failed;
- после commit исходный batch не открывается повторно.

## AF-24.7. Будущая service/microservice граница

Process-local lock является реализацией filesystem v0.4, а не частью публичного
контракта.

При переходе к PostgreSQL и distributed runtime та же critical section заменяется
одним из эквивалентных механизмов:

```text
transaction + row/advisory lock
single-writer ingress coordinator
partitioned event consumer by authoritative scope
```

При этом не меняются:

- `ClientInputEnvelope`;
- `ClientIngressEvent`;
- `InputGroupingDecision`;
- `InputBatchDraft`;
- `CommittedInputBatch`;
- transport adapters и agent protocol.

Distributed queue, `CycleInbox`, workers и cross-process leases остаются scope
v0.5/v0.6 и не добавляются этим патчем.

## AF-24.8. Acceptance criteria

### Reservation ordering

```text
file event уже вошёл в ingress,
но persistence искусственно задержана
+ parallel instruction event того же scope
→ instruction ждёт завершения reservation critical section
→ видит ровно один open attachment draft
→ оба event имеют один input_batch_id
```

### Parallel streaming

```text
несколько media-group events зарезервированы
→ reservation lock освобождён
→ attachment streams могут обрабатываться параллельно
→ commit возможен только после stored всех slots
```

### Authority isolation

```text
другой sender/client instance/session
→ другой reservation scope
→ его atomic input не присоединяется к чужому draft
→ не обязан ждать чужой attachment streaming
```

### Failure

```text
provider stream fails после reservation
→ event и draft остаются наблюдаемыми
→ draft state=failed
→ agent cycle не запускается
```

### Regression result

```text
10-file media group + later instruction
→ один committed InputBatch
→ artifact_count=10
→ text_part_count=1
→ один response anchor/presentation
→ один agent cycle
```

## AF-24.9. Реализация и проверки

Основной код:

```text
src/ingress/unified_service.py
src/ingress/coordinated_store.py
src/ingress/grouping.py
src/api/attachment_provider.py
```

Regression tests:

```text
tests/test_ingress_reservation_race.py
tests/test_unified_input_runtime_foundation.py
tests/test_artifact_ingress_grouping.py
tests/test_artifact_transport_failures.py
tests/test_attachment_provider.py
```

`implementation_status` переводится в `implemented` только после успешного
compile/test validation и повторного Telegram workflow без разделения instruction
и artifacts на разные batches.
