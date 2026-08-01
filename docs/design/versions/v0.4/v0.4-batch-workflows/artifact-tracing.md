# Artifact lifecycle tracing

## Status

Implemented as a v0.4 observability foundation.

Artifact traces are an append-only diagnostic projection of already accepted
domain transitions. They are not a command bus, transaction log or source of
business truth.

Authoritative state remains in:

- ingress event and InputBatch stores;
- artifact lineage/version and content stores;
- delivery records and OutputBatch receipts;
- AgentCycle runtime state.

Failure to write an artifact trace must not roll back successful ingress,
artifact mutation or delivery.

## Purpose

`cycle_trace` explains what one agent cycle did. Artifact traces explain how
files moved and changed across the whole session, potentially through several
cycles and transports.

The trace allows one workflow to be correlated end-to-end:

```text
Telegram/Web input
→ ingress event
→ InputBatch
→ exact artifact version
→ AgentCycle authority/read/mutation
→ delivery selection
→ transport attempt
→ receipt
```

Typical diagnostic questions:

- how many files and text parts entered one InputBatch;
- which transport and client instance supplied a file;
- whether an exact input artifact reached the next session cycle;
- which version was selected for delivery;
- where a delivery became failed or unknown;
- whether a visible count mismatch was durable-state loss or presentation lag.

## Storage layout

The v0.4 implementation uses validated JSON Lines:

```text
<storage-root>/artifact_traces/
└── session_<sha256-prefix>/
    ├── session.json
    ├── 2026-08-01.jsonl
    ├── 2026-08-01.001.jsonl
    └── 2026-08-02.jsonl
```

The directory name contains only a hash of `session_id`. Exact authority is
stored inside `session.json` and every event.

Files rotate by UTC day and configured byte target. One validated event that is
larger than the target is still persisted in an empty part; it must never cause
an unbounded rotation loop.

Writes are serialized inside the filesystem store. Every JSONL line is one
complete Pydantic-validated event followed by `\n`.

## Event contract

Core fields:

```json
{
  "schema_version": 1,
  "trace_type": "artifact_event",
  "event_id": "aevt_<opaque-id>",
  "occurred_at": "2026-08-01T09:30:14.382Z",
  "session_id": "telegram:conversation:<chat-id>",
  "cycle_id": "<optional-cycle-id>",
  "operation_id": "<optional-operation-id>",
  "event_type": "artifact_ingress_stored",
  "stage": "ingress",
  "status": "succeeded",
  "direction": "inbound",
  "correlation": {},
  "transport": null,
  "artifact": null,
  "metrics": {},
  "error": null,
  "data": {}
}
```

Allowed directions:

```text
inbound | internal | outbound
```

Allowed statuses:

```text
started | succeeded | partially_succeeded | failed | unknown | observed
```

Correlation IDs are optional and include:

- `ingress_event_id`;
- `input_batch_id`;
- `output_batch_id`;
- `delivery_id`;
- `candidate_id`.

Transport metadata is normalized and may include:

- `client_type`;
- `client_instance_id`;
- `conversation_id` and `thread_id`;
- source update/message/group IDs;
- delivery mode and resulting client message ID.

Artifact metadata may include exact opaque IDs, filename, format, MIME, size,
hash, purpose and version. File content is never written into the trace.

## Initial event families

### Ingress

```text
input_batch_updated
input_batch_trace_snapshot_failed
artifact_ingress_stored
artifact_ingress_observed
artifact_ingress_failed
```

`input_batch_updated` records counts from the durable draft, not from Telegram
message timing:

```text
file_count
text_part_count
semantic_part_count
```

### Artifact runtime

```text
artifact_created
artifact_version_created
artifact_replaced
artifact_patched
artifact_read_completed
artifact_search_completed
artifact_candidate_promoted
```

The production client projects already normalized `ArtifactToolOutcome` values
through a dedicated tracing mixin. It invokes the existing cycle/progress hook
first and then writes a safe session-level event. Read/search traces contain
exact IDs and aggregate counts, never returned text, search queries or match
contents.

A native tool without a user-facing progress event may still have a diagnostic
trace event when its structured payload has an unambiguous domain type. This is
used for `artifact_search_completed` without changing the existing progress
contract.

### Session authority

```text
input_batch_artifacts_activated
artifact_handoff_saved
artifact_handoff_applied
```

These events distinguish three failure classes:

1. an input ref was never activated;
2. a completed-cycle handoff was not saved or applied;
3. authority was available but the model did not use it.

### Delivery

```text
artifact_delivery_selected
artifact_delivery_started
artifact_delivery_succeeded
artifact_delivery_failed
artifact_delivery_unknown
artifact_delivery_cancelled
```

Delivery events are appended only after the delivery store successfully
persists the corresponding transition. The delivery store remains authoritative.
Idempotent retries that do not change durable state do not append duplicate
transition events. Superseding one selected/failed lineage head records both the
new selection and the durable cancellation of the old head.

## Redaction and safety

Artifact traces must not contain:

- file contents or full user messages;
- credentials, cookies or authorization headers;
- bot/API tokens;
- signed or presigned download URLs;
- absolute local/workspace paths;
- arbitrary unbounded exception representations.

Sensitive mapping keys are removed recursively. Key matching covers normalized
secret suffixes such as `_token`, `_api_key`, `_secret`, `_url` and `_path`.
String values are also scrubbed for Bearer credentials, secret assignments,
HTTP(S) URLs and absolute Windows/POSIX paths before length bounding. Error
records contain a safe type, optional code, bounded redacted message and optional
retryability.

## Configuration

The artifact configuration accepts:

```json
{
  "artifacts": {
    "trace_enabled": true,
    "trace_max_file_bytes": 8388608,
    "trace_max_string_chars": 2000
  }
}
```

Defaults enable tracing. No environment-specific transport configuration is
required.

## Failure policy

`ArtifactTraceService.record()` is best-effort:

```text
domain transition succeeds
→ trace append attempted
→ trace append failure logs artifact_trace_write_failed
→ domain result remains successful
```

Direct reads through the trace store remain strict: malformed JSONL, symlinks,
invalid schemas or session authority mismatches raise integrity/storage errors.
This keeps diagnostics trustworthy without coupling production success to the
observability backend.

## Future migration

Callers depend on `ArtifactTraceStore`, not filesystem paths. A later version may
replace the JSONL store with PostgreSQL, a queue-backed sink or an analytics
pipeline without changing ingress, artifact runtime or delivery contracts.

The event schema is intentionally transport-neutral. Telegram, Web and future
clients contribute normalized transport metadata but do not define the domain
model.
