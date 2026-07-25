---
id: design.decisions.index
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-07-25
---

# Architecture Decision Records

ADR используется для значимого решения, у которого есть альтернативы,
компромиссы или долгосрочные последствия.

ADR отвечает на вопрос «почему принято это решение». Канонический тематический
документ отвечает на вопрос «как теперь устроена система».

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
id: design.adr.0001
status: proposed
date: YYYY-MM-DD
affects:
  - design.v0.4.example
---

# ADR-0001: Название решения

## Контекст

## Рассмотренные варианты

## Решение

## Последствия

## Миграция канонической спецификации
```

После принятия ADR необходимо обновить перечисленные в `affects`
канонические документы. Нельзя оставлять новое правило только внутри ADR.
