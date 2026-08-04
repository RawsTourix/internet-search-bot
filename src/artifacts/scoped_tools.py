"""Scoped artifact catalog and bounded current-cycle activation."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, model_validator

from ..mcp.manager_context import ManagerToolContext
from .activation import (
    ArtifactActivation,
    ArtifactActivationReason,
    ArtifactCatalogScope,
)
from .errors import ArtifactLimitError, ArtifactValidationError
from .models import ArtifactAccessContext
from .tools import (
    ARTIFACT_NATIVE_TOOL_DEFINITIONS,
    ArtifactListInput,
    ArtifactResultPolicy,
    ArtifactToolController,
    ArtifactToolDefinition,
    ArtifactToolOutcome,
)


class ScopedArtifactListInput(ArtifactListInput):
    """Bounded catalog query with an authority scope and opaque cursor."""

    scope: ArtifactCatalogScope = ArtifactCatalogScope.CURRENT
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_cursor_offset(self) -> "ScopedArtifactListInput":
        if self.cursor is not None and self.offset != 0:
            raise ValueError("cursor and non-zero offset are mutually exclusive")
        return self


_SCOPED_LIST_DEFINITION = ArtifactToolDefinition(
    name="artifact_list",
    description=(
        "Authoritative bounded metadata catalog. scope=current lists the active "
        "cycle manifest; scope=session lists exact versions from the current "
        "session history; scope=workspace uses the current filesystem workspace "
        "authority. Returned page items are explicitly activated for this cycle. "
        "Use the opaque next_cursor for pagination; filename ambiguity is returned "
        "without automatic selection."
    ),
    input_model=ScopedArtifactListInput,
    progress_key="artifact_list",
)


SCOPED_ARTIFACT_NATIVE_TOOL_DEFINITIONS = tuple(
    _SCOPED_LIST_DEFINITION if item.name == "artifact_list" else item
    for item in ARTIFACT_NATIVE_TOOL_DEFINITIONS
)


class ScopedArtifactToolController(ArtifactToolController):
    """Extend exact artifact tools with catalog scopes and activation provenance."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._definitions["artifact_list"] = _SCOPED_LIST_DEFINITION
        self.committed_batch_store = None

    async def _list(
        self,
        parsed: ScopedArtifactListInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        offset = self._decode_cursor(parsed.cursor, expected_scope=parsed.scope)
        if offset is None:
            offset = parsed.offset

        if parsed.scope == ArtifactCatalogScope.CURRENT:
            access = self._access(context)
            effective_scope = ArtifactCatalogScope.CURRENT
        else:
            access = await self._session_access(
                context,
                include_archived=parsed.include_archived,
            )
            # Filesystem v0.4 has one session-owned workspace. The public
            # workspace scope remains stable while v0.8 replaces this authority
            # backend with real multi-user workspace ownership.
            effective_scope = ArtifactCatalogScope.SESSION

        deliveries = []
        if self.delivery_service is not None:
            deliveries = await self.delivery_service.list_cycle_refs(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
        result = await self.service.catalog_artifacts(
            access=access,
            artifact_ids=parsed.artifact_ids,
            artifact_lineage_ids=parsed.artifact_lineage_ids,
            filenames=parsed.filenames,
            purpose_filter=parsed.purpose_filter,
            format_filter=parsed.format_filter,
            current_only=parsed.current_only,
            include_versions=parsed.include_versions,
            include_archived=parsed.include_archived,
            offset=offset,
            limit=parsed.limit,
            read_artifact_ids=self._read_artifact_ids(context),
            deliveries=deliveries,
        )
        activated = self._activate_catalog_page(
            context,
            artifact_ids=[item.artifact_id for item in result.items],
            scope=parsed.scope,
            source_operation_id=f"artifact_list:{parsed.scope.value}:{offset}",
        )
        next_cursor = (
            self._encode_cursor(
                scope=parsed.scope,
                offset=offset + len(result.items),
            )
            if result.items_truncated
            else None
        )
        payload = result.model_dump(mode="json")
        payload.update(
            {
                "scope": parsed.scope.value,
                "effective_scope": effective_scope.value,
                "cursor": parsed.cursor,
                "next_cursor": next_cursor,
                "activated_artifact_ids": activated,
                "activation_count": len(activated),
                "workspace_scope_note": (
                    "filesystem_v0.4_workspace_equals_session"
                    if parsed.scope == ArtifactCatalogScope.WORKSPACE
                    else None
                ),
            }
        )
        return ArtifactToolOutcome(
            payload=payload,
            event_type="artifact_catalog_activated" if activated else None,
            severity="info",
            visibility="internal",
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )

    async def _session_access(
        self,
        context: ManagerToolContext,
        *,
        include_archived: bool,
    ) -> ArtifactAccessContext:
        artifact_ids: list[str] = []
        lineages = await self.service.artifact_store.list_lineages(
            session_id=context.session_id,
            include_archived=include_archived,
        )
        for lineage in lineages:
            versions = await self.service.artifact_store.list_versions(
                lineage.artifact_lineage_id
            )
            if not await self._lineage_is_visible(versions):
                continue
            for version in versions:
                artifact_ids.append(version.artifact_id)
        return ArtifactAccessContext(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            allowed_artifact_ids=artifact_ids,
        )

    async def _lineage_is_visible(self, versions: list[Any]) -> bool:
        """Hide user-upload lineages until their whole InputBatch commits."""

        if self.committed_batch_store is None or not versions:
            return True
        provenance = getattr(versions[0], "provenance", None)
        if getattr(provenance, "origin", None) != "user_upload":
            return True
        input_batch_id = str(
            getattr(provenance, "input_batch_id", "") or ""
        ).strip()
        if not input_batch_id:
            return False
        try:
            await self.committed_batch_store.get_committed(input_batch_id)
        except Exception as error:
            if type(error).__name__ == "IngressNotFoundError":
                return False
            raise
        return True

    def _activate_catalog_page(
        self,
        context: ManagerToolContext,
        *,
        artifact_ids: list[str],
        scope: ArtifactCatalogScope,
        source_operation_id: str,
    ) -> list[str]:
        existing_ids = list(context.active_cycle.artifact_refs)
        new_ids = [
            artifact_id
            for artifact_id in artifact_ids
            if artifact_id not in existing_ids
        ]
        if (
            len(existing_ids) + len(new_ids)
            > self.service.config.max_artifacts_per_cycle
        ):
            raise ArtifactLimitError(
                "Catalog page would exceed the bounded current-cycle activation set; "
                "retry with a smaller limit or narrower filters"
            )

        activations = getattr(context.active_cycle, "artifact_activations", None)
        if activations is None:
            activations = []
            context.active_cycle.artifact_activations = activations
        already_recorded = {
            str(item.get("artifact_id"))
            for item in activations
            if isinstance(item, dict)
        }
        activated: list[str] = []
        timestamp = datetime.now(timezone.utc)
        for artifact_id in artifact_ids:
            if artifact_id not in context.active_cycle.artifact_refs:
                context.active_cycle.artifact_refs.append(artifact_id)
                activated.append(artifact_id)
            if artifact_id in already_recorded:
                continue
            record = ArtifactActivation(
                artifact_id=artifact_id,
                cycle_id=context.cycle_id,
                reason=ArtifactActivationReason.CATALOG_RESULT,
                scope=scope,
                source_operation_id=source_operation_id,
                activated_at=timestamp,
            )
            activations.append(record.model_dump(mode="json"))
            already_recorded.add(artifact_id)

        if artifact_ids:
            context.active_cycle.cycle_trace.append(
                {
                    "type": "artifact_catalog_activated",
                    "scope": scope.value,
                    "artifact_ids": list(artifact_ids),
                    "newly_activated_artifact_ids": activated,
                    "source_operation_id": source_operation_id,
                }
            )
        return activated

    @staticmethod
    def _encode_cursor(*, scope: ArtifactCatalogScope, offset: int) -> str:
        payload = json.dumps(
            {"v": 1, "scope": scope.value, "offset": offset},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        *,
        expected_scope: ArtifactCatalogScope,
    ) -> int | None:
        if cursor is None:
            return None
        normalized = cursor.strip()
        if not normalized:
            raise ArtifactValidationError(
                "invalid_artifact_cursor",
                "Artifact catalog cursor must not be empty.",
            )
        try:
            padded = normalized + "=" * (-len(normalized) % 4)
            payload: Any = json.loads(
                base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            )
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("scope") != expected_scope.value
                or not isinstance(payload.get("offset"), int)
                or payload["offset"] < 0
            ):
                raise ValueError("invalid cursor payload")
            return int(payload["offset"])
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise ArtifactValidationError(
                "invalid_artifact_cursor",
                "Artifact catalog cursor is invalid for the requested scope.",
                retryable=True,
            ) from error
