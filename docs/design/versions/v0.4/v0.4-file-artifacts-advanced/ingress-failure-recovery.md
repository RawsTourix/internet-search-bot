---
id: design.v0.4.ingress-failure-recovery
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-28
---

# v0.4 — Ingress failure recovery hardening

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-25. Назначение

Этот патч закрывает failure-path после durable reservation
`InputBatchDraft`. Он является продолжением [`AF-24`](ingress-reservation-hardening.md):
reservation должна происходить до streaming, но любой последующий
infrastructure failure обязан переводить зарезервированный draft в terminal
состояние, а не оставлять его открытым кандидатом для будущего grouping.

Патч не меняет deterministic ambiguity policy и не добавляет обязанности
`v0.4-input-runtime`.

## AF-25.1. Обнаруженный robustness-сценарий

В live Telegram-прогоне большого файла на Windows публикация immutable artifact
metadata завершилась transient ошибкой:

```text
PermissionError: [WinError 5] Access is denied
.tmp-art_* → art_*
```

Content уже был принят, а media-group draft находился в `INGESTING`. Сервис
пробросил `ArtifactStorageError`, но оставил draft открытым.

Последствия образовали каскад:

```text
storage failure
→ draft остаётся INGESTING
→ следующая инструкция видит старый draft
→ инструкция присоединяется к старому пакету
→ новые файлы commit-ятся без инструкции
→ agent просит уточнить задачу
```

После ещё одной попытки одновременно существовали старый zombie draft и новый
media-group draft:

```text
text event
→ matches multiple open attachment drafts
→ InputGroupingAmbiguityError
→ HTTP 422
```

Команда `/reset` не помогала, потому что очищала только LLM session memory и не
меняла durable ingress state.

## AF-25.2. Главные invariants

### Transient filesystem publish

Retry разрешён только для финальной атомарной публикации нового immutable
metadata-каталога:

```text
write temporary metadata directory
→ os.replace(temp, final)
→ bounded retry только для transient PermissionError
```

Запрещено автоматически повторять целиком:

- transport download;
- content ingestion;
- artifact lineage creation workflow;
- InputBatch reservation;
- agent cycle;
- output delivery.

Если target уже существует либо ошибка не является transient publish failure,
операция завершается обычным `ArtifactStorageError`.

### Reserved draft failure

После `ArtifactStorageError` или `ArtifactIntegrityError`, возникшего после
reservation:

```text
reserved/open InputBatchDraft
→ state=failed
→ failure_code persisted
→ draft excluded from list_open_drafts
→ agent cycle не запускается
```

FAILED, CANCELLED и ABANDONED являются terminal uncommitted states. Поздний
parallel member не может вернуть такой draft в `INGESTING` или `COLLECTING` и не
может дописать attachment state.

Для exact failed `media_group_id` сохраняется terminal group tombstone:

```text
late member of the same failed media group
→ resolves to the same FAILED batch
→ upload stream не потребляется
→ partial replacement draft не создаётся
```

Tombstone не участвует в generic `list_open_drafts`, поэтому не влияет на
следующие независимые пакеты и text-only grouping.

### Grouping ambiguity

Принятая policy сохраняется:

```text
one compatible open attachment draft
→ exact join

two real compatible open drafts
→ InputGroupingAmbiguityError
```

Исправление не выбирает «последний» или «самый новый» draft эвристически. Оно
устраняет ложного кандидата: infrastructure-failed draft больше не считается
open.

### Session reset

Для текущего filesystem runtime `/reset` означает:

```text
cancel all open drafts of exact session
→ release their active grouping indexes
→ close/fail active presentation handles
→ clear LLM session memory
```

Reset не удаляет:

- committed InputBatch;
- content-addressed bytes;
- artifact lineages и versions;
- completed OutputBatch и receipts;
- audit/recovery evidence;
- terminal failed-group tombstones других уже завершённых attempts.

## AF-25.3. Граница ответственности

```text
ResilientFileSystemArtifactStore
→ bounded transient directory-publish retry

ResilientUnifiedArtifactIngressService
→ convert post-reservation storage/integrity failure into terminal draft

ResilientFileSystemCoordinatedInputBatchStore
→ terminal-state enforcement, exact failed-group isolation,
  failure/cancel persistence and active-index cleanup on reset

session_reset
→ session-level composition of ingress cancellation and memory reset
```

Эти классы являются filesystem v0.4 implementation detail. При PostgreSQL и
worker-backed runtime те же invariants должны быть реализованы транзакцией,
lease/attempt state и explicit cancellation workflow, а не process-local
подклассами.

## AF-25.4. Acceptance criteria

### Transient retry

```text
first immutable metadata publish raises PermissionError
→ bounded retry
→ second publish succeeds
→ ровно один final metadata object
→ temporary directory removed
```

### Permanent storage failure

```text
attachment artifact creation raises ArtifactStorageError
→ reserved draft becomes FAILED
→ no open draft remains for session
→ exact failed media-group key returns the same terminal batch
→ late same-group member does not create a replacement draft
→ next independent file package + instruction joins one new batch
```

### Terminal protection

```text
FAILED draft
+ late mark_collecting / attachment mutation
→ IngressConflictError
→ state remains FAILED
```

### Reset recovery

```text
one open media-group draft
+ /reset
→ draft becomes CANCELLED with failure_code=session_reset
→ no open draft remains
→ LLM session memory cleared
→ committed history and artifacts preserved
```

### Live regression

Повторить robustness tests №2–4 на Windows без очистки `storage` между обычными
успешными прогонами и подтвердить:

- transient publish retry либо clean terminal failure;
- отсутствие zombie open drafts;
- повторный пакет не присоединяется к failed draft;
- `/reset` сообщает число отменённых незавершённых пакетов;
- нет files-only agent cycle из-за потерянной инструкции;
- true ambiguity не маскируется эвристикой.

## AF-25.5. Реализация и проверки

Основной код:

```text
src/artifacts/resilient_file_store.py
src/ingress/resilient_service.py
src/ingress/resilient_store.py
src/api/session_reset.py
src/core/message_processor.py
```

Regression tests:

```text
tests/test_artifact_ingress_failure_recovery.py
tests/test_artifact_ingress_reservation_race.py
tests/test_artifact_ingress_grouping.py
```

Автоматический validation workflow после патча:

```text
artifact suite: 153 tests, success
storage suite: 41 tests, success
plans suite: 45 tests, success
planning suite: 19 tests, success
api suite: success
compile: success
```

`implementation_status` переводится в `implemented` после live Windows
повторения robustness tests №2–4 и подтверждения отсутствия zombie drafts.
