---
id: design.adr.0003
status: accepted
date: 2026-07-27
affects:
  - design.architecture-evolution
  - design.principles
  - design.v0.9.isolated-execution
  - design.v0.10.distributed-execution
---

# ADR-0003: Разделение control plane и execution plane

## Контекст

Будущий агент должен запускать Python, shell, file processors и skills с
process capabilities. Выполнение такого кода в Gateway/Agent worker создаёт
риск доступа к host filesystem, provider/database credentials и другим users.

Постоянный container на каждого account/session расходует ресурсы и смешивает
durable user state с временным execution environment.

## Рассмотренные варианты

1. Выполнять команды непосредственно в Agent Runtime process.
2. Создавать постоянный container/VM на user или conversation.
3. Оставить durable state и orchestration в trusted control plane, а для
   `TaskRun`/`ExecutionAttempt` создавать ephemeral sandbox через
   `ExecutionBackend`.

## Решение

Выбран вариант 3.

Control plane содержит auth, policies, durable state, scheduler, LLM/tool
gateways и Sandbox Manager. Execution plane получает только validated
`ExecutionRequest`, scoped workspace inputs, profile и resource/network policy.

Sandbox:

- не получает DB/Redis/LLM/container-daemon credentials;
- не является source of truth;
- привязывается к task/attempt, а не навсегда к user/session;
- сохраняет outputs/snapshot до teardown;
- имеет network denied by default.

В v0.9 execution plane работает на одном host. В v0.10 тот же contract
расширяется remote runners, leases и fencing.

## Последствия

Положительные:

- ограничивается blast radius недоверенного кода;
- local/container/remote backends используют один port;
- durable state переживает teardown/loss runner;
- resources и quotas учитываются per attempt.

Отрицательные:

- появляются materialization/upload latency и lifecycle states;
- isolation требует отдельного threat model и hardening;
- Docker container сам по себе не считается абсолютной security guarantee;
- distributed mode требует identity, leases, fencing и reconciliation.

## Миграция канонической спецификации

Решение отражено в `architecture-evolution.md`, `principles.md`, v0.9 isolated
execution и v0.10 distributed execution.