---
id: design.current
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# Текущее состояние архитектуры

Текущий активный архитектурный baseline — `v0.4-input-runtime`.

Статус этапов:

- IR-1 — implemented;
- IR-2 — implemented;
- IR-3 — implemented;
- IR-4 — implemented;
- IR-5 — implemented;
- IR-6 — implemented;
- IR-7 — implemented;
- IR-8 — implemented;
- IR-9 — implemented;
- IR-10 — planned.

Общий `v0.4-input-runtime` остаётся `partial`: release-final randomized/restart/live acceptance относится к IR-10 и ещё не выполнен.

## Канонические документы

Основной дизайн:

- [README](README.md)
- [principles](principles.md)
- [dependency rules](dependency-rules.md)
- [runtime and deployment profiles](runtime-and-deployment-profiles.md)
- [v0.4 overview](versions/v0.4/README.md)
- [v0.4-input-runtime](versions/v0.4/v0.4-input-runtime/README.md)
- [domain models and state machines](versions/v0.4/v0.4-input-runtime/domain-models-and-state-machines.md)
- [admission and cycle inbox](versions/v0.4/v0.4-input-runtime/admission-and-cycle-inbox.md)
- [checkpoints and context revisions](versions/v0.4/v0.4-input-runtime/checkpoints-and-context-revisions.md)
- [control plane pause/resume](versions/v0.4/v0.4-input-runtime/control-plane-pause-resume.md)
- [agent emissions and client projections](versions/v0.4/v0.4-input-runtime/agent-emissions-and-client-projections.md)
- [finalization and recovery](versions/v0.4/v0.4-input-runtime/finalization-and-recovery.md)
- [implementation sequence](versions/v0.4/v0.4-input-runtime/implementation-sequence.md)
- [contracts and acceptance](versions/v0.4/v0.4-input-runtime/contracts-and-acceptance.md)

## Текущий implemented baseline

### Ingress и durable input

Client adapters не передают произвольный пользовательский текст непосредственно в agent cycle. Transport input нормализуется в ingress/application слой, а durable `CommittedInputBatch` остаётся источником принятого входа.

IR-1—IR-4 реализуют:

- domain models `SessionInputRuntimeState`, `InputAdmissionRecord`, `CycleInboxItem`, `ActiveCycleSnapshot`, `CycleContextRevision`;
- durable admission с stable IDs/idempotency;
- exact-session coordination;
- cycle-local accepted/applied watermarks;
- bounded inbox/capacity accounting;
- safe checkpoints и `CycleInputApplier`;
- context revision history без повторного создания semantic input;
- generation/reset fencing.

Durable state остаётся semantic authority. Process-local queues, client messages и diagnostics не участвуют в admission/checkpoint correctness.

### Control plane

IR-5 реализует durable `SessionControlCommand` и semantics `/stop`, `/continue`, `/reset`.

Ключевые свойства:

- pause request и фактический `PAUSED_BY_USER` различаются;
- pause становится applied только на safe checkpoint;
- `/continue` возобновляет тот же cycle и использует frozen resume target;
- `WAITING_USER` без реального пользовательского ответа не превращается в RUNNING только из-за `/continue`;
- stale/reset-fenced commands не меняют новый generation;
- duplicate transport delivery не создаёт вторую semantic command authority.

### Intermediate AgentEmission

IR-6 реализует durable intermediate `AgentEmission` lifecycle:

`READY -> DELIVERING -> DELIVERED | FAILED | UNKNOWN | CANCELLED`.

`UNKNOWN` сохраняет delivery ambiguity и запрещает blind replay как будто доставка точно не произошла.

`ProgressEvent` остаётся transient/coalescible presentation и не конвертируется в durable emission.

### Finalization

IR-7 реализует `CycleFinalizationRecord` и terminal barrier:

`PREPARED -> RESULT_PERSISTED -> OUTPUT_READY -> TERMINAL_COMMITTED`

с возможными `ABORTED_NEW_INPUT`, `ABORTED_CONTROL`, `FAILED_RECOVERABLE`, `FAILED_TERMINAL`.

`SessionInputRuntimeState.cycle_status == DONE` сам по себе не является достаточной terminal authority для clients. Valid terminal projection требует matching `TERMINAL_COMMITTED` и завершённый runtime handoff в соответствии с IR-7/IR-8 contract.

### Startup recovery и readiness

IR-8 реализует startup recovery/reconciliation до ordinary runtime work.

Process readiness отделена от session cycle status:

- `RECOVERING`;
- `READY`;
- `FAILED`;
- `STOPPING`;
- `STOPPED`.

Recovery сохраняет conservative semantics для interrupted/ambiguous side effects и не выполняет blind retry там, где durable evidence не доказывает, что side effect не произошёл.

### IR-9 diagnostics и client projections

IR-9 реализован на code/test boundary
`068f8f6682e7b7b805b60dbb640b53b671cc8565`.

Code evidence:

- `Validate Input Runtime` #685 — success;
- focused IR-9 — `102 passed`, `0 failed`;
- full input-runtime/config regression — `538 passed`, `0 failed`;
- production compile — success;
- `Validate v0.4 file artifacts PR` #807 — success.

IR-9 добавляет transport-neutral diagnostics/query layer:

`durable IR-1—IR-8 records -> coherent exact-session read -> RuntimeStatusSnapshot / RuntimeTimeline -> client renderer`.

Application diagnostics не импортирует Telegram/FastAPI/filesystem path semantics и не становится runtime authority.

Filesystem implementation делает короткий exact-session coherent read под существующей `SessionLockRegistry`; formatting, localization, Telegram network, HTTP serialization и CLI rendering происходят после release lock.

Current status включает safe structured fields:

- process readiness;
- session cycle status;
- generation;
- active cycle/context revision IDs;
- accepted/applied input watermarks;
- queued/claimed/applying/applied/cancelled/failed counts;
- oldest queued age;
- pending/applied control sequence и effective command/state;
- handoff state;
- emission counts;
- finalization state;
- current/last safe issue code;
- initial-request и addendum projections;
- safe recovery summary/notice, когда evidence доступен текущему process.

Generic diagnostics по умолчанию не включают:

- raw user text;
- LLM/system messages;
- prompts;
- tool arguments/results;
- file/artifact contents;
- tokens/API keys/callback auth;
- arbitrary `response_route.metadata`;
- internal filesystem paths;
- traceback/raw exception dump.

Current counts всегда fenced текущим session generation. Старые generation records могут остаться audit evidence, но не попадают в current queue/emission/finalization counts.

### Bounded timeline

IR-9 timeline является projection existing durable history, а не новым event sourcing layer.

В разных streams сохраняются собственные ordering authorities. Cross-stream display ordering использует durable timestamp и stable deterministic tie-break, но не является semantic global sequence и не используется для runtime correctness.

Текущая query boundary:

- default `limit = 20`;
- maximum `limit = 100`;
- current exact session/current cycle metadata only;
- deterministic ordering при одинаковых timestamps.

Новый durable event bus, WebSocket stream, Kafka-like log или repository-wide status scan не добавлялись.

### Input/addendum projection

Initial START_CYCLE projection отражает durable lifecycle:

`admitted/running -> waiting | pause_requested | paused | interrupted | finalizing | terminal`.

Для additions IR-9 завершает client projection:

- `input_addendum_admitted`;
- `input_addendum_applying`;
- `input_addendum_applied`;
- `input_addendum_cancelled`;
- `input_addendum_failed`.

Acknowledgement различает:

- `QUEUED_RUNNING` — принято и поставлено в очередь текущего cycle;
- `QUEUED_PAUSED` — принято в очередь, cycle остаётся paused;
- `RESUME_WAITING` — реальный reply принят в тот же cycle.

Queued acknowledgement никогда не обещает, что addition уже applied.

После durable `INPUT_APPLIED` MCP/checkpoint integration испускает только transient structured projection event с `input_batch_id`, `cycle_sequence`, generation и locale. Это событие не создаёт `AgentEmission`, не добавляется в LLM history и не меняет admission/checkpoint state.

### Telegram projection

Production `/status` — high-priority read-only consumer общей diagnostics DTO:

- trusted Telegram session определяется server-side;
- `/status` не создаёт `CommittedInputBatch`;
- не создаёт control sequence;
- не будит runner;
- не меняет watermarks/generation;
- не создаёт `AgentEmission`;
- не входит в collection semantic FIFO barrier;
- legacy duplicate `/status` handler после high-priority response не выполняется.

Compact Telegram status локализуется через RU/EN catalogs и не является database dump.

Для addendum presentation сохраняется bounded presentation-only mapping `input_batch_id -> presentation handle`. После durable APPLIED редактируется handle именно этого InputBatch, а не status исходного long-running run.

Presentation edit policy:

- deterministic Telegram edit impossibility -> один safe fallback send и локальное rebinding presentation handle;
- ambiguous network edit/send -> `UNKNOWN`, без blind duplicate send;
- session generation + presentation revision fencing не даёт старому queued/old-generation update стать финальным visible state;
- existing generic progress path сохраняет собственную versioned queue и `stop_progress_edits()` terminal barrier, поэтому stale progress не перезаписывает terminal presentation.

Semantic `AgentEmission` по-прежнему доставляется отдельным сообщением и не смешивается с status edit lifecycle.

### Web/API и CLI

HTTP/Web получает structured JSON, а не Telegram-ready text:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Web key разрешается только в существующий `web:session:*` namespace и не может подставить Telegram/internal session ID. Internal `*` scope может явно читать exact session. Telegram API key не получает generic session-injection diagnostics route.

Отдельного production runtime CLI framework в проекте сейчас нет. IR-9 не создаёт его искусственно: общий `RuntimeStatusSnapshot -> CLI renderer` существует как transport-neutral consumer path и тестируется без отдельной business logic/repository scan.

### Localization и configuration examples

Все новые user-visible Telegram projection strings находятся в существующих `ru.json`/`en.json` catalogs. Deterministic tests проверяют parity required keys и formatter placeholders.

IR-9 не добавил новых configuration knobs или environment reads. Поэтому `.env.example` и `src/api/mcp.config.example` не требуют новых полей. Existing common configuration-example audit сохранён и входит в green `538 passed` regression.

## Архитектурные инварианты после IR-9

1. Durable IR-1—IR-8 state остаётся semantic authority.
2. Diagnostics только READ/DERIVE и не принимает admission/control/finalization/recovery решений.
3. Exact-session current-generation projection не смешивает данные разных sessions/generations.
4. Network/rendering не выполняются под runtime session coordination lock.
5. Timeline не является global semantic event sequence.
6. `UNKNOWN` side-effect outcome не отображается как доказанный `FAILED`.
7. `stop accepted` не отображается как уже paused до durable pause authority.
8. `WAITING_USER + /continue` не отображается как running без real reply.
9. `DONE` projection требует IR-7/IR-8 terminal authority.
10. Client status/progress text не попадает в LLM history.
11. Presentation handles не являются addendum semantic identity.
12. RU/EN localization и config example audit входят в deterministic regression.

## Что ещё не реализовано

### IR-10 — planned

Следующий этап — release-final acceptance/roast:

- randomized race matrix;
- randomized corruption matrix;
- restart/recovery repetition;
- synthetic whole-system marathon;
- live Telegram maintainer acceptance;
- real LLM/MCP/internet smoke только в IR-10 acceptance boundary, если предусмотрено его планом;
- final release evidence для всего `v0.4-input-runtime`.

До IR-10 весь `v0.4-input-runtime` остаётся `partial`.

## Deferred вне текущего update

Не входят в IR-9 и не реализованы этим stage:

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic migration;
- Redis/distributed workers/leases;
- scheduler;
- AgentRun/TaskRun;
- parallel branches/fork-join;
- новый durable global event bus.

## Следующий implementation stage

Следующий canonical stage: `IR-10`.

До его начала не следует менять уже закрытые IR-1—IR-9 semantics без конкретного production-reachable defect или необходимости, обнаруженной самим IR-10 acceptance.
