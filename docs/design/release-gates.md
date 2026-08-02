---
id: design.release-gates
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Общие release gates

## Назначение

Каждая основная версия состоит из функциональных именованных обновлений и
заканчивается stabilization/hardening этапом. Версия не считается завершённой
только потому, что happy path работает локально.

Runtime/application profiles определены в
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).

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
9. **Self-hosted compatibility** — supported self-hosted Service Application
   profile остаётся рабочим.
10. **Profile boundary compatibility** — application profile, hosting mode,
    topology и execution backend не смешиваются; новая capability не расширяет
    security ceiling другого profile.
11. **Documentation consistency** — current, README, principles, glossary,
    roadmap, contracts и thematic specs не противоречат друг другу.
12. **Next-version readiness** — определены contracts, которые следующая версия
    должна заменить adapters, а не переписывать.

Future Local Agent Application не является обязательным acceptance target
текущих service-side версий. Gate требует только не создавать зависимость
AgentRuntime от Service Application shell, которая сделает отдельный composition
root невозможным.

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
- Service Application создаёт `AgentRuntime` через явный composition root.
- `AgentRuntime` не импортирует Telegram/Web adapters, FastAPI composition или
  будущие local-agent adapters.
- `ConfigProvider` публикует validated revision; invalid reload сохраняет
  предыдущую revision, а active AgentCycle не меняет config посередине.
- MCP registry scopes `builtin|instance|user|session` имеют deterministic local
  snapshot/revision и precedence.
- MCP transport support отделён от application/hosting admission policy.
- Новая builtin definition использует Streamable HTTP.
- Managed Service Application отклоняет user/session stdio definition до spawn.
- Self-hosted operator-managed instance stdio, если реализовано, требует явной
  policy и не становится user capability.
- Unknown MCP tools используют generic safe presentation; trusted bindings могут
  использовать approved semantic profiles.
- Retry соответствует declared tool semantics; mutating call с потерянным
  response возвращает `unknown` и не повторяется автоматически.
- Remote resource handle не зависит от MCP connection object и изолирован по
  lifecycle owner.
- Terminal/process manager contract не получает host execution fallback в
  Service Application.
- Terminal/sandbox output становится user-visible artifact только через import и
  delivery contracts.
- Terminal/reset/shutdown cleanup bounded и не превращает готовый `AgentResult`
  в failure при недоступности optional сервиса.
- Optional builtin MCP outage не блокирует unrelated Agent Runtime capabilities.
- Реализация Future Local Agent Application не требуется для завершения v0.4.

## Gate v0.5

- PostgreSQL implementations проходят те же contract tests, что filesystem.
- Critical transitions транзакционны.
- Restart восстанавливает durable session/cycle/workspace state.
- Derived chunks/embeddings перестраиваемы и не заменяют canonical content.
- Migration с filesystem baseline проверена на реальных fixtures.
- Single-process self-hosted Service Application сохраняется.
- Persistence models не смешивают operator configuration и owner-scoped user
  settings/resources.

## Gate v0.6

- Gateway disconnect не прерывает durable AgentRun.
- Worker loss приводит к controlled retry/lease recovery.
- Duplicate jobs и progress events безопасны.
- `TaskContextManifest` bounded и provenance-aware.
- Parallel tasks запускаются только после policy/dependency validation.
- Final result persisted до terminal `succeeded`.
- Agent Runtime worker/service использует тот же AgentRuntime contract, что
  in-process Service Application.
- Distributed MCP registry сохраняет v0.4 scope/precedence/admission semantics.
- Два workers видят одну committed registry revision; stale binding не может
  исполняться после disable/rebind.
- Unresolved remote-resource lifecycle metadata восстанавливается после restart.
- PostgreSQL остаётся source of truth registry state; Redis loss не уничтожает
  definitions/revisions.

## Gate v0.7

- Skills загружаются bounded и task-scoped.
- Registry snapshots versioned и replayable.
- Skill requirements не расширяют permissions или application profile ceiling.
- Extension API не зависит от subclasses AgentRuntime.
- Builtin и внешние skills проходят capability/trust tests.

## Gate v0.8

- Каждая durable resource имеет owner/scope.
- Negative authorization tests закрывают cross-user access.
- Auth sessions и linked identities имеют revocation/recovery.
- Quotas применяются до expensive execution.
- Operator `agent.config` и per-user settings/credentials имеют разные owners и
  repositories.
- Self-hosted и managed Service Application используют совместимые identity
  contracts.
- Security audit и migration ownership являются обязательными.

## Gate v0.9

- Sandbox не видит host filesystem и infrastructure credentials.
- Inputs materialized только из разрешённых refs.
- Outputs imported до teardown и проходят declared-output policy.
- Terminal manager tools Service Application используют approved execution
  backend и не выполняются на host control plane как fallback.
- Timeout/cancellation/orphan cleanup проверены.
- Local и container execution backends проходят общий contract suite.
- Tests явно различают `LocalProcessExecutionBackend`, sandbox instance и Future
  Local Agent Application.

## Gate v0.10

- Потеря runner не теряет canonical run state.
- Старый attempt не может commit после нового fencing token.
- Workspace восстанавливается на другом runner из remote refs.
- Placement соблюдает profiles, capacity и tenant quotas.
- Control plane не зависит от локального container daemon.
- Новый execution backend подключается без изменения AgentRuntime.
