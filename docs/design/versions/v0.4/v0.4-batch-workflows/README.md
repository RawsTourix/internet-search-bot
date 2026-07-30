---
id: design.v0.4.batch-workflows
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# v0.4-batch-workflows

`v0.4-batch-workflows` — самостоятельное именованное обновление между
[`v0.4-file-artifacts-advanced`](../v0.4-file-artifacts-advanced/README.md) и
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

Обновление завершает пользовательские и transport-independent workflows вокруг
`InputBatch`, `OutputBatch` и artifact workspace:

- строгая AUTO-политика без задержки обычного текста;
- explicit collection `/collect → /send | /cancel`;
- безопасное перемещение status/progress presentation;
- корректная Telegram multipart delivery;
- стабильная группировка `OutputPart` без изменения порядка;
- bounded current manifest и явный доступ к artifact history;
- durable commit до in-process AgentCycle;
- cancellation-safe Gateway/MCP shutdown;
- согласованные aggregate progress events.

`CycleInbox`, safe checkpoints и additions во время активного agent cycle остаются
у следующего [`v0.4-input-runtime`](../v0.4-input-runtime.md).

## Документы обновления

| Раздел | Документ |
|---|---|
| `BW-1`–`BW-4` | [`input-assembly.md`](input-assembly.md) |
| `BW-2A` | [`forwarded-sequencing-hardening.md`](forwarded-sequencing-hardening.md) |
| `BW-5`–`BW-6` | [`presentation-and-controls.md`](presentation-and-controls.md) |
| `BW-7`–`BW-8` | [`output-grouping.md`](output-grouping.md) |
| `BW-9` | [`artifact-access.md`](artifact-access.md) |
| `BW-10`–`BW-12` | [`contracts-and-acceptance.md`](contracts-and-acceptance.md) |
| `BW-13` | [`shutdown-and-execution.md`](shutdown-and-execution.md) |
| `BW-14` | [`progress-events.md`](progress-events.md) |
| `BW-15` | [`draft-control-foundation.md`](draft-control-foundation.md) |
| `BW-16` | [`explicit-control-plane.md`](explicit-control-plane.md) |

## Главные инварианты

1. Обычный text-only AUTO input не ждёт будущих файлов.
2. `text → files` автоматически не объединяется; для обратного порядка существует
   explicit collection.
3. `/send` коммитит любой непустой explicit draft: text-only, files-only или mixed.
4. `/cancel` не запускает AgentCycle и не удаляет audit evidence.
5. На exact principal/client-instance/conversation/thread scope существует не более
   одной active collection.
6. Пустая collection хранится отдельным `InputCollectionRecord`, не нарушая
   `InputBatchDraft.source_event_ids != []`.
7. Пользовательский интерфейс имеет только `/collect`, `/send`, `/cancel`.
   Дублирующие aliases без отдельной семантики запрещены.
8. Presentation relocation выполняется только как
   `create → durable bind → supersede → best-effort delete`.
9. Только current presentation generation является writable; failed deletion не
   возвращает старое сообщение в active state.
10. Output grouping сохраняет `OutputPart.index` и группирует только непрерывные
    совместимые участки.
11. Telegram media upload использует SDK-generated `attach://...` mapping.
12. Все авторизованные historical artifacts остаются доступны, но автоматически в
    prompt попадает только bounded active manifest.
13. Delivery выбирает exact immutable `artifact_id`; возраст сам по себе не является
    запретом.
14. Durable commit завершается до AgentCycle; cancellation run не откатывает batch.
15. Forwarding является provenance, а не attachment type.
16. Внешний `duplicate` определяется первой authoritative reservation.
17. Aggregate progress message соответствует всей операции, а full evidence хранится
    в structured `data`.
18. Client-supplied collection metadata очищается и не предоставляет authority.
19. Explicit draft не имеет transport quiet/deadline semantics и не auto-commit-ится.
20. Persisted canonical grouping mode — `explicit_collection`; rollout-era
    `immediate_text` records читаются и переписываются при reconcile.
21. После `/send` AgentCycle progress направляется в status-сообщение ниже команды;
    stale/superseded callback targets проходят bounded redirect chain.
22. Поздний quiet-timer callback explicit media group после `/send` или `/cancel`
    является silent no-op и не может повторно вызвать AUTO commit.
23. До появления `CycleInbox` одна exact Telegram session имеет один FIFO admission/run
    lane, поэтому два AgentCycle одной сессии не выполняются параллельно.
24. Артефакты завершённого цикла не наследуются новым циклом автоматически:
    historical exact IDs сначала активируются через `artifact_list(scope="session")`.

## Положение в архитектуре

```text
v0.4-file-artifacts-advanced
→ durable InputBatch/OutputBatch, capabilities, delivery, hardening

v0.4-batch-workflows
→ draft assembly, presentation generations, output grouping,
  artifact activation/catalog scopes, staged execution and progress projection

v0.4-input-runtime
→ CycleInbox, active-cycle additions, checkpoints and finalization races
```

## Этапы реализации

### BW-P1. Native output correctness — реализован

- native Telegram document groups;
- request/live regressions и individual fallback;
- durable commit/run separation;
- cancellation-safe shutdown;
- reservation idempotency;
- concurrent forwarded sequencing;
- aggregate delivery progress projection.

### BW-P2. Shared explicit draft control — реализован

- durable `InputCollectionRecord`;
- exact `InputDraftScope`;
- crash-safe `InputDraftControlService`;
- authenticated HTTP controls;
- только `/collect`, `/send`, `/cancel`;
- text-only/files-only/mixed explicit commit;
- transport auto-commit guard;
- `/send → commit → run` boundary;
- terminal media-group tombstone после `/send`/`/cancel`.

### BW-P3. Presentation relocation — реализован

- schema-v2 presentation generations;
- schema-v1 read upgrade;
- pending relocation reservation;
- generation compare-and-set;
- create → bind → supersede → best-effort delete;
- deleted/failed/unknown receipts;
- stale generation rejection;
- Telegram transport executor;
- old handle остаётся неизменным при failed deletion;
- stale progress targets redirect-ятся в latest writable status;
- initial status creation имеет bounded Telegram retry.

### BW-P4. Artifact access scopes — реализован

- bounded current activation set;
- `artifact_list(scope=current|session|workspace)`;
- opaque scope-bound cursor;
- exact activation provenance;
- historical read/search/delivery после activation;
- отсутствие implicit cross-cycle artifact handoff;
- filesystem workspace projection объявляет `effective_scope=session`.

### BW-P5. Integration and acceptance — ожидает повторный maintainer live gate

- named persisted `EXPLICIT_COLLECTION` migration — реализована;
- rollout-era JSON/index regression — реализован;
- CI thematic suites — зелёные;
- live Telegram прогон 2026-07-30 выявил stale progress target, late album commit,
  parallel same-session cycles и implicit artifact handoff — исправлено;
- новый full Windows suite — требуется;
- повторные Telegram scenarios `/collect`, `/send`, `/cancel`, relocation — требуются;
- multi-client live environment — переносится к появлению Web/CLI adapters.

## Текущий статус

Кодовые этапы `BW-P1`–`BW-P4`, structural migration и live-race hardening
реализованы.

Последний CI head `6be4d280ff324651ab1f2dfa868f6e0140788539`:

```text
compile: success
artifact suite: 231 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Последняя предоставленная полная локальная Windows suite до BW-P2B:

```text
622 tests, OK (skipped=4)
```

Перед переводом PR из draft требуется новый Windows full run и повторный живой
Telegram gate с canonical commands, status relocation, быстрыми последовательностями
команд и отменой ещё не завершившего quiet period альбома.

Новый параметр forwarded sequencing slice:

```env
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS="1.5"
```

Он присутствует в `.env.example`; в `mcp.config` новых keys нет. Live-race hardening
также не добавляет конфигурационных параметров. Каждый будущий ключ обязан в том же
patch обновлять example, validation, tests и release notes.
