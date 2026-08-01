---
id: design.release-gates
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-01
---

# Общие release gates

## Назначение

Каждая основная версия состоит из функциональных именованных обновлений и
заканчивается stabilization/hardening этапом. Версия не считается завершённой
только потому, что happy path работает локально.

## Универсальный gate

Перед завершением версии проверяются:

1. **Functional acceptance** — выполнены канонические acceptance criteria.
2. **Contract compatibility** — ports и public models не расходятся между
   implementations.
3. **Migration** — описан и проверен переход с предыдущего baseline.
4. **Recovery** — проверено восстановление после process/restart failure.
5. **Idempotency** — replay/retry не создают дублирующий durable side effect.
6. **Concurrency** — проверены locks, revisions, leases и finalization races,
   относящиеся к версии.
7. **Security** — проверены trust, ownership, secrets и resource boundaries.
8. **Observability** — есть structured state/events/metrics для диагностики.
9. **Local compatibility** — local/self-hosted mode остаётся рабочим.
10. **Documentation consistency** — current, README, principles, glossary,
    roadmap, contracts и thematic specs не противоречат друг другу.
11. **Next-version readiness** — определены contracts, которые следующая версия
    должна заменить adapters, а не переписывать.

## Минимальный отчёт обновления

Для каждого именованного update документируются:

- scope и non-goals;
- dependencies;
- domain/contracts;
- implementation sequence;
- migration/compatibility;
- failure and recovery behavior;
- required tests;
- acceptance criteria;
- подготовленные границы следующей версии.

## Gate v0.4

- Agent loop сохраняет прежнее поведение после modularization.
- Storage, planning, artifacts, input runtime и delivery доступны через явные
  services/ports.
- LLM transport и MCP runtime не являются неотделимыми частями agent loop.
- Compatibility facade не становится новым permanent owner архитектуры.
- Characterization и integration tests закрывают WAITING_USER, compaction,
  planning, artifacts и finalization.
- MCP registry scopes `builtin|instance|user|session` имеют deterministic local
  snapshot/revision и precedence.
- Unknown MCP tools используют generic safe presentation; trusted bindings могут
  использовать approved semantic profiles.
- Retry соответствует declared tool semantics; mutating call с потерянным
  response возвращает `unknown` и не повторяется автоматически.
- Remote resource handle не зависит от MCP connection object и изолирован по
  lifecycle owner.
- Terminal/reset/shutdown cleanup bounded и не превращает готовый `AgentResult`
  в failure при недоступности optional сервиса.
- Optional builtin MCP outage не блокирует unrelated Agent Runtime capabilities.

## Gate v0.5

- PostgreSQL implementations проходят те же contract tests, что filesystem.
- Critical transitions транзакционны.
- Restart восстанавливает durable session/cycle/workspace state.
- Derived chunks/embeddings перестраиваемы и не заменяют canonical content.
- Migration с filesystem baseline проверена на реальных fixtures.

## Gate v0.6

- Gateway disconnect не прерывает durable AgentRun.
- Worker loss приводит к controlled retry/lease recovery.
- Duplicate jobs и progress events безопасны.
- `TaskContextManifest` bounded и provenance-aware.
- Parallel tasks запускаются только после policy/dependency validation.
- Final result persisted до terminal `succeeded`.
- Distributed MCP registry сохраняет v0.4 scope/precedence semantics.
- Два workers видят одну committed registry revision; stale binding не может
  исполняться после disable/rebind.
- Unresolved remote-resource lifecycle metadata восстанавливается после restart.
- PostgreSQL остаётся source of truth registry state; Redis loss не уничтожает
  definitions/revisions.

## Gate v0.7

- Skills загружаются bounded и task-scoped.
- Registry snapshots versioned и replayable.
- Skill requirements не расширяют permissions.
- Extension API не зависит от subclasses AgentRuntime.
- Builtin и внешние skills проходят capability/trust tests.

## Gate v0.8

- Каждая durable resource имеет owner/scope.
- Negative authorization tests закрывают cross-user access.
- Auth sessions и linked identities имеют revocation/recovery.
- Quotas применяются до expensive execution.
- Security audit и migration ownership являются обязательными.

## Gate v0.9

- Sandbox не видит host filesystem и infrastructure credentials.
- Inputs materialized только из разрешённых refs.
- Outputs imported до teardown и проходят declared-output policy.
- Timeout/cancellation/orphan cleanup проверены.
- Local и container execution backends проходят общий contract suite.

## Gate v0.10

- Потеря runner не теряет canonical run state.
- Старый attempt не может commit после нового fencing token.
- Workspace восстанавливается на другом runner из remote refs.
- Placement соблюдает profiles, capacity и tenant quotas.
- Control plane не зависит от локального container daemon.
- Новый execution backend подключается без изменения AgentRuntime.
