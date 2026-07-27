---
id: design.decisions.index
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-07-27
---

# Architecture Decision Records

ADR используется для значимого решения, у которого есть альтернативы,
компромиссы или долгосрочные последствия.

ADR отвечает на вопрос «почему принято это решение». Канонический тематический
документ отвечает на вопрос «как теперь устроена система».

## Принятые решения

| ADR | Решение |
|---|---|
| [`ADR-0001`](ADR-0001-modular-monolith-before-microservices.md) | Сначала модульный монолит и стабильные contracts, затем обоснованные services |
| [`ADR-0002`](ADR-0002-agent-runtime-composition.md) | `AgentRuntime` и composition вместо роста production inheritance chain |
| [`ADR-0003`](ADR-0003-control-and-execution-plane.md) | Trusted control plane и ephemeral isolated execution plane |

## Именование

```text
ADR-0001-short-kebab-case-title.md
```

## Статусы ADR

- `proposed`;
- `accepted`;
- `rejected`;
- `superseded`.

## Шаблон

```markdown
---
id: design.adr.0004
status: proposed
date: YYYY-MM-DD
affects:
  - design.v0.4.example
---

# ADR-0004: Название решения

## Контекст

## Рассмотренные варианты

## Решение

## Последствия

## Миграция канонической спецификации
```

После принятия ADR необходимо обновить перечисленные в `affects` канонические
документы. Нельзя оставлять новое правило только внутри ADR.