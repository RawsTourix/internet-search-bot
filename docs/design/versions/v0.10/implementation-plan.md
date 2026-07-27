---
id: design.v0.10.implementation-plan
version: v0.10
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# Пошаговый план v0.10

## v0.10.1-runner-protocol

- versioned messages/API;
- runner identity/register/heartbeat;
- supported profiles/runtime/security features;
- capacity and active lease snapshot;
- accept/reject/cancel/status/result operations;
- protocol/profile compatibility negotiation;
- drain/maintenance state;
- mutual authentication и credential rotation;
- local fake runner для contract tests.

Acceptance: runner не может изменить owner, task contract или resource policy;
unknown protocol/profile version отклоняется управляемо.

## v0.10.2-placement-and-capacity

- runner registry/read model;
- allocatable/reserved capacity;
- sandbox profile matching;
- quota reservation;
- priority/deadline;
- data/image locality hints;
- maintenance/drain;
- affinity/anti-affinity;
- no-capacity/backpressure state;
- deterministic reason codes/metrics;
- bounded re-placement attempts.

Placement является control-plane policy и не выполняется произвольным LLM.

## v0.10.3-leases-and-fencing

- monotonic lease generation/fencing token;
- atomic assignment и runner acceptance;
- heartbeat renewal;
- expiry/loss transition;
- cancellation/termination;
- uncertain execution classification;
- retry/new attempt policy;
- stale status/result/output rejection;
- idempotent result proposal;
- cleanup acknowledgement.

Test: runner A с generation N теряет связь, runner B получает N+1, после чего A
не может commit или изменить terminal state.

## v0.10.4-remote-workspace

- object storage mandatory execution transport;
- scoped short-lived attempt credentials;
- exact manifest refs;
- streaming materialization;
- hash/size verification;
- temporary output namespace;
- upload-complete marker;
- control-plane validation/atomic commit;
- snapshot/restore;
- temporary/orphan object cleanup;
- revoked lease credential handling;
- locality/cache policy без shared tenant-writable state.

## v0.10.5-execution-backends

Runner adapters могут включать:

- Docker/OCI;
- Kubernetes Jobs/Pods;
- Nomad;
- gVisor-compatible runtime;
- microVM.

Для каждого backend:

- implementation v0.9 contracts;
- profile capability mapping;
- isolation/resource enforcement matrix;
- cancellation/timeout semantics;
- logs/output limits;
- orphan discovery;
- version compatibility;
- conformance suite.

Ни один backend не становится обязательной domain dependency.

## v0.10.6-distributed-security

- runner service identity и mTLS/token model;
- enrollment/revocation/rotation;
- scoped object credentials;
- no DB/Redis/LLM secrets;
- image/profile provenance/allowlist;
- isolation class attestation/feature reporting;
- network policy enforcement;
- tenant separation;
- output untrusted validation;
- audit run/task/attempt/runner/profile/image;
- quarantine/incident response;
- secret broker design только при доказанной необходимости.

Threat model включает compromised runner, replayed lease, forged heartbeat,
stolen scoped credential, malicious image/output и resource falsification.

## v0.10.7-observability-and-recovery

- fleet/runner health dashboard;
- distributed traces;
- placement/queue/start/materialization/upload/commit timings;
- lease expiry/lost runner/superseded attempt metrics;
- resource usage/reservation/accounting;
- reconciliation workers;
- durable vs runner-reported state diff;
- orphan workload/temp object detection;
- runbook для drain, upgrade, partial outage и stuck attempt;
- protocol/profile rollout compatibility;
- capacity forecasting и backpressure alerts.

## v0.10.8-distributed-execution-hardening

### Failure matrix

- runner dies before accept;
- dies after sandbox start;
- network partition with continued execution;
- output uploaded before lease expiry;
- stale result after retry;
- object storage partial outage;
- PostgreSQL/Redis partial outage;
- control-plane restart;
- protocol rolling upgrade;
- profile/image unavailable;
- duplicate messages;
- quota/capacity race;
- compromised/quarantined runner.

### Scale/security tests

- concurrent attempts/placement fairness;
- bounded queue/backpressure;
- cross-user isolation;
- stale fencing rejection;
- credential scope/revocation;
- runner spoof/replay;
- large workspace streaming;
- orphan cleanup;
- backend conformance.

### Release gate

- runner loss не теряет canonical task/run state;
- duplicate/superseded attempts не дают double commit;
- workspace восстанавливается на другом runner;
- placement соблюдает profiles/resources/quotas;
- control plane не зависит от локального Docker daemon;
- backend replacement не меняет AgentRuntime/TaskRuntime;
- object/result commit auditable и idempotent;
- local/self-hosted single-node mode v0.9 остаётся поддерживаемым.

## Допустимая параллельность

Runner protocol и fencing semantics проектируются до production placement.
Remote workspace и backend adapters могут развиваться параллельно после
стабилизации attempt/lease contract. Security и observability являются
сквозными работами с первого update, а не только последним hardening этапом.

## Non-goals

- обязательный multi-region deployment;
- обязательный Kubernetes;
- billing marketplace;
- перенос LLM provider execution на runner без отдельной спецификации;
- вечные per-user machines;
- shared writable host volumes как основной workspace transport.