---
id: design.index
version: cross-version
spec_status: accepted
implementation_status: not-applicable
last_reviewed: 2026-08-02
---

# Архитектура ИИ-агента

Это единственная точка входа в дизайн-документацию проекта. Документы
организованы по версиям и архитектурным темам так, чтобы для анализа не
требовалось загружать всю историю проекта.

Инструкции для ИИ-агентов, изменяющих документацию, находятся в
[`../AGENTS.md`](../AGENTS.md).

## Быстрый старт

Для общего анализа читайте:

1. [`current.md`](current.md) — что считается текущим baseline.
2. [`overview.md`](overview.md) — назначение и направление развития системы.
3. [`principles.md`](principles.md) — каталог архитектурных инвариантов.
4. [`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md) —
   границы AgentRuntime, Service Application, self-hosted/managed hosting и
   будущего Local Agent Application.
5. [`architecture-evolution.md`](architecture-evolution.md) — путь от модульного
   runtime к distributed execution plane.
6. README интересующей версии.
7. Применимый документ в [`contracts/`](contracts/README.md), если задача
   затрагивает внешний сервис или protocol boundary.

Не начинайте анализ с roadmap или history: эти документы дают хронологический
контекст, но не заменяют тематические спецификации.

## Версии

| Версия | Статус спецификации | Статус реализации | Индекс |
|---|---|---|---|
| `v0.3` | accepted | implemented baseline | [`versions/v0.3/`](versions/v0.3/README.md) |
| `v0.4` | accepted | partial | [`versions/v0.4/`](versions/v0.4/README.md) |
| `v0.5` | draft | planned | [`versions/v0.5/`](versions/v0.5/README.md) |
| `v0.6` | draft | planned | [`versions/v0.6/`](versions/v0.6/README.md) |
| `v0.7` | draft | planned | [`versions/v0.7/`](versions/v0.7/README.md) |
| `v0.8` | draft | planned | [`versions/v0.8/`](versions/v0.8/README.md) |
| `v0.9` | draft | planned | [`versions/v0.9/`](versions/v0.9/README.md) |
| `v0.10` | draft | planned | [`versions/v0.10/`](versions/v0.10/README.md) |

Статус реализации является навигационным. Для release-решений его необходимо
проверять по коду, миграциям и тестам.

## Как работать со списком обновлений

1. Откройте README нужной основной версии.
2. Найдите идентификатор обновления.
3. Перейдите в указанную тематическую спецификацию или implementation plan.
4. Если обновление стало достаточно крупным, оно может быть представлено папкой
   с собственным README без изменения стабильного идентификатора.

Исторические имена v0.3 и v0.4 сохраняются. Начиная с v0.5, именованные
implementation updates используют формат:

```text
v<major>.<minor>.<sequence>-<descriptive-slug>
```

Например: `v0.5.1-postgresql-foundation` или `v0.6.3-task-runtime`.
Подробные правила находятся в [`versions/README.md`](versions/README.md).

## Сквозные документы

| Документ | Назначение |
|---|---|
| [`current.md`](current.md) | Текущий baseline и граница между существующим и будущим |
| [`overview.md`](overview.md) | Цели развития архитектуры `v0.3 → v0.10` |
| [`principles.md`](principles.md) | Каталог инвариантов разных этапов развития |
| [`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md) | Application profiles, hosting modes, configuration ownership и transport admission |
| [`architecture-evolution.md`](architecture-evolution.md) | Этапы модульного, сервисного и execution-plane развития |
| [`dependency-rules.md`](dependency-rules.md) | Допустимое направление imports и ports/adapters |
| [`release-gates.md`](release-gates.md) | Универсальные и version-specific критерии завершения |
| [`glossary.md`](glossary.md) | Канонические значения основных терминов |
| [`roadmap.md`](roadmap.md) | Хронологическая сводка; не источник детальных контрактов |
| [`contracts/`](contracts/README.md) | Сквозные интеграционные контракты агента с внешними компонентами |
| [`decisions/`](decisions/README.md) | Правила ведения ADR |

## Рекомендуемые наборы контекста

| Задача | Что читать |
|---|---|
| Анализ текущего agent loop | `current.md` → `v0.3/README.md` → memory, pending cycle, progress и MCP runtime |
| Runtime/deployment profiles | `runtime-and-deployment-profiles.md` → architecture evolution → modularization → нужная version specification |
| Context management v0.4 | `v0.4/README.md` → storage → result compaction → cycle compaction |
| DAG planning | `v0.4/README.md` → storage → DAG planning |
| Файлы, input и delivery | `v0.4/README.md` → unified input/artifact → file artifacts → semantic interaction → output delivery → input runtime |
| Рефакторинг runtime | `runtime-and-deployment-profiles.md` → `v0.4/README.md` → `v0.4-runtime-modularization/` → dependency rules |
| Builtin MCP-сервисы | `runtime-and-deployment-profiles.md` → `contracts/builtin-mcp-service-contract.md` → `versions/v0.4/v0.4-mcp-registry-foundation/` → dependency rules |
| PostgreSQL и RAG | `v0.5/README.md` → architecture overview → implementation plan |
| Distributed runtime | `runtime-and-deployment-profiles.md` → `v0.6/README.md` → v0.5 persistence → implementation plan |
| Skills | `v0.7/README.md` → v0.6 task runtime → implementation plan |
| Identity и multi-user | `runtime-and-deployment-profiles.md` → `v0.8/README.md` → ownership-ready persistence/runtime/skills |
| Isolated execution | `runtime-and-deployment-profiles.md` → `v0.9/README.md` → v0.6 TaskRun → v0.7 capabilities → v0.8 authorization |
| Distributed runners | `v0.10/README.md` → v0.9 execution contracts и hardening |

## Правила каноничности

1. README основной версии является каноническим реестром именованных
   обновлений и их порядка.
2. Для одной архитектурной темы внутри обновления существует один канонический
   тематический файл.
3. README версии определяет порядок чтения и область действия, но не
   переопределяет подробные контракты тематического файла.
4. `contracts/` владеет cross-version требованиями интеграционной границы, а
   version-specific документ — реализацией стороны агента.
5. `runtime-and-deployment-profiles.md` владеет различием application profile,
   hosting mode, topology и execution backend; version-specific документы
   применяют эту модель, но не переопределяют её молча.
6. `roadmap.md` описывает последовательность развития и не является второй
   спецификацией.
7. ADR объясняет причину решения. После принятия решения актуальное состояние
   интегрируется в канонический тематический файл.
8. Документы со статусом `historical` или `superseded` не участвуют в обычном
   архитектурном анализе.
9. Более новый файл не уточняет предыдущий неявно. Изменение ограничивается
   версией либо оформляется явным `supersedes`.
10. При конфликте сначала применяется версия из `current.md`, затем
    канонический тематический документ этой версии. Конфликт между двумя
    каноническими файлами считается дефектом документации.

## Статусы

`spec_status`:

- `accepted` — утверждённая спецификация;
- `draft` — рабочий проект будущей версии;
- `provisional` — предварительная концепция;
- `summary` — навигационная или хронологическая сводка;
- `historical` — описание реализованной истории;
- `superseded` — неактуальный документ, сохранённый только для контекста.

`implementation_status`:

- `implemented` — решение отражено в коде и подтверждено тестами;
- `partial` — реализована или стабилизирована только часть контура;
- `planned` — реализация запланирована;
- `mixed` — документ охватывает несколько версий;
- `not-applicable` — статус реализации неприменим.

## Правила изменения документации

- Один Markdown-файл отвечает за одну архитектурную ответственность.
- В каждом файле один заголовок `# H1`.
- Детали не копируются в README: используется краткое резюме и ссылка.
- Новый крупный патч обновляет существующий канонический файл либо создаёт новую
  ясно названную тему.
- Внутренние шаги update не получают обязательную нумерацию `v0.x.y.z`;
  dependencies и допустимая параллельность задаются implementation plan.
- Fenced blocks применяются для кода, схем и форматозависимых flow, а не как
  замена обычным Markdown-спискам.
- Каждый новый документ должен быть достижим из README своей версии, из
  `contracts/README.md` или из таблицы сквозных документов.
- Каждая основная версия заканчивается stabilization/hardening update и
  проверяется по [`release-gates.md`](release-gates.md).
