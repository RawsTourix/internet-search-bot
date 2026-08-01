# Configuration examples contract

## Canonical public examples

The project has two current canonical configuration templates:

- `.env.example` — every supported environment variable of the active
  application/runtime;
- `src/api/mcp.config.example` — every supported field of every current root
  agent configuration model.

A supported parameter must be present in the corresponding example even when
runtime code provides a default value and the user normally does not need to
override it. Examples document the complete supported configuration surface,
not only the minimum required settings.

Real credentials must never be committed. Secret fields use empty values or
explicit replacement placeholders.

## Legacy reads are not public configuration

A variable does not become part of the supported configuration surface only
because old code still reads it.

Legacy entrypoints and legacy builtin stdio MCP servers are migration targets.
Their private compatibility parameters are removed together with that code or
moved into the configuration of a replacement service. They must not be added
to `.env.example` merely to satisfy a source-code scan.

Current legacy environment names scheduled for removal are:

```text
LLM_API_URL
LLM_MODEL
NVIDIA_API_KEY
LLM_API_KEY
DEBUG
YANDEX_SEARCH_API_KEY
YANDEX_CLOUD_FOLDER_ID
```

The generic proxy variables remain supported and stay in `.env.example`:

```text
HTTP_PROXY
HTTPS_PROXY
NO_PROXY
```

Any temporary audit exemption for legacy code must name exact paths and exact
keys. A broad directory exclusion is not allowed, and a legacy key appearing in
new production code is an audit failure.

## Configuration file evolution

`mcp.config` is the current compatibility filename, but it already contains the
configuration of the whole agent rather than only MCP servers.

During `v0.4-runtime-modularization` the canonical name becomes:

```text
agent.config
```

`mcp.config` may remain as a bounded compatibility alias during migration.
Documentation and new entrypoints must use `agent.config` after the transition.

Configuration loading is owned by `ConfigProvider`, not by individual services
and not by import-time application construction.

```text
agent.config changed
→ ConfigProvider reads and validates the complete file
→ a new immutable configuration snapshot/revision is published atomically
→ new operations use the new revision
```

If validation fails, the previous valid snapshot remains active. One active
AgentCycle uses one configuration revision; a file change does not mutate its
settings midway through execution.

## Required change discipline

Any patch that adds, renames or removes a supported configuration parameter must
update in the same patch:

1. the authoritative configuration model or environment read;
2. `.env.example` or the current canonical agent configuration example;
3. comments/documentation explaining purpose, units and allowed values when the
   name alone is insufficient;
4. relevant validation and tests.

Keeping a default in source code is not a reason to omit the parameter from the
example.

Removing legacy code must also remove its audit exemption and obsolete example
entry in the same patch.

## Automated audit

Run:

```bash
python scripts/audit_configuration_examples.py
```

The audit is executed by CI. Its target contract is:

- supported production environment reads are statically resolvable;
- every supported environment key exists in `.env.example`;
- explicitly registered legacy reads do not escape their exact migration paths;
- every discovered root JSON configuration section is registered for audit and
  exists in the current canonical configuration example;
- every Pydantic model field is represented in the example;
- example values pass the authoritative model validation;
- stale legacy exemptions fail after their source paths are removed.

Tests and their temporary fixture variables are excluded from environment-key
discovery because they are not runtime configuration.
