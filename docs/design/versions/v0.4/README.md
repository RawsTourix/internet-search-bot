---
id: design.v0.4.index
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# v0.4

`v0.4` развивает фундамент v0.3 в сторону durable input runtime и управляемого long-running agent cycle.

Текущий активный update:

- [v0.4-input-runtime](v0.4-input-runtime/README.md)

## Current status

`v0.4-input-runtime` остаётся `partial`.

Реализовано и validated:

- IR-1 — domain models, configuration, repository ports;
- IR-2 — filesystem repositories и atomic durable state;
- IR-3 — admission, generation/reset, capacity/fencing;
- IR-4 — safe checkpoints, cycle inbox, context revisions;
- IR-5 — durable pause/continue/reset control plane;
- IR-6 — durable intermediate `AgentEmission` delivery lifecycle;
- IR-7 — finalization barrier и terminal authority;
- IR-8 — startup recovery/reconciliation и process readiness;
- IR-9 — transport-neutral diagnostics, client projections, Telegram `/status`, Web/API structured status/timeline, localization и configuration-example evidence.

Запланировано:

- IR-10 — release-final randomized/restart/live acceptance.

Следовательно, IR-9 не закрывает весь update: `v0.4-input-runtime = partial` до IR-10.

## IR-9 evidence

Code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Validated на этой границе:

- focused IR-9: `102 passed`, `0 failed`;
- full input-runtime + configuration examples: `538 passed`, `0 failed`;
- production compile: success;
- `Validate Input Runtime` #685: success;
- `Validate v0.4 file artifacts PR` #807: success.

IR-9 не меняет semantic authority предыдущих этапов. Status/timeline являются read-only projections durable IR-1—IR-8 records.

## Архитектурная граница v0.4-input-runtime

Durable authority остаётся в:

- `SessionInputRuntimeState`;
- `InputAdmissionRecord`;
- `CycleInboxItem`;
- `SessionControlCommand`;
- `ActiveCycleSnapshot`;
- `CycleContextRevision`;
- runtime handoff records;
- `AgentEmission`;
- `CycleFinalizationRecord`;
- `OutputBatch`;
- recovery evidence.

IR-9 добавляет только query/projection слой:

`durable state -> coherent exact-session diagnostics -> safe DTO -> client renderer`.

Ни Telegram status message, ни Web JSON, ни CLI renderer, ни merged display timeline не используются для admission/control/finalization/recovery correctness.

## Privacy и client independence

Generic diagnostics по умолчанию не возвращают raw user/LLM/tool/file content, secrets, callback auth, arbitrary response route metadata, internal paths или traceback.

Clients получают одну semantic DTO основу и выбирают собственную presentation strategy:

- Telegram — localized compact text/edit;
- Web/API — structured JSON;
- CLI-compatible consumer — compact renderer той же DTO.

Отдельного production runtime CLI framework сейчас нет и в IR-9 он не создавался.

## Timeline

Timeline — bounded deterministic projection existing durable history.

Она:

- сохраняет per-stream sequence/identity;
- использует durable timestamp + stable display tie-break для merge;
- не вводит global semantic sequence;
- не является event bus/event sourcing authority;
- не добавляет WebSocket/Kafka-like stream.

## Telegram

`/status` является read-only command и сохраняет существующее правило: он не входит в collection FIFO barrier и не становится input/control event.

Addendum acknowledgement различает queued/running, queued/paused и waiting reply semantics. Durable APPLIED может завершить существующий presentation status без нового semantic `AgentEmission` и без изменения LLM history.

Telegram editing использует presentation-level generation/revision fencing. Deterministic edit impossibility допускает один fallback send; ambiguous network outcome не приводит к blind duplicate send.

## Configuration

IR-9 не добавил новых configuration knobs или environment reads. Поэтому существующие `.env.example` и `src/api/mcp.config.example` остаются достаточными; общий deterministic configuration-example audit входит в green regression.

## Canonical documents

- [input-runtime README](v0.4-input-runtime/README.md)
- [domain models and state machines](v0.4-input-runtime/domain-models-and-state-machines.md)
- [admission and cycle inbox](v0.4-input-runtime/admission-and-cycle-inbox.md)
- [checkpoints and context revisions](v0.4-input-runtime/checkpoints-and-context-revisions.md)
- [control plane pause/resume](v0.4-input-runtime/control-plane-pause-resume.md)
- [agent emissions and client projections](v0.4-input-runtime/agent-emissions-and-client-projections.md)
- [finalization and recovery](v0.4-input-runtime/finalization-and-recovery.md)
- [implementation sequence](v0.4-input-runtime/implementation-sequence.md)
- [contracts and acceptance](v0.4-input-runtime/contracts-and-acceptance.md)

## Deferred after IR-9

IR-10 owns:

- randomized race matrix;
- randomized corruption/restart matrix;
- synthetic whole-system marathon;
- live Telegram maintainer acceptance;
- release-final acceptance evidence.

Also deferred outside this update:

- Telegram edited-message history rewind;
- PostgreSQL/SQLAlchemy/Alembic;
- Redis/distributed workers/leases;
- scheduler/AgentRun/TaskRun;
- parallel branches/fork-join;
- new durable global event bus.
