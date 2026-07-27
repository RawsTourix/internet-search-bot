---
id: design.v0.5.implementation-plan
version: v0.5
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Пошаговый план v0.5

## Общая цель

Перенести durable metadata/runtime state в PostgreSQL и добавить
provenance-aware RAG, сохранив модульный local mode и существующие v0.4 ports.

## Реестр updates

| Порядок | Update | Главный результат |
|---:|---|---|
| 1 | `v0.5.1-postgresql-foundation` | SQLAlchemy/Alembic, connection lifecycle и transaction foundation |
| 2 | `v0.5.2-repository-backends` | PostgreSQL implementations существующих stores/repositories |
| 3 | `v0.5.3-durable-runtime-state` | Transactional session/cycle/workspace recovery |
| 4 | `v0.5.4-lazy-content-processing` | Versioned extraction, structured representations и chunk cache |
| 5 | `v0.5.5-rag-and-memory-tools` | Keyword/semantic/hybrid retrieval с provenance |
| 6 | `v0.5.6-migration-and-recovery` | Filesystem migration, restart и rollback strategy |
| 7 | `v0.5.7-persistence-stabilization` | Performance, consistency и readiness для v0.6 |

## v0.5.1-postgresql-foundation

### Scope

- PostgreSQL service в local Docker Compose/deployment profile;
- SQLAlchemy 2.x async engine/session patterns;
- Alembic environment и baseline migration;
- connection/session factory;
- transaction manager;
- `UnitOfWork` port/implementation;
- health/readiness checks;
- schema/table/index naming conventions;
- migration validation в CI.

### Правила

- Domain models не зависят от SQLAlchemy declarative classes.
- Один request/job не передаёт живую DB session через process boundary.
- Transaction открывается application service/UnitOfWork, а не router или LLM
  tool.
- Alembic migration является единственным production schema mutation path.

### Acceptance

- empty database поднимается всеми migrations;
- downgrade policy определена для development/testing;
- failed transaction rollback не оставляет partial state;
- readiness отличает process alive от database ready;
- CI обнаруживает divergent heads и непроверяемые migrations.

## v0.5.2-repository-backends

### Scope

PostgreSQL implementations для:

```text
Content metadata / payload locator
Artifact lineage/version/candidate/delivery
Plan and plan revisions
Ingress events/drafts/batches
CycleInbox and control inbox
Output/response outbox
Session/cycle/trace state
```

Payload bytes могут оставаться filesystem-backed или перейти в hybrid
filesystem/object storage; PostgreSQL хранит identity, relations, ownership-ready
metadata и transaction boundary.

### Требования

- filesystem и PostgreSQL implementations проходят один contract suite;
- refs и public application models не меняются из-за backend;
- current artifact/plan state читается exact query, не RAG;
- optimistic revisions сохраняют semantics v0.4;
- repository methods принимают scope/access context там, где он понадобится v0.8.

## v0.5.3-durable-runtime-state

### Scope

- durable session/conversation-ready metadata;
- cycle snapshots/messages/trace events;
- `CycleWorkingMemory` и compaction generation;
- active plan/artifact access state;
- committed batches и inbox replay state;
- final result/outbox state;
- ownership/scope placeholders;
- `request_id`, `run_id`, task-output refs как preparation для v0.6.

Полный distributed `AgentRun` lifecycle относится к v0.6, но schema не должна
мешать его введению.

### Transaction boundaries

```text
commit InputBatch + admission decision
claim/apply inbox item + replay state
final inbox recheck + final result + terminal state + outbox
artifact version + lineage head
plan revision + optimistic generation
outbox claim + delivery attempt/receipt
```

### Acceptance

Process restart восстанавливает messages, working memory, plan, artifact state,
inbox, waiting state и outbox без повторного LLM cycle.

## v0.5.4-lazy-content-processing

### Scope

- processor/extractor registry;
- extractor/chunker identity и version;
- lazy extraction on first retrieval;
- representations: PDF pages/sections, spreadsheet sheets/ranges,
  presentation slides, source modules/functions;
- bounded processing by parts;
- persistent chunks и extraction status;
- rebuildable derived data;
- cache invalidation при version change.

### Правила

- original content immutable и canonical;
- extraction failure не повреждает artifact/content identity;
- OCR/previews/antivirus могут быть отдельными processors, но не являются
  обязательным core v0.5;
- background execution допускается local/in-process до v0.6 workers.

## v0.5.5-rag-and-memory-tools

### Scope

- keyword search;
- pgvector embeddings;
- semantic search;
- hybrid ranking;
- bounded retrieved chunks/ranges;
- retrieval events и evidence provenance;
- memory/cycle/content/artifact/plan tools;
- temporary retrieved context и later compaction;
- exact authorization-ready filters.

### Инварианты

- RAG не определяет current lineage head, plan revision или lease state;
- непрочитанный chunk не считается evidence;
- retrieved context содержит exact IDs/ranges;
- final grounding использует только фактически retrieved evidence;
- embedding/chunk indexes могут быть полностью перестроены.

## v0.5.6-migration-and-recovery

### Scope

- inventory filesystem manifests/state;
- deterministic import с stable IDs;
- hash/size verification;
- migration journal и resumable batches;
- duplicate-safe rerun;
- validation report до cutover;
- backup/restore rehearsal;
- rollback/read-only fallback policy;
- orphan/corruption handling.

При необходимости используется временный dual-read/compare mode, но dual-write
не становится постоянной архитектурой.

## v0.5.7-persistence-stabilization

### Проверки

- schema/index audit и query plans;
- N+1 и unbounded query review;
- connection pool limits/timeouts;
- concurrency/optimistic conflict tests;
- crash between DB commit and payload operation;
- retention/GC invariants;
- metrics для transactions, queries, extraction и retrieval;
- architecture tests dependency direction;
- documentation/migration consistency.

### Gate v0.6

После update:

- worker может получить `run_id`/job payload, открыть собственный UnitOfWork и
  восстановить state;
- Redis loss не потеряет canonical data;
- object storage adapter можно добавить без изменения agent runtime;
- task/workflow tables могут ссылаться на exact content/result/artifact refs;
- critical transitions готовы к leases/idempotent replay.

## Допустимая параллельность

- Foundation предшествует production repositories.
- Repository domains могут реализовываться параллельно после naming/transaction
  conventions.
- Lazy processors и RAG могут проектироваться параллельно с durable runtime, но
  интеграция требует canonical content/chunk schema.
- Migration начинается после стабилизации target schemas и завершается до
  persistence release gate.

## Non-goals v0.5

- Redis как обязательная dependency;
- distributed workers;
- automatic workflow scheduler;
- full microservices;
- user-code sandbox;
- полноценная account authorization;
- обязательное object storage для local mode.