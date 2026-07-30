---
id: design.v0.4.batch-workflows
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# v0.4-batch-workflows

`v0.4-batch-workflows` — самостоятельное обновление между
[`v0.4-file-artifacts-advanced`](../v0.4-file-artifacts-advanced/README.md) и
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

Оно завершает client-facing workflow вокруг `InputBatch`, `OutputBatch` и
artifact workspace:

- AUTO text input без artificial delay;
- explicit `/collect → /send | /cancel` для text/files/mixed package;
- отдельные collection snapshot и AgentCycle run status;
- native Telegram document groups;
- stable `OutputPart` grouping;
- bounded artifact manifest и scoped history catalog;
- durable commit до in-process AgentCycle;
- cancellation-safe Gateway/MCP shutdown;
- aggregate progress projection.

`CycleInbox`, additions во время реально выполняющегося AgentCycle и durable
checkpoints принадлежат следующему
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

## Документы

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
2. `text → files` автоматически не объединяется; обратный порядок выражается
   явным `/collect`.
3. `/send` коммитит любой непустой explicit draft: text-only, files-only или mixed.
4. `/cancel` не запускает AgentCycle и не удаляет audit evidence.
5. На exact principal/client-instance/conversation/thread scope существует не
   более одной active collection.
6. UI содержит только `/collect`, `/send`, `/cancel`.
7. Collection snapshot и run status — разные presentation roles.
8. После `/send` collection snapshot terminalize-ится и остаётся в истории.
9. `/run` получает execution-scoped progress metadata; durable
   `InputBatch.response_route` не мутируется.
10. `result_ready` остаётся промежуточным событием. После подтверждённого
    `delivered` receipt run status переходит в `cycle_done`.
11. `/collect` является способом упаковать пользовательский ввод, а не командой
    очистки памяти.
12. Если session содержит suspended `WAITING_USER` cycle, committed package
    продолжает тот же cycle, сохраняет его messages/working memory/artifact refs
    и добавляет refs нового InputBatch.
13. Дополнение разрешено только для suspended `WAITING_USER`; active running cycle
    всё ещё требует будущий `CycleInbox`.
14. Артефакты независимого завершённого цикла не добавляются в новый cycle
    автоматически; history активируется через `artifact_list(scope="session")`.
15. Изоляция истории не применяется внутри continuation одного `WAITING_USER`
    cycle: его существующие artifact refs являются текущим рабочим состоянием.
16. Output grouping сохраняет `OutputPart.index`; Telegram media использует
    SDK-owned `attach://...` mapping.
17. Durable commit завершается до AgentCycle; отмена run не откатывает batch.
18. Forwarding — provenance, не attachment type.
19. Explicit draft не имеет transport quiet/deadline auto-commit semantics.
20. Persisted canonical grouping mode — `explicit_collection`; rollout-era
    `immediate_text` records мигрируют при reconcile.
21. Поздний album callback после `/send`/`/cancel` является silent no-op.
22. Exact Telegram session использует один FIFO dispatcher; разные sessions
    остаются параллельными.

## Этапы

### BW-P1 — реализован

Native Telegram delivery, commit/run split, shutdown hardening, reservation
idempotency, forwarded sequencing и aggregate progress.

### BW-P2 — реализован

Durable `InputCollectionRecord`, exact scope, HTTP controls, canonical commands,
text/files/mixed commit, manual commit guard и terminal album tombstone.

### BW-P3 — реализован

Presentation generations для активного collection, persistent terminal snapshot,
отдельный run status и execution-scoped progress overlay.

### BW-P4 — реализован

`artifact_list(scope=current|session|workspace)`, bounded activation, provenance,
scope-bound cursor и historical read/search/delivery после activation.

### BW-P5 — live acceptance

Последние live-прогоны выявили и исправили:

- stale progress target;
- поздний album commit после `/cancel`;
- non-FIFO same-session admission;
- удаление collection snapshot;
- отсутствие run callback;
- ошибочный fresh-task boundary при `/collect`;
- отсутствие continuation нового committed package в suspended `WAITING_USER`;
- отсутствие `cycle_done` после подтверждённой доставки.

## Acceptance gates

Перед переводом PR из draft требуются:

- новый полный Windows suite;
- text-only/files-only/mixed `/collect → /send`;
- сохранение terminal collection snapshot;
- progress status непосредственно под `/send`;
- `result_ready → cycle_done` после delivered receipt;
- `/collect → package → /send` как continuation после `WAITING_USER`;
- rapid FIFO scenario;
- `/cancel` до завершения album quiet period без позднего commit/409;
- проверка, что independent historical artifacts требуют catalog activation.

Новых параметров `.env` или `mcp.config` последние runtime-патчи не добавляют.
Ранее добавленный параметр уже присутствует в `.env.example`:

```env
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS="1.5"
```
