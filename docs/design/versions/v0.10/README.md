---
id: design.v0.10.index
version: v0.10
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# v0.10 — Distributed execution plane

Версия масштабирует v0.9 Sandbox Manager с одного host до fleet runner-узлов.
Control plane планирует execution по profiles/capacity/quotas, а remote runners
материализуют workspace, запускают sandbox и возвращают immutable results.

```text
Control plane
├── Agent/Workflow runtime
├── Placement Scheduler
├── PostgreSQL / Redis / Object Storage
└── Sandbox Manager API
        ↓ leases + runner protocol
Execution plane
├── Runner A → sandbox attempts
├── Runner B → sandbox attempts
└── Runner C → sandbox attempts
```

## Читать

1. [`distributed-execution.md`](distributed-execution.md) — runner protocol,
   placement, leases, remote workspace и security model.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные updates
   и distributed hardening gate.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.10.1-runner-protocol` | Versioned runner registration/heartbeat/execution API |
| 2 | `v0.10.2-placement-and-capacity` | Profile/resource/quota-aware scheduling |
| 3 | `v0.10.3-leases-and-fencing` | Safe ownership attempts и stale commit rejection |
| 4 | `v0.10.4-remote-workspace` | Object-storage materialization и upload barrier |
| 5 | `v0.10.5-execution-backends` | Docker/Kubernetes/gVisor/microVM adapters |
| 6 | `v0.10.6-distributed-security` | Runner identity, credentials и isolation classes |
| 7 | `v0.10.7-observability-and-recovery` | Fleet health, tracing и reconciliation |
| 8 | `v0.10.8-distributed-execution-hardening` | Failure/scale/security release gate |

## Зависимости

- [`../v0.9/README.md`](../v0.9/README.md) — execution contracts и sandbox policy;
- [`../v0.8/README.md`](../v0.8/README.md) — principal/quotas;
- [`../v0.6/README.md`](../v0.6/README.md) — jobs/workflows/object storage;
- [`../../architecture-evolution.md`](../../architecture-evolution.md) —
  control/execution plane evolution.

Kubernetes, gVisor или microVM являются adapters/implementation choices, а не
частью domain contract версии.