---
id: design.v0.9.isolated-execution
version: v0.9
document_role: architecture-overview
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Архитектура single-node isolated execution

## Цель

Предоставить агенту безопасную среду Python/shell/file execution, не помещая
недоверенный код в Gateway, Agent Runtime worker или host project filesystem.

## Trust boundary

```text
Trusted control plane
├── AgentRuntime
├── PostgreSQL/Redis access
├── LLM and tool gateways
├── authorization and quotas
├── scheduler/workers
└── Sandbox Manager
        ↓ validated ExecutionRequest
Untrusted execution plane
└── ephemeral sandbox
```

Control plane не передаёт sandbox:

- database URL/credentials;
- Redis credentials;
- LLM provider keys;
- Docker/container daemon socket;
- unrestricted object-storage credentials;
- host project directory.

## Что выполняется в sandbox

- generated/user-provided Python или shell commands;
- coding/data-analysis tasks;
- skill execution с process capability;
- file conversion/processing с повышенным trust risk;
- bounded package/tool operations, если профиль это разрешает.

Обычный LLM reasoning, exact retrieval и безопасный remote MCP call не обязаны
создавать sandbox.

## Lifecycle и привязка

Sandbox обычно привязан к `TaskRun` или `ExecutionAttempt`, а не к account,
conversation или всей session.

```text
TaskRun starts
→ create/claim sandbox
→ materialize exact workspace inputs
→ execute bounded requests
→ collect declared outputs
→ optional workspace snapshot
→ commit ExecutionResult
→ terminate sandbox
```

Для интерактивной coding task один sandbox может использоваться несколькими
commands одного active TaskRun. Idle timeout приводит к snapshot/teardown;
следующий run получает новое environment.

## Execution contracts

```python
class ExecutionBackend(Protocol):
    async def create(self, spec: SandboxSpec) -> ExecutionHandle: ...
    async def execute(
        self,
        handle: ExecutionHandle,
        request: ExecutionRequest,
    ) -> ExecutionResult: ...
    async def snapshot(
        self,
        handle: ExecutionHandle,
    ) -> WorkspaceSnapshotRef: ...
    async def terminate(
        self,
        handle: ExecutionHandle,
        reason: str,
    ) -> None: ...
```

Initial adapters:

```text
LocalProcessExecutionBackend   trusted development/testing only
DockerExecutionBackend         isolated production/default single-node path
```

AgentRuntime и skills не зависят от Docker SDK.

## SandboxSpec

Минимальные поля:

```text
execution_attempt_id
run_id / task_run_id
principal/account scope
sandbox_profile_id + version/digest
workspace_manifest_id
CPU / memory / PID / disk / output limits
timeout and idle timeout
network policy
allowed capabilities
secret reference policy (normally none)
```

## Sandbox profiles

Profile является server-approved manifest, например:

```text
python-basic
python-data
node-basic
document-converter
browser-automation
```

Он связывается с immutable image digest и policy. LLM/skill не передаёт
произвольный image name, privileged flag или host mount.

## Workspace materialization

`WorkspaceManifest` содержит logical relative paths и exact refs:

```json
{
  "inputs": [
    {
      "artifact_id": "art_...",
      "mount_path": "inputs/report.pdf",
      "read_only": true
    }
  ],
  "outputs": [
    {
      "relative_path": "outputs/result.csv",
      "required": false
    }
  ]
}
```

Flow:

```text
verify principal/scope
→ resolve exact content/artifact versions
→ materialize read-only inputs
→ create isolated writable temp/output
→ execute
→ inspect only declared regular non-symlink outputs
→ upload/import immutable content
→ create artifact candidates/versions
```

Physical host paths не попадают в LLM/task contracts.

## Security policy

Минимум:

- non-root user;
- read-only root filesystem;
- writable workspace only;
- dropped Linux capabilities;
- `no-new-privileges`;
- seccomp/AppArmor или эквивалент;
- bounded CPU/RAM/PIDs/disk/output;
- wall-clock/idle timeout;
- network disabled by default;
- no host network/PID namespace;
- no arbitrary host mounts;
- no container daemon socket;
- immutable image digest;
- sanitized stdout/stderr limits;
- declared outputs only.

Network-required profiles используют controlled egress proxy/allowlist и
блокируют private/internal/metadata ranges.

## Secrets

Default: secrets отсутствуют. В будущем разрешён только short-lived scoped
credential broker для конкретной capability. Raw secret не сохраняется в
manifest, trace, logs, snapshot или artifact.

## Durable state

PostgreSQL хранит:

```text
sandbox_profiles
execution_attempts
sandbox_instances
sandbox_leases
workspace_manifests/snapshots
resource_usage
execution results/errors
```

Container runtime state является наблюдаемым фактом, но не canonical application
state.

## Failure model

- create failure → attempt retry/classification;
- command timeout → terminate process/sandbox и persist timeout result;
- worker crash → sweeper reconciles lease/runtime;
- output upload failure → attempt не становится succeeded;
- teardown failure → result сохраняется, instance помечается orphan cleanup;
- ambiguous commit → idempotency key/fencing generation предотвращает duplicate
  artifact version/result.

## Non-goals

- полноценная VM isolation guarantee;
- distributed placement;
- permanent user environment;
- arbitrary internet access;
- прямое выполнение AgentRuntime/DB внутри sandbox;
- Kubernetes как обязательная dependency.