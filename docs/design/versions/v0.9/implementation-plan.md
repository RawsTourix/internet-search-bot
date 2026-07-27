---
id: design.v0.9.implementation-plan
version: v0.9
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Пошаговый план v0.9

## v0.9.1-execution-contracts

- определить `ExecutionBackend`, requests/results/handles/errors;
- отделить `ExecutionAttempt` от `TaskRun` и job attempt;
- добавить idempotency/fencing-ready generation;
- реализовать trusted `LocalProcessExecutionBackend` для contract tests;
- запретить concrete Docker types в AgentRuntime/skills;
- определить cancellation, timeout, snapshot и teardown semantics.

Acceptance: local backend проходит deterministic contract suite, а TaskRuntime
вызывает execution только через port.

## v0.9.2-sandbox-profiles

- schema/version profile;
- approved runtime/image digest;
- commands/entrypoint/user;
- capabilities и supported skill requirements;
- CPU/RAM/PID/disk/output/default timeouts;
- network policy;
- package installation policy;
- profile lifecycle/deprecation;
- instance/admin authority для регистрации profiles.

Acceptance: произвольный image/privileged/mount из LLM request отклоняется.

## v0.9.3-workspace-materialization

- `WorkspaceManifest` и stable identity;
- exact content/artifact input refs;
- authorization/ownership check;
- relative logical paths;
- read-only inputs и separate temp/outputs;
- limits на count/bytes;
- declared outputs;
- regular-file/no-symlink validation;
- output upload/import/candidate/version flow;
- optional immutable workspace snapshot.

Существующий artifact workspace v0.4 обобщается, а не дублируется отдельным
несовместимым механизмом.

## v0.9.4-single-node-runner

- Sandbox Manager application/service boundary;
- Docker backend;
- create/start/exec/logs/stop/remove;
- instance IDs, labels и reconciliation metadata;
- per-attempt lease;
- controlled reuse within one TaskRun;
- idle timeout;
- cancellation;
- startup cleanup и periodic sweeper;
- health/readiness container runtime;
- Docker socket доступен только Sandbox Manager, не Gateway/AgentRuntime/sandbox.

Первый deployment может работать на одном host с остальными services, но
security boundary и API/port остаются отдельными.

## v0.9.5-security-and-resource-policy

- non-root/read-only/drop capabilities/no-new-privileges;
- seccomp/AppArmor/rootless runtime feasibility;
- CPU/memory/PID/disk/output limits;
- timeout/kill grace;
- network `none` default;
- egress allowlist/proxy для отдельных profiles;
- internal/private/metadata network block;
- no host mounts/socket/secrets;
- image digest/registry allowlist;
- per-principal quotas/reservations;
- stdout/stderr sanitization и truncation metadata;
- audit policy decision.

Threat model отдельно учитывает prompt injection, malicious package/setup,
resource exhaustion, data exfiltration и cross-user artifacts.

## v0.9.6-lifecycle-and-recovery

- durable attempt/instance/lease state;
- worker crash reconciliation;
- orphan containers/workspaces;
- lost response after successful execution;
- output upload/commit barrier;
- retry classes: safe retry, requires new sandbox, terminal;
- snapshot restore after idle teardown;
- cancellation before/while/after command;
- quota release/accounting;
- retention and cleanup;
- admin diagnostics без raw secrets/user content.

Terminal success допускается только после durable `ExecutionResult` и output refs.
Teardown может завершиться позже отдельным cleanup state.

## v0.9.7-sandbox-hardening

### Test matrix

- filesystem escape/symlink/hardlink;
- environment/secret inspection;
- fork bomb/PID exhaustion;
- memory/CPU/disk/output exhaustion;
- network private-range access;
- timeout/cancel races;
- container daemon access;
- cross-user input/output access;
- malicious archives/path traversal;
- duplicate commit/retry;
- orphan cleanup/restart;
- profile/image mismatch;
- local/Docker backend contract parity.

### Release gate

- sandbox не видит host project и infrastructure credentials;
- ownership verified до materialization;
- output imported до success/teardown;
- orphan runtime не влияет на canonical result;
- observability коррелирует run/task/attempt/instance;
- v0.10 может заменить local Sandbox Manager remote runner protocol без
  изменения ExecutionBackend caller.

## Допустимая параллельность

Contracts и profile schema проектируются первыми. Workspace materialization и
single-node runner могут реализовываться параллельно после стабилизации
`ExecutionRequest`. Security policy интегрируется с самого начала, а не только в
последнем hardening patch.

## Non-goals

- remote placement и fleet scheduler;
- Kubernetes/Firecracker mandatory backend;
- permanent package cache writable между разными users;
- передача database/LLM credentials sandbox;
- network enabled by default.