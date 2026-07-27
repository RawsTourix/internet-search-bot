---
id: design.main
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-07-27
---

# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.10

Compatibility entrypoint архитектуры, версий и именованных обновлений проекта.
Каноническая карта документации находится в
[`design/README.md`](design/README.md).

## Текущая архитектура

- [`design/current.md`](design/current.md) — применимый baseline.
- [`design/principles.md`](design/principles.md) — архитектурные инварианты.
- [`design/architecture-evolution.md`](design/architecture-evolution.md) — путь
  к services и execution plane.
- [`design/dependency-rules.md`](design/dependency-rules.md) — правила ports и
  зависимостей.
- [`design/release-gates.md`](design/release-gates.md) — критерии завершения.
- [`design/glossary.md`](design/glossary.md) — термины.

## Версии и обновления

| Версия | Индекс именованных обновлений |
|---|---|
| `v0.3` | [`design/versions/v0.3/README.md`](design/versions/v0.3/README.md) |
| `v0.4` | [`design/versions/v0.4/README.md`](design/versions/v0.4/README.md) |
| `v0.5` | [`design/versions/v0.5/README.md`](design/versions/v0.5/README.md) |
| `v0.6` | [`design/versions/v0.6/README.md`](design/versions/v0.6/README.md) |
| `v0.7` | [`design/versions/v0.7/README.md`](design/versions/v0.7/README.md) |
| `v0.8` | [`design/versions/v0.8/README.md`](design/versions/v0.8/README.md) |
| `v0.9` | [`design/versions/v0.9/README.md`](design/versions/v0.9/README.md) |
| `v0.10` | [`design/versions/v0.10/README.md`](design/versions/v0.10/README.md) |

Начиная с v0.5, именованные implementation updates используют формат
`v<major>.<minor>.<sequence>-<slug>`. Подробные правила навигации, каноничности и
выбора контекста находятся в [`design/README.md`](design/README.md).