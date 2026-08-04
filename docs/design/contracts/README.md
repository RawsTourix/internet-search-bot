---
id: design.contracts.index
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-08-01
---

# Интеграционные контракты

Каталог содержит сквозные контракты на границе ИИ-агента и внешних компонентов.
Они задают обязательную семантику интеграции, но не описывают внутреннюю
архитектуру, технологический стек или deployment конкретного внешнего сервиса.

## Правила

- Contract является cross-version source of truth для одной интеграционной
  границы.
- Version-specific документ описывает реализацию стороны агента и ссылается на
  применимый contract.
- Внутренняя реализация отдельного сервиса документируется в репозитории этого
  сервиса.
- Contract не заменяет implementation plan конкретной версии.
- Изменение contract требует проверки совместимости всех version specifications,
  которые на него ссылаются.

## Контракты

| Документ | Назначение |
|---|---|
| [`builtin-mcp-service-contract.md`](builtin-mcp-service-contract.md) | Требования к встроенным MCP-сервисам и границе их интеграции с Agent Runtime |
