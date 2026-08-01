# Configuration examples contract

## Canonical public examples

The project has two canonical configuration templates:

- `.env.example` — every environment variable read by production code;
- `src/api/mcp.config.example` — every supported field of every root agent/MCP configuration model.

A parameter must be present in the corresponding example even when runtime code
provides a default value and the user normally does not need to override it.
Examples document the complete supported configuration surface, not only the
minimum required settings.

Real credentials must never be committed. Secret fields use empty values or
explicit replacement placeholders.

## Required change discipline

Any patch that adds, renames or removes a configuration parameter must update in
the same patch:

1. the authoritative configuration model or environment read;
2. `.env.example` or `src/api/mcp.config.example`;
3. comments/documentation explaining purpose, units and allowed values when the
   name alone is insufficient;
4. relevant validation and tests.

Keeping a default in source code is not a reason to omit the parameter from the
example.

## Automated audit

Run:

```bash
python scripts/audit_configuration_examples.py
```

The same audit is executed by the artifact CI suite. It verifies that:

- production environment reads are statically resolvable;
- every discovered environment key exists in `.env.example`;
- every discovered root JSON configuration section is registered for audit and
  exists in `mcp.config.example`;
- every Pydantic model field is represented in the example;
- example values pass the authoritative model validation.

Tests and their temporary fixture variables are excluded from environment-key
discovery because they are not runtime configuration.
