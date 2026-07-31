---
id: design.v0.4.batch-workflows
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-31
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
- bounded current/session artifact authority;
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
2. Его initial status — `Сообщение принято. Обрабатываю…`, а не transport-level
   `Входной пакет принят.`.
3. `text → files` автоматически не объединяется; обратный порядок выражается
   явным `/collect`.
4. `/send` коммитит любой непустой explicit draft: text-only, files-only или mixed.
5. `/cancel` не запускает AgentCycle и не удаляет audit evidence.
6. На exact principal/client-instance/conversation/thread scope существует не
   более одной active collection.
7. UI содержит только `/collect`, `/send`, `/cancel`.
8. Collection snapshot и run status — разные presentation roles.
9. После `/send` collection snapshot terminalize-ится и остаётся в истории.
10. `/run` получает execution-scoped progress metadata; durable
    `InputBatch.response_route` не мутируется.
11. `result_ready` остаётся промежуточным событием. После сохранённого terminal
    receipt tracked run status редактируется напрямую: `delivered → cycle_done`,
    остальные состояния получают соответствующий delivery status.
12. Отдельное сообщение `Готово.` не заменяет tracked status и управляется
    `TELEGRAM_FINAL_STATUS_MODE=always|artefacts_only|never`; default —
    `artefacts_only`.
13. `/collect` является способом упаковать пользовательский ввод, а не командой
    очистки памяти.
14. Если session содержит suspended `WAITING_USER` cycle, committed package
    продолжает тот же cycle, сохраняет messages/working memory/artifact refs и
    добавляет refs нового InputBatch.
15. Дополнение разрешено только для suspended `WAITING_USER`; active running cycle
    всё ещё требует будущий `CycleInbox`.
16. Последний bounded набор artifact refs завершённого цикла наследуется следующим
    циклом той же session, чтобы пользователь мог продолжать работу с присланным
    или созданным файлом без повторной загрузки.
17. Session handoff не пересекает session boundary. Более старая история за
    пределами bounded handoff остаётся доступна через
    `artifact_list(scope="session")`.
18. Полная очистка session удаляет её dialog memory, runtime state и bounded
    artifact handoff; состояние других sessions не затрагивается.
19. Output grouping сохраняет `OutputPart.index`; Telegram media использует
    SDK-owned `attach://...` mapping.
20. Durable commit завершается до AgentCycle; отмена run не откатывает batch.
21. Forwarding — provenance, не attachment type.
22. Explicit draft не имеет transport quiet/deadline auto-commit semantics.
23. Persisted canonical grouping mode — `explicit_collection`; rollout-era
    `immediate_text` records мигрируют при reconcile.
24. Поздний album callback после `/send`/`/cancel` является silent no-op.
25. Exact Telegram session использует один FIFO dispatcher; разные sessions
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
отдельный run status, execution-scoped progress overlay и receipt-driven status
finalization.

### BW-P4 — реализован

Bounded same-session handoff, session reset cleanup,
`artifact_list(scope=current|session|workspace)`, activation provenance,
scope-bound cursor и historical read/search/delivery.

### BW-P5 — live acceptance

Последние live-прогоны выявили и исправили:

- stale progress target;
- поздний album commit после `/cancel`;
- non-FIFO same-session admission;
- удаление collection snapshot;
- отсутствие run callback;
- ошибочный fresh-task boundary при `/collect`;
- отсутствие continuation нового committed package в suspended `WAITING_USER`;
- потерю файлов между последовательными циклами одной session;
- сохранение bounded artifact handoff после `/reset`;
- отдельное `Готово.` после каждого text-only ответа;
- отсутствие `cycle_done` после подтверждённой доставки;
- transport-level acknowledgement вместо user-facing text acknowledgement.

## Acceptance gates

Перед переводом PR из draft требуются:

- новый полный Windows suite;
- ordinary AUTO text с initial status `Сообщение принято. Обрабатываю…`;
- text-only delivery: `result_ready → cycle_done`, без отдельного `Готово.` при
  default mode;
- artifact delivery: tracked `cycle_done` и отдельное `Готово.` после файлов при
  default mode;
- text-only/files-only/mixed `/collect → /send`;
- сохранение terminal collection snapshot;
- `/collect → package → /send` как continuation после `WAITING_USER`;
- новый cycle той же session продолжает работу с предыдущим файлом без re-upload;
- другая session не получает artifact authority;
- `/reset` очищает bounded handoff текущей session и не затрагивает другие sessions;
- rapid FIFO scenario;
- `/cancel` до завершения album quiet period без позднего commit/409.

Параметры Telegram UX в `.env.example`:

```env
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS="1.5"
TELEGRAM_FINAL_STATUS_MODE="artefacts_only"
```
