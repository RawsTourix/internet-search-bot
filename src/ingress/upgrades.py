"""Explicit persisted ingress schema upgrades.

Only persisted metadata is upgraded here. Client payload validation remains
strict and always produces the current schema.
"""

from __future__ import annotations

from typing import Any


CURRENT_INGRESS_SCHEMA_VERSION = 2


def upgrade_ingress_event(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    version = int(result.get("schema_version", 1))
    if version == 1:
        result.update(
            schema_version=2,
            client_binding_id=None,
            client_version=None,
            semantic_parts=[],
            transport_locale=None,
            capability_declaration=None,
            capability_snapshot_ref=None,
            capability_snapshot=None,
            response_anchor_candidates=[],
        )
        version = 2
    if version != CURRENT_INGRESS_SCHEMA_VERSION:
        raise ValueError(f"unsupported ingress event schema version: {version}")
    return result


def upgrade_input_batch_draft(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    version = int(result.get("schema_version", 1))
    if version == 1:
        result.update(
            schema_version=2,
            semantic_parts=[],
            locale=None,
            capability_snapshot=None,
            response_anchor=None,
        )
        version = 2
    if version != CURRENT_INGRESS_SCHEMA_VERSION:
        raise ValueError(f"unsupported input draft schema version: {version}")
    return result


def upgrade_committed_input_batch(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    version = int(result.get("schema_version", 1))
    if version == 1:
        result.update(
            schema_version=2,
            semantic_parts=[],
            locale=None,
            capability_snapshot=None,
            response_anchor=None,
            artifact_manifest={
                "items": [],
                "available_count": len(result.get("artifact_refs", [])),
                "truncated": bool(result.get("artifact_refs")),
            },
        )
        version = 2
    if version != CURRENT_INGRESS_SCHEMA_VERSION:
        raise ValueError(f"unsupported committed batch schema version: {version}")
    return result
