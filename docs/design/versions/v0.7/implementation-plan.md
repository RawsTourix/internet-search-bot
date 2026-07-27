---
id: design.v0.7.implementation-plan
version: v0.7
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Пошаговый план v0.7

## Общая цель

Создать extension platform, в которой skills выбираются и загружаются для
конкретного `TaskRun`, используют bounded context и не получают разрешения только
на основании собственного manifest.

## Реестр updates

| Порядок | Update | Главный результат |
|---:|---|---|
| 1 | `v0.7.1-skill-package-format` | Versioned manifest и skill content contract |
| 2 | `v0.7.2-skill-registry` | Scopes, revisions, enable/disable и precedence |
| 3 | `v0.7.3-skill-discovery` | Bounded retrieval и task-scoped selection |
| 4 | `v0.7.4-skill-runtime-integration` | Providers/policies/hooks в TaskRuntime |
| 5 | `v0.7.5-capability-and-trust` | Effective capability и trust enforcement |
| 6 | `v0.7.6-builtin-skills` | Базовый набор system/domain skills |
| 7 | `v0.7.7-extension-platform-stabilization` | API compatibility, security и v0.8/v0.9 readiness |

## v0.7.1-skill-package-format

Skill package определяет:

- stable name/ID и semantic package version;
- schema version;
- title/description/tags;
- `skill.md` или эквивалентный instruction content;
- required capabilities;
- supported executor profiles;
- tool/provider dependencies;
- input/output expectations;
- resource/network requirements;
- trust class и provenance;
- compatibility range extension API.

Manifest не содержит secrets и не может назначить себе system/builtin scope.

## v0.7.2-skill-registry

Scopes:

```text
builtin
instance
user
session
```

Registry обеспечивает:

- immutable snapshot/revision;
- deterministic precedence и conflict policy;
- enable/disable;
- install/update/remove state;
- package hash/signature/provenance metadata;
- visibility filters;
- cache invalidation;
- recovery after restart;
- совместимость с MCP capability registry v0.6.

`user` ownership полноценно enforced после v0.8, но schema и API уже принимают
scope/owner context.

## v0.7.3-skill-discovery

Discovery использует compact index и bounded candidate loading:

```text
TaskContextManifest
→ keyword/hybrid skill retrieval
→ policy-visible candidates
→ structured selection
→ load selected skill content only
```

Требования:

- вся library не помещается в system prompt;
- selection сохраняет registry revision и reason/evidence;
- stale snapshot приводит к controlled rediscovery;
- один task получает bounded число skills;
- skill selection не создаёт рекурсивный бесконтрольный workflow;
- deterministic rules используются для очевидных system skills.

## v0.7.4-skill-runtime-integration

Skill подключается к `TaskRun` через композиционные extension points:

```text
ToolProvider
RuntimeProjectionProvider
ActionGuard
EvidenceContributor
LifecycleHook
ExecutorProfile contribution
```

System prompt собирается runtime из vetted/versioned blocks. Skill не передаёт
произвольный system prompt дочернему executor и не subclass-ит `AgentRuntime`.

`TaskContextManifest` фиксирует selected skill IDs/versions/revisions, чтобы
resume/replay использовали тот же snapshot либо controlled migration.

## v0.7.5-capability-and-trust

Effective capabilities:

```text
skill requirements
∩ principal permissions
∩ scope/instance policy
∩ executor profile policy
∩ task/run budget policy
```

Trust classes различают как минимум:

- builtin audited;
- instance-admin installed;
- user-provided;
- session-ephemeral.

Policy определяет доступ к:

- manager/MCP tools;
- artifact/content read/write;
- memory/retrieval;
- network;
- local process/sandbox execution;
- secrets references;
- side-effect classes.

До v0.9 skill с process/code requirement может быть disabled либо направлен в
ограниченный trusted local backend. Полный untrusted sandbox начинается в v0.9.

## v0.7.6-builtin-skills

Минимальный набор определяется после анализа реальных tasks. Возможные группы:

- research/source evaluation;
- coding/repository work;
- artifact/document processing;
- planning/workflow integration;
- memory/retrieval;
- domain-specific adapters.

Builtin skill не дублирует core safety/protocol и не превращает общую agent
архитектуру в набор неявных prompts.

Для каждого builtin skill нужны fixtures, selection tests, capability tests и
regression scenarios.

## v0.7.7-extension-platform-stabilization

Проверяются:

- package schema migration;
- registry snapshot replay;
- update/remove while tasks active;
- conflicts и duplicate identities;
- bounded context/token impact;
- malicious/prompt-injection content handling;
- negative capability tests;
- plugin API versioning;
- traces без raw secrets/content;
- local mode;
- preparation owner/principal context v0.8;
- preparation execution profiles v0.9.

## Non-goals v0.7

- полноценные accounts/auth UI;
- автоматическое доверие user-provided skill;
- произвольная установка OS packages в control plane;
- постоянный container на user/session;
- distributed runner fleet;
- загрузка всей skill library в каждый agent request.