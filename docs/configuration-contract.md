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

Application profiles, hosting modes and configuration ownership are defined in
[`design/runtime-and-deployment-profiles.md`](design/runtime-and-deployment-profiles.md).

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
configuration of the whole Service Application rather than only MCP servers.

During `v0.4-runtime-modularization` the canonical service configuration name
becomes:

```text
agent.config
```

`mcp.config` may remain as a bounded compatibility alias during migration.
Documentation and new service entrypoints must use `agent.config` after the
transition.

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

## Configuration ownership

### Service operator configuration

`agent.config` is operator-owned configuration of one Service Application
deployment. It may contain:

- LLM providers and runtime defaults;
- storage, ingress, artifacts, planning and delivery settings;
- Gateway/client adapters;
- builtin and instance MCP definitions;
- topology and operator policy;
- references to secrets.

Self-hosted and managed are hosting modes of the same Service Application. They
may use different defaults, but share the same validated contract family.

Fields such as hosting mode, environment or topology do not switch Service
Application into Future Local Agent Application. The security-critical
application profile is selected by entrypoint/composition root.

### Per-user service configuration

User MCP definitions, credentials, preferences, workspace grants and other
per-user settings of a multi-user service are not stored as editable sections of
one global `agent.config`.

They are loaded through owner-aware application services/repositories and become
fully authorization-enforced with the Identity/Multi-user layer. The exact
persistence backend evolves in v0.5–v0.8.

### Future local-agent configuration

Future Local Agent Application will have a separate root configuration owned by
the local user. It may reuse common validated submodels for LLM, runtime,
memory, artifacts and MCP, but it is not required to share the complete Service
Application root schema.

The filename, packaging, local credential store and host permission schema are
not fixed by the current contract.

## ConfigProvider scope

The ConfigProvider contract may be generic over a validated snapshot type:

```text
Service ConfigProvider
→ ServiceConfigSnapshot / AgentConfigSnapshot

Future Local ConfigProvider
→ LocalAgentConfigSnapshot
```

Both preserve the common guarantees:

- complete validation before publication;
- immutable revisioned snapshot;
- atomic replacement;
- previous valid revision remains active after a failed reload;
- one active AgentCycle keeps one configuration revision.

ConfigProvider does not become a universal repository for per-user service
settings or durable registry ownership data.

## Required change discipline

Any patch that adds, renames or removes a supported configuration parameter must
update in the same patch:

1. the authoritative configuration model or environment read;
2. `.env.example` or the current canonical application configuration example;
3. comments/documentation explaining purpose, units and allowed values when the
   name alone is insufficient;
4. relevant validation and tests.

Keeping a default in source code is not a reason to omit the parameter from the
example.

Removing legacy code must also remove its audit exemption and obsolete example
entry in the same patch.

A parameter belonging to a future Local Agent Application must not be added to
the Service Application example before its root configuration contract exists.

## Automated audit

Run:

```bash
python scripts/audit_configuration_examples.py
```

The audit is executed by CI. Its target contract is:

- supported production environment reads are statically resolvable;
- every supported environment key exists in `.env.example`;
- explicitly registered legacy reads do not escape their exact migration paths;
- every discovered current Service Application root JSON configuration section
  is registered for audit and exists in the current canonical configuration
  example;
- every Pydantic model field is represented in the example;
- example values pass the authoritative model validation;
- stale legacy exemptions fail after their source paths are removed.

Tests and their temporary fixture variables are excluded from environment-key
discovery because they are not runtime configuration.

When a Future Local Agent root configuration is introduced, it receives its own
canonical example and audit target rather than being silently merged into the
Service Application example.
