---
id: design.v0.9.index
version: v0.9
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# v0.9 — Single-node isolated execution

Версия добавляет execution plane на одном runner-host: потенциально недоверенный
код, process/file operations и sandbox-required skills выполняются в ephemeral
изолированном environment через общий `ExecutionBackend`.

Основная модель:

```text
AgentRun
└── TaskRun
    ├── AgentCycle in trusted control plane
    └── ExecutionAttempt
        └── ephemeral SandboxInstance
```

Sandbox не является durable хранилищем, не живёт всё время conversation и не
получает unrestricted credentials control plane.

## Читать

1. [`isolated-execution.md`](isolated-execution.md) — control/execution plane,
   workspace, security и lifecycle contracts.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные updates
   и hardening gate.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.9.1-execution-contracts` | Neutral ExecutionBackend и attempts |
| 2 | `v0.9.2-sandbox-profiles` | Approved environments/capabilities/resources |
| 3 | `v0.9.3-workspace-materialization` | Exact inputs, logical paths и declared outputs |
| 4 | `v0.9.4-single-node-runner` | Sandbox Manager и container lifecycle |
| 5 | `v0.9.5-security-and-resource-policy` | Isolation, network и limits |
| 6 | `v0.9.6-lifecycle-and-recovery` | Cancellation, snapshots и orphan cleanup |
| 7 | `v0.9.7-sandbox-hardening` | Security/contract release gate |

## Зависимости

- [`../v0.6/README.md`](../v0.6/README.md) — TaskRun/jobs/object storage;
- [`../v0.7/README.md`](../v0.7/README.md) — capabilities/executor profiles;
- [`../v0.8/README.md`](../v0.8/README.md) — principal/ownership/quotas;
- [`../../dependency-rules.md`](../../dependency-rules.md) — execution port;
- [`../../release-gates.md`](../../release-gates.md) — sandbox gate.

Distributed runner fleet относится к [`../v0.10/README.md`](../v0.10/README.md).