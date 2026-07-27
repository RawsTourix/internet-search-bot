---
id: design.versions.index
version: cross-version
spec_status: summary
implementation_status: mixed
last_reviewed: 2026-07-27
---

# Версии архитектуры

Каждая папка версии содержит индекс и тематические спецификации своего этапа.
Для определения применимой версии сначала прочитайте
[`../current.md`](../current.md).

| Версия | Архитектурный этап | Индекс |
|---|---|---|
| `v0.3` | Agent loop baseline | [`v0.3/`](v0.3/README.md) |
| `v0.4` | Agent workspace, compaction, planning, files, input runtime и modularization | [`v0.4/`](v0.4/README.md) |
| `v0.5` | PostgreSQL, indexing и RAG | [`v0.5/`](v0.5/README.md) |
| `v0.6` | Distributed runtime и workflow orchestration | [`v0.6/`](v0.6/README.md) |
| `v0.7` | Skills и extension platform | [`v0.7/`](v0.7/README.md) |
| `v0.8` | Identity, authorization и multi-user workspace | [`v0.8/`](v0.8/README.md) |
| `v0.9` | Single-node isolated execution | [`v0.9/`](v0.9/README.md) |
| `v0.10` | Distributed execution plane | [`v0.10/`](v0.10/README.md) |

README версии является навигационным слоем и каноническим реестром updates.
Подробные контракты находятся в перечисленных тематических документах.

## Именование updates

Исторические идентификаторы v0.3 и v0.4 не переименовываются.

Начиная с v0.5, именованное implementation update использует формат:

```text
v<major>.<minor>.<sequence>-<descriptive-slug>
```

Примеры:

```text
v0.5.1-postgresql-foundation
v0.5.2-repository-backends
v0.6.3-task-runtime
```

Правила:

1. `sequence` начинается с `1` и уникален внутри основной версии.
2. Число отражает рекомендуемый порядок реализации, но dependencies остаются
   каноническим источником допустимой параллельности.
3. После `spec_status: accepted`, `implementation_status: partial` или появления
   реализации идентификатор не переименовывается.
4. Вставка нового этапа не должна массово переименовывать начатые updates: новый
   этап получает следующий свободный номер и явные dependencies.
5. Slug описывает устойчивый архитектурный результат, а не временное действие
   `new`, `improved`, `final` или `advanced`.
6. Внутренние шаги не получают обязательную нумерацию вида `v0.5.1.1`.

## Рекомендуемый frontmatter update

```yaml
---
id: design.v0.5.1.postgresql-foundation
version: v0.5
update: v0.5.1-postgresql-foundation
sequence: 1
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---
```

Архитектурный overview версии не притворяется implementation update и может
использовать `document_role: architecture-overview`.

## Развёртывание крупного update

Сначала update может быть разделом в `implementation-plan.md`. Перед началом
реализации или при росте контракта его разрешено развернуть в отдельный файл или
папку с README. Идентификатор, sequence и смысл update при этом сохраняются.