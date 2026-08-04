# Документация проекта

Архитектурная документация ИИ-агента находится в каталоге
[`design/`](design/README.md).

## Начальная точка

1. [`AGENTS.md`](AGENTS.md) — стабильные правила для ИИ-агентов, работающих с
   документацией.
2. [`design/README.md`](design/README.md) — карта документации и правила чтения.
3. [`design/current.md`](design/current.md) — текущий архитектурный baseline.
4. [`design/principles.md`](design/principles.md) — архитектурные инварианты.
5. README нужной версии в [`design/versions/`](design/versions/).
6. [`design/contracts/`](design/contracts/README.md) — сквозные контракты на
   границе агента и внешних компонентов.

Для изменений, затрагивающих отдельный сервис или внешний runtime, сначала
прочитайте применимый contract, затем version-specific документ стороны агента.
Внутренняя архитектура внешнего сервиса должна оставаться в его собственном
репозитории.

`design_document.md` сохраняется как compatibility entrypoint. Каноническая
структура, статусы, contracts и version indexes находятся в `design/`.