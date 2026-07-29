---
id: design.v0.4.telegram-robustness-hardening
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-29
---

# v0.4 — Telegram robustness hardening

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-26. Назначение

AF-26 закрывает проблемы, обнаруженные второй ревизией robustness tests после
AF-24/AF-25. Патч не переносит Telegram semantics в shared ingress и не
подменяет решения LLM filename-эвристиками.

Области:

1. transport-level sequencing отдельной инструкции после Telegram media group;
2. native document group upload и точная диагностика Telegram `BadRequest`;
3. startup reconciliation неclaimable legacy `READY OutputBatch`;
4. видимая terminal error delivery при сетевом timeout;
5. базовый prompt contract для явного `format_id` и проверки tool result.

## AF-26.1. Active album text sequencing

Telegram присылает альбом и следующую текстовую инструкцию отдельными updates.
Даже после durable reservation hardening текст мог попасть в Gateway на
несколько миллисекунд раньше первого file request:

```text
Telegram server already receives album members
→ standalone text enters scoped bridge first
→ shared ingress has no open attachment draft yet
→ text becomes atomic
→ files form a separate files-only batch
```

Transport adapter уже знает об активном album. Поэтому exact sequencing
выполняется до shared ingress:

```text
first album member enters instance-scoped Telegram bridge
→ active group registered for exact bot/chat/thread scope
→ one standalone text in the bounded join window receives exact source_group_id
→ file and text requests may reach Gateway in any order
→ deterministic group key creates/joins one InputBatchDraft
```

Invariants:

- обычный text без active album остаётся atomic и не задерживается;
- active group закрывается перед commit-and-run;
- join window bounded и не превышает media-group maximum lifetime;
- два active albums одного scope дают explicit transport ambiguity;
- текстовое содержимое не анализируется для принятия grouping-решения;
- shared ingress ambiguity policy не ослабляется.

Live Telegram workflow 2026-07-29 подтвердил эту policy: три файла и отдельная
поздняя инструкция с первого прогона сформировали один InputBatch с
`artifact_count=3`, `text_part_count=1`.

## AF-26.2. Native Telegram document group

### Correct multipart mapping

`sendMediaGroup` передаёт media descriptions как JSON, а file bytes — отдельными
multipart parts. Поэтому каждый `InputFile` внутри `InputMediaDocument` обязан
иметь unique attach name и JSON URI вида:

```text
attach://<attach_name>
```

Первоначальный AF-26 path создавал:

```text
InputFile(handle, filename, read_file_handle=false)
```

без `attach=True`. Уже готовый `InputFile` не перепарсивался
`InputMediaDocument`, matching multipart reference отсутствовала, и live
Telegram вернул:

```text
BadRequest: Can't parse inputmedia: media not found
```

Исправленный streaming path:

```text
open exact claimed delivery bytes
→ keep SpooledTemporaryFile handles open
→ InputFile(
     handle,
     filename,
     attach=true,
     read_file_handle=false,
   )
→ InputMediaDocument(media=InputFile)
→ send_media_group
```

PTB создаёт unique `attach_name`, `attach_uri=attach://...` и matching multipart
field. Handles остаются открытыми до завершения request.

Следующий `v0.4-batch-workflows` закрепляет предпочтительный SDK-owned path, где
raw handle/bytes передаются прямо в `InputMedia*`; текущий low-level
`attach=True` path является официально поддерживаемым и закрывает live defect.

### Diagnostics and fallback

При `BadRequest` лог сохраняет bounded sanitized сообщение Telegram вместе с
representation, числом частей и filenames. Receipt error category остаётся
стабильной и не включает произвольный transport text.

Безопасные retry/fallback правила:

- reply-target `BadRequest` известен как unsent: streaming group один раз
  повторяется без reply metadata;
- другой `BadRequest` известен как unsent: если все sizes известны и суммарный
  объём не превышает configured spool-memory budget, выполняется один bounded
  eager retry с теми же exact bytes;
- второй `BadRequest` после eager retry известен как unsent и разрешает
  существующий individual document fallback;
- если eager representation превышает budget, individual fallback выполняется
  сразу после первого known-unsent `BadRequest`;
- timeout/network error после начала send остаётся `UNKNOWN` и не повторяется;
- receipt mismatch остаётся `UNKNOWN`;
- fallback не скрывает original diagnostic evidence.

## AF-26.3. Legacy READY authority

Старые compatibility batches могли получить искусственный
`client_instance_id=legacy-committed-batch:<client>`. Ни один реальный transport
worker не имеет права claim-ить такую authority, поэтому batch навсегда
оставался `READY`.

В текущем runtime reconciliation вызывается Gateway composition root до общего
`API.start`, то есть до открытия Gateway для transport requests:

```text
scan recoverable READY OutputBatch
→ exact legacy sentinel
→ state=CANCELLED
→ error_code=unclaimable_legacy_client_instance
→ immutable manifest/history preserved
```

Сам reconciler transport-independent и может использоваться будущими
composition roots. При modularization его ownership переносится в общий runtime
startup coordinator, чтобы Web/CLI/worker entrypoints не повторяли wiring.

Другие `READY` batches автоматически не отменяются. Для них startup log обязан
показывать:

- output_batch_id;
- session_id;
- kind;
- client_type;
- client_instance_id.

Так valid batch другого bot instance не принимается за orphan.

## AF-26.4. Terminal text fallback

Новая Telegram text message является non-idempotent transport operation. После
исчерпания retries timeout не доказывает, что message не был принят, поэтому
повторять тот же send небезопасно.

Если существует известный progress/status message:

```text
send-new retries exhausted with TimedOut/NetworkError
→ do not repeat send-new again
→ edit exact known status_message_id
→ terminal error remains visible
```

Fallback не утверждает успешную доставку результата и не создаёт новый agent
cycle. Если status handle отсутствует, ошибка остаётся explicit transport
failure.

## AF-26.5. Artifact format prompt contract

Runtime не выводит `format_id` из расширения и не исправляет решение модели
эвристикой. Базовый system prompt требует:

1. явно передавать `format_id` по требуемой структуре и назначению;
2. не считать filename extension доказательством фактического формата;
3. после create/replace/version проверять returned filename, format_id, MIME и
   exact artifact_id;
4. не выбирать mismatched artifact для delivery.

В v0.7 эти правила могут быть расширены отдельным file-workflow skill, но
базовые invariants остаются в system protocol.

## AF-26.6. Acceptance criteria

### Text before file reservation

```text
file submit enters scoped bridge
→ its Gateway HTTP request is blocked
→ separate text submit starts
→ text envelope carries exact album source_group_id
→ both responses reference one input_batch_id
```

### Multiple active albums

```text
two active media groups in exact scope
+ standalone text
→ explicit TelegramArtifactBridgeError
→ no guessed association
```

### Document group

```text
two documents
→ each InputFile has unique non-empty attach_uri
→ one send_media_group with open streaming handles and exact filenames

known reply-target BadRequest
→ one streaming retry without reply metadata

other BadRequest within eager budget
→ exact bounded diagnostic log
→ one eager retry

second known-unsent BadRequest
→ safe individual fallback
```

### READY recovery

```text
legacy sentinel READY + current-instance READY
→ legacy becomes CANCELLED with audit code
→ current batch remains READY
→ remaining authority is logged
```

### Terminal error

```text
send-new exhausts network retries
+ known status message
→ exact status message edited with terminal text
```

### Prompt contract

System protocol contains no extension-to-format runtime heuristic. It requires
explicit format selection and verification of returned metadata.

## AF-26.7. Automated validation

Current thematic validation:

```text
artifact suite: 168 tests, success
storage suite: 41 tests, success
plans suite: 45 tests, success
planning suite: 19 tests, success
api suite: success
compile: success
```

AF-26 regression modules входят в существующий `test_artifact*.py` discovery и
проверяют:

- конкурентный text submit при заблокированном file HTTP request;
- ordinary text после закрытия album;
- explicit ambiguity для двух active albums;
- streaming InputFile filenames;
- unique non-empty `attach_uri` и lazy open handles;
- bounded eager group retry;
- individual fallback после двух known-unsent BadRequest;
- reply-target retry без reply metadata;
- legacy/current READY authority reconciliation;
- terminal status fallback;
- отсутствие extension-to-format heuristic в system protocol.

## AF-26.8. Live gate

Повторить targeted Telegram delivery и подтвердить:

- text и active album всегда дают один InputBatch;
- native document group после `attach=True` доставляется одним album;
- старые legacy READY больше не появляются в recoverable startup count;
- текущие remaining READY имеют понятную exact authority;
- terminal LLM/network error остаётся видимой пользователю;
- working CSV создаётся с корректным format_id либо LLM самостоятельно
  исправляет mismatch до delivery.
