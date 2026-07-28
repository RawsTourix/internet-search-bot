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
меняла durable ingress state. После перезапуска Gateway старый open draft также
оставался активным: startup только пытался commit-нуть ready drafts, но не
закрывал пакеты, прежние process-local owners которых уже исчезли.

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
устраняет ложного кандидата: infrastructure-failed или restart-abandoned draft
больше не считается open.

### Session reset

Для текущего filesystem runtime `/reset` означает:

```text
cancel all open drafts of exact session
→ release their active grouping indexes
→ close/fail active presentation handles
→ clear LLM session memory
```

### Startup reconciliation

`API.start` является общей границей восстановления для Telegram, Web, CLI и
других будущих transport adapters. До подключения MCP-серверов и до приёма новых
запросов shared ingress выполняет:

```text
scan ready drafts
→ commit fully stored drafts whose durable deadline elapsed
→ never start agent cycle for recovered commit
→ mark every remaining open draft as ABANDONED
→ failure_code=process_restart_abandoned
→ release active grouping indexes
→ finalize active presentation handles as failed
```

После process restart прежние upload streams, debounce tasks и commit owners не
существуют. Поэтому оставшийся open draft нельзя считать реально продолжаемым.
Он сохраняется как terminal audit record, но не должен загрязнять следующие
logical inputs.

Startup reconciliation не удаляет:

- committed InputBatch;
- content-addressed bytes;
- artifact lineages и versions;
- completed OutputBatch и receipts;
- audit/recovery evidence;
- terminal failed-group tombstones уже завершённых attempts.

## AF-25.3. Граница ответственности

```text
ResilientFileSystemArtifactStore
→ bounded transient directory-publish retry

ResilientUnifiedArtifactIngressService
→ runtime failure recovery;
→ shared API startup hook через commit_ready_drafts

ResilientFileSystemCoordinatedInputBatchStore
→ terminal-state enforcement;
→ exact failed-group isolation;
→ CANCELLED/ABANDONED transitions;
→ active group-index cleanup

ingress.startup_recovery
→ ready commit before abandonment;
→ presentation finalization;
→ structured recovery report

session_reset
→ exact-session cancellation and memory reset
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

### Restart recovery

```text
persist one ready draft and one incomplete open draft
→ recreate storage/artifact/ingress services from same filesystem root
→ API startup hook commits ready draft without agent run
→ incomplete draft becomes ABANDONED
→ group index released
→ new media group + separate instruction join one new batch
→ no InputGroupingAmbiguityError from historical state
```

### Live regression

Повторить robustness tests №2–4 на Windows без удаления `storage` и подтвердить:

- при старте появляется structured reconciliation log;
- старый zombie draft автоматически становится `ABANDONED`;
- ручной `/reset` для cleanup не требуется;
- новый пакет не присоединяется к failed/abandoned draft;
- нет files-only agent cycle из-за потерянной инструкции;
- true ambiguity не маскируется эвристикой.

## AF-25.5. Реализация и проверки

Основной код:

```text
src/artifacts/resilient_file_store.py
src/ingress/resilient_service.py
src/ingress/resilient_store.py
src/ingress/startup_recovery.py
src/api/session_reset.py
src/core/message_processor.py
```

Regression tests:

```text
tests/test_artifact_ingress_failure_recovery.py
tests/test_artifact_ingress_reservation_race.py
tests/test_artifact_ingress_grouping.py
tests/test_artifact_ingress_startup_recovery.py
```

Автоматический validation workflow после патча:

```text
artifact suite: 156 tests expected
storage suite: 41 tests
plans suite: 45 tests
planning suite: 19 tests
api suite
compile
```

`implementation_status` переводится в `implemented` после успешного CI на
актуальном head, полного локального suite и live Windows повторения robustness
теста №2 без ручного cleanup.
