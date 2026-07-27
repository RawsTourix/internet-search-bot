---
id: design.adr.0001
status: accepted
date: 2026-07-27
affects:
  - design.architecture-evolution
  - design.dependency-rules
  - design.v0.4.runtime-modularization
  - design.v0.6.implementation-plan
---

# ADR-0001: Модульный монолит до микросервисов

## Контекст

Проект растёт от одного agent loop к persistence, workers, skills, multi-user и
isolated execution. Преждевременное выделение каждого package/repository в
network service увеличит deployment, transaction, debugging и compatibility
сложность до стабилизации contracts.

Одновременно сохранение всей логики в одном orchestration class блокирует
переход к workers и independent security boundaries.

## Рассмотренные варианты

1. Сразу разделить проект на множество микросервисов.
2. Оставить один неструктурированный монолит до появления нагрузки.
3. Создать модульный монолит с ports, затем выделять процессы/сервисы по
   подтверждённой operational необходимости.

## Решение

Выбран вариант 3.

Сначала внутри одного repository/application стабилизируются bounded modules,
application services, ports, transaction boundaries и local implementations.

Далее физическое разделение происходит постепенно:

```text
v0.4–v0.5: modular monolith
v0.6: Gateway + Agent/Background/Delivery workers
v0.9: separate Sandbox Manager security boundary
v0.10: remote runner fleet
```

Подсистема выделяется в service только при независимом scaling, lifecycle,
security/failure domain или deployment requirement.

## Последствия

Положительные:

- local development остаётся простым;
- contracts тестируются до network boundary;
- меньше distributed transactions;
- будущие adapters заменяются без rewrite domain runtime.

Отрицательные:

- требуется строгая дисциплина imports и composition root;
- один repository может содержать несколько deployable entrypoints;
- некоторое время code ownership и deployment boundary не совпадают один к одному.

## Миграция канонической спецификации

Решение отражено в:

- `architecture-evolution.md`;
- `dependency-rules.md`;
- `v0.4-runtime-modularization`;
- v0.6 service-boundary stabilization.