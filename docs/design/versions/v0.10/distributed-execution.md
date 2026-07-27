---
id: design.v0.10.distributed-execution
version: v0.10
document_role: architecture-overview
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Архитектура distributed execution plane

## Цель

Выполнять sandbox workloads на нескольких runner-узлах без зависимости control
plane от локального Docker daemon и без потери canonical state при исчезновении
runner.

## Компоненты

### Sandbox Manager control plane

- принимает validated execution intents;
- хранит attempt/lease/fencing state;
- выбирает/координирует runner через placement layer;
- выдаёт short-lived execution credentials;
- reconciles heartbeats и orphan workloads;
- commit-ит canonical result/output refs.

### Placement Scheduler

Выбирает runner с учётом:

```text
sandbox profile and runtime class
requested CPU/RAM/disk/GPU when introduced
available and reserved capacity
principal/tenant quota
security/isolation class
image/profile availability
workspace/data locality
priority/deadline
maintenance/drain state
affinity/anti-affinity
```

### Runner agent

- регистрируется и сообщает capabilities;
- принимает только valid lease/fencing generation;
- materializes workspace через scoped access;
- запускает local sandbox backend;
- отправляет heartbeat/status/usage;
- загружает outputs;
- предлагает result commit;
- cleans local runtime после acknowledgement/timeout.

Runner не является scheduler и не меняет workflow/task state напрямую.

## Runner protocol

Versioned protocol включает:

```text
register / renew identity
heartbeat and capacity snapshot
claim/accept/reject execution lease
execution status and progress
cancel/terminate
authenticated output upload
result proposal/commit acknowledgement
drain/maintenance
protocol/profile compatibility
```

Registration сообщает:

```text
runner_id
protocol version
supported sandbox profiles/versions
runtime/backend classes
capacity and allocatable resources
image/profile cache metadata
security/isolation features
active lease IDs
timestamps/health
```

## Lease и fencing

```text
execution queued
→ placement selects runner
→ lease generation N issued
→ runner accepts N
→ sandbox starts/runs
→ outputs uploaded
→ result proposal with N
→ control plane verifies N/current state
→ atomic result commit
→ cleanup acknowledgement
```

При heartbeat expiry:

```text
lease N invalidated
→ attempt marked lost/uncertain
→ retry policy
→ lease generation N+1 on another runner
```

Result от generation N после выдачи N+1 отклоняется. Это предотвращает
split-brain double commit даже если старый runner восстановил связь.

## Remote workspace

Общий local filesystem отсутствует.

```text
WorkspaceManifest with exact refs
→ short-lived scoped download authorization
→ runner streams and verifies hash/size
→ local read-only materialization
→ execution
→ outputs upload to temporary object keys
→ control plane validates/commits immutable refs
→ temporary cleanup
```

Object storage хранит payload, PostgreSQL — owner, relations, hashes, manifest,
lease и commit state.

Runner credential:

- short-lived;
- ограничен одним attempt/workspace;
- разрешает только объявленные objects/operations;
- не раскрывает account-wide bucket access;
- revocable после lease invalidation.

## Result commit barrier

Attempt не становится succeeded, пока control plane атомарно не подтвердил:

- current fencing generation;
- complete execution result metadata;
- uploaded output existence/hash/size;
- quota/resource usage record;
- declared output policy;
- task/run state allows commit.

После commit duplicate proposal возвращает existing result idempotently.

## Execution backends

Runner может использовать:

```text
Docker/OCI
Kubernetes Job/Pod
Nomad allocation
gVisor runtime
microVM/Firecracker-like backend
```

Backend реализует v0.9 `ExecutionBackend` и runner-local policy. Control plane
работает с profile/capability contract, а не с vendor-specific fields.

## Security

### Runner identity

- unique machine/service identity;
- mutual authentication;
- certificate/token rotation;
- explicit revocation;
- protocol authorization;
- no trust based only on runner-supplied ID.

### Isolation classes

Profiles могут требовать разные classes:

```text
trusted-worker
container-isolated
enhanced-sandbox (gVisor-like)
microVM-isolated
```

Placement не запускает workload на runner, который не подтверждает required
class/profile/version.

### Compromised runner model

Runner считается менее доверенным, чем control plane:

- не получает DB/Redis/LLM secrets;
- scoped object credentials минимальны;
- output считается untrusted до validation;
- audit хранит runner/attempt/image/profile identities;
- runner не определяет owner или authorization;
- unusual usage/attestation failure может quarantine runner;
- secrets-required task использует отдельный broker/policy либо запрещается.

## Recovery и reconciliation

Control plane периодически сравнивает:

- durable leases/attempts;
- runner heartbeat active leases;
- container/orchestrator workloads;
- temporary object uploads;
- terminal results и cleanup acknowledgements.

Классы состояний:

```text
running and healthy
runner lost
lease expired
execution unknown
output uploaded but not committed
result committed but cleanup pending
orphan workload
orphan temporary objects
```

Каждый класс имеет deterministic recovery/retention policy.

## Observability

Корреляция:

```text
request_id
run_id
workflow_revision
task_run_id
execution_attempt_id
lease_id/generation
runner_id
sandbox_instance_id
profile/image digest
```

Метрики:

- queue/placement latency;
- cold start/image pull;
- materialization/upload time;
- execution time и resource usage;
- lease expirations/lost runners;
- retry/superseded attempts;
- capacity/reservation/utilization;
- result commit/cleanup latency;
- policy/security rejections.

## Non-goals

- обязательный конкретный orchestrator;
- перенос workflow scheduler внутрь runner;
- shared writable filesystem между tenants;
- вечные user VMs;
- доверие output только потому, что runner вернул success;
- automatic cross-region architecture без отдельной необходимости.