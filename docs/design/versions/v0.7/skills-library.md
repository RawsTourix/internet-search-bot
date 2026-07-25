---
id: design.v0.7.skills-library
version: v0.7
spec_status: provisional
implementation_status: planned
---
# Часть XI. v0.7 — предварительная концепция Skills Library

> **Статус раздела:** предварительная архитектурная концепция. Это не готовое ТЗ
> и не утверждённый набор промежуточных релизов. Точный формат skills, registry,
> selection policy и execution contracts следует проектировать после
> стабилизации `v0.6`.

## 135. Главная идея v0.7

`v0.7` добавляет расширяемую библиотеку декларативных навыков, позволяющую
агенту применять специализированные workflows для отдельных классов задач без
разрастания core system prompt и без жёсткого встраивания каждой методики в код.

```text
устойчивый Agent Runtime + RAG + workflow orchestration
→ выбор подходящего skill для конкретной task
→ загрузка bounded instructions
→ task-local DAG и выполнение
```

Core runtime отвечает на вопрос «как безопасно выполнять задачи вообще».
Skill описывает «как качественно выполнять конкретный класс задач».

Простые запросы не должны проходить обязательный skill-selection ceremony.
Если базового agent loop достаточно, задача выполняется без skill.

---

## 136. Skill как декларативный модуль поведения

Skill не сводится к произвольному prompt fragment. Предварительно он может
содержать:

- назначение и область применимости;
- краткое описание для retrieval;
- пошаговый workflow или authoring rules;
- ограничения и safety notes;
- declarative required capabilities/tools;
- expected inputs и outputs;
- критерии проверки результата;
- примеры и дополнительные resources.

Возможная файловая форма:

```text
skills/<skill-slug>/
  skill.md
  examples/
  resources/
```

`skill.md` может использовать YAML frontmatter:

```yaml
---
name: architecture-analysis
version: 1.0.0
description: Анализ архитектуры программного проекта
tags: [architecture, codebase, analysis]
required_tools: [repository_read]
execution_mode: workflow
---
```

Markdown остаётся человекочитаемым source, а metadata позволяет registry
валидировать, индексировать и выбирать skill программно. Точная schema не
фиксируется до отдельного проектирования `v0.7`.

---

## 137. Skill Registry и scopes

Skill storage не должен быть прямой зависимостью agent loop. Нужен
`SkillRegistry`/`SkillService`, скрывающий filesystem, PostgreSQL или другой
backend.

Предварительные scopes унифицируются с MCP registry:

```text
builtin
  поставляется вместе с проектом, versioned и tested

instance
  установлен администратором deployment

user
  принадлежит конкретному account

session
  временно подключён к одной conversation/run boundary
```

Для local development допускается приватная пользовательская папка, добавленная
в `.gitignore`. В self-hosted/multi-user mode metadata и ownership могут храниться
в PostgreSQL, а files/resources — на filesystem/object storage.

Registry должен предоставлять compact metadata, exact version/content hash,
enabled state, source/trust level и capability requirements.

---

## 138. Поиск и загрузка skills

Полное содержимое всех skills нельзя помещать в system prompt. Предварительный
двухэтапный механизм:

```text
1. Compact index
   name, description, tags, scope, version, required capabilities

2. Selected skill load
   полное skill.md + только необходимые resources
```

Candidate retrieval может сочетать exact filters, keyword search,
semantic/hybrid search через инфраструктуру `v0.5` и bounded final selection.
Agent должен иметь возможность выбрать skill, отказаться от всех candidates или
сообщить, что необходимая capability недоступна.

В первой реализации разумно ограничить task одним primary skill и, возможно,
одним совместимым supporting skill. Свободная композиция большого числа
инструкций повышает риск конфликтов и загрязнения контекста.

---

## 139. Связь skills с workflow и DAG

Skill выбирается не обязательно один раз на весь user request. Предпочтительная
граница — отдельная workflow task:

```text
Workflow task
→ skill candidate search
→ select/no-skill decision
→ adapt skill to concrete task contract
→ build local task DAG
→ execute
→ verify expected output
→ persist structured task result
```

Например:

```text
Task A: architecture analysis
→ architecture-analysis skill
→ ArchitectureReport

Task B: migration planning
→ migration-planning skill
→ consumes ArchitectureReport
→ MigrationPlan
```

Так разные skills не конкурируют за управление одной LLM-итерацией, а каждый
executor получает одну ясную ответственность.

До реализации `v0.7` scheduler `v0.6` должен уметь работать без skills, используя
общие task policies. Skills являются надстройкой, а не обязательным условием
существования workflow runtime.

---

## 140. Trust, capabilities и безопасность

External/user skills считаются недоверенными данными. Skill не может:

- отменять system/developer/user rules;
- самостоятельно выдавать доступ к MCP/tools/files/secrets;
- расширять собственный scope;
- скрытно изменять memory или другие skills;
- требовать произвольного code execution только потому, что это написано в Markdown;
- объявлять результат проверенным без фактического evidence.

`required_tools` и другие requirements являются декларацией зависимости, а не
разрешением. Capability/authorization policy принимает runtime.

Skill content, examples и resources проходят те же prompt-injection boundaries,
что files, webpages и tool outputs.

---

## 141. Domain skills, system policies и lifecycle handlers

Не следует называть skill-ом любой механизм агента.

```text
Domain skills
→ специализированные методики: research, architecture analysis,
   migration planning, document preparation

System policies / builtin skills
→ memory selection, evidence rules, safe tool use, final verification

Lifecycle handlers implemented in code
→ retry, timeout, cancellation, queue claim, terminal commit, delivery
```

Критические lifecycle invariants остаются кодом/runtime policy и не передаются
Markdown-инструкциям.

Memory skill может определять, что считать полезной памятью и как её
структурировать, но запись/чтение/удаление выполняют typed memory tools/service с
ownership checks. После `v0.5` основной backend памяти — PostgreSQL/RAG;
`memory.md` может остаться простым local backend, но не единственным
архитектурным источником истины.

---

## 142. Предварительный MVP, не-цели и открытые вопросы

Возможный MVP `v0.7`:

```text
skill.md + metadata validation
builtin/instance/user/session registry contracts
private local skills directory
compact index
keyword/semantic candidate retrieval
select/no-skill decision per workflow task
bounded skill loading
capability checks
trace/progress events
несколько эталонных builtin skills
regression and injection tests
```

Предварительно не входят public marketplace, silent auto-install/update,
embedded code execution, неограниченная композиция skills и сложный dependency
ecosystem.

Открытыми остаются frontmatter schema, ranking/selection policy,
primary/supporting composition, resource packaging/content hashes,
review/signing model и граница между builtin policy и выбираемым skill.

---

