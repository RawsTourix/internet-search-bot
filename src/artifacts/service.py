"""Artifact business service for exact versions and native text operations."""

from __future__ import annotations

import codecs
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..storage.interfaces import ContentStore
from ..storage.models import ContentMatch
from .config import ArtifactConfigType
from .errors import (
    ArtifactAccessError,
    ArtifactCapabilityError,
    ArtifactFilenameConflictError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactValidationError,
)
from .format_registry import (
    ArtifactFormatRegistry,
    build_default_format_registry,
)
from .interfaces import ArtifactStore
from .models import (
    ArtifactAccessContext,
    ArtifactCatalogCapabilities,
    ArtifactCatalogItem,
    ArtifactCatalogResult,
    ArtifactCapability,
    ArtifactDeliveryRef,
    ArtifactDeliveryState,
    ArtifactFilenameResolution,
    ArtifactFilenameResolutionStatus,
    ArtifactLineage,
    ArtifactLineageStatus,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    ArtifactVersionRef,
    ExactTextPatchOperation,
    normalize_artifact_filename,
)
from .text_operations import apply_exact_text_patch, enforce_text_size
from .validators import validate_native_text


class ArtifactTextReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_text"
    artifact: ArtifactVersionRef
    text: str
    offset_chars: int = Field(ge=0)
    length_chars: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    eof: bool


class ArtifactSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_search_results"
    artifact: ArtifactVersionRef
    query: str
    matches: list[ContentMatch] = Field(default_factory=list)


class ArtifactService:
    """Coordinate exact artifact metadata with ContentStore payloads."""

    def __init__(
        self,
        *,
        config: ArtifactConfigType,
        artifact_store: ArtifactStore,
        content_store: ContentStore,
        format_registry: ArtifactFormatRegistry | None = None,
    ) -> None:
        self.config = config
        self.artifact_store = artifact_store
        self.content_store = content_store
        self.format_registry = format_registry or build_default_format_registry()

    async def list_artifacts(
        self,
        *,
        access: ArtifactAccessContext,
        purpose_filter: Iterable[ArtifactPurpose] = (),
        format_filter: Iterable[str] = (),
        offset: int = 0,
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[ArtifactVersionRef]:
        if offset < 0:
            raise ArtifactValidationError(
                "invalid_artifact_offset",
                "Artifact list offset must not be negative.",
            )
        if limit <= 0:
            raise ArtifactValidationError(
                "invalid_artifact_limit",
                "Artifact list limit must be positive.",
            )
        limit = min(limit, self.config.max_artifacts_per_cycle)
        purposes = set(purpose_filter)
        formats = {value.strip().lower() for value in format_filter if value.strip()}
        allowed_lineages = await self._allowed_lineage_ids(access)
        result: list[ArtifactVersionRef] = []
        for lineage in await self.artifact_store.list_lineages(
            session_id=access.session_id,
            include_archived=include_archived,
        ):
            if lineage.artifact_lineage_id not in allowed_lineages:
                continue
            if purposes and lineage.purpose not in purposes:
                continue
            version = await self.artifact_store.get_current_version(
                lineage.artifact_lineage_id
            )
            if formats and version.format_id not in formats:
                continue
            result.append(self._build_ref(lineage, version))
        result.sort(
            key=lambda item: (
                item.filename.lower(),
                item.artifact_lineage_id,
                item.version,
            )
        )
        return result[offset : offset + limit]

    async def get_artifact(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccessContext,
    ) -> ArtifactVersionRef:
        lineage, version = await self._authorized_version(artifact_id, access)
        return self._build_ref(lineage, version)

    async def catalog_artifacts(
        self,
        *,
        access: ArtifactAccessContext,
        artifact_ids: Iterable[str] = (),
        artifact_lineage_ids: Iterable[str] = (),
        filenames: Iterable[str] = (),
        purpose_filter: Iterable[ArtifactPurpose] = (),
        format_filter: Iterable[str] = (),
        current_only: bool = True,
        include_versions: bool = False,
        include_archived: bool = False,
        offset: int = 0,
        limit: int = 20,
        read_artifact_ids: Iterable[str] = (),
        deliveries: Iterable[ArtifactDeliveryRef] = (),
    ) -> ArtifactCatalogResult:
        """Build an authoritative bounded catalog for the current authority."""

        if offset < 0:
            raise ArtifactValidationError(
                "invalid_artifact_offset",
                "Artifact list offset must not be negative.",
            )
        if limit <= 0:
            raise ArtifactValidationError(
                "invalid_artifact_limit",
                "Artifact list limit must be positive.",
            )
        bounded_limit = min(limit, self.config.max_artifacts_per_cycle)
        requested_artifact_ids = set(artifact_ids)
        requested_lineage_ids = set(artifact_lineage_ids)
        requested_filenames = [
            normalize_artifact_filename(value) for value in filenames
        ]
        requested_filename_set = set(requested_filenames)
        purposes = set(purpose_filter)
        formats = {
            value.strip().lower()
            for value in format_filter
            if value.strip()
        }
        read_ids = set(read_artifact_ids)
        delivery_by_artifact = {
            item.artifact_id: item for item in deliveries
        }
        allowed_lineages = await self._allowed_lineage_ids(access)

        all_items: list[ArtifactCatalogItem] = []
        available_filenames: list[str] = []
        resolution_candidates: dict[str, list[ArtifactCatalogItem]] = {
            filename: [] for filename in requested_filenames
        }
        included_lineage_ids: set[str] = set()
        lineages = await self.artifact_store.list_lineages(
            session_id=access.session_id,
            include_archived=include_archived,
        )
        for lineage in lineages:
            if lineage.artifact_lineage_id not in allowed_lineages:
                continue
            if (
                not include_archived
                and lineage.status != ArtifactLineageStatus.ACTIVE
            ):
                continue
            versions = await self.artifact_store.list_versions(
                lineage.artifact_lineage_id
            )
            current = versions[-1]
            available_filenames.append(current.filename)
            current_item = self._build_catalog_item(
                lineage,
                current,
                read_ids=read_ids,
                delivery=delivery_by_artifact.get(current.artifact_id),
                current_cycle_id=access.cycle_id,
            )
            if current.filename in resolution_candidates:
                resolution_candidates[current.filename].append(current_item)

            if purposes and lineage.purpose not in purposes:
                continue
            if requested_lineage_ids and (
                lineage.artifact_lineage_id not in requested_lineage_ids
            ):
                continue

            selected_versions = (
                versions
                if include_versions or not current_only
                else [current]
            )
            for version in selected_versions:
                if requested_artifact_ids and (
                    version.artifact_id not in requested_artifact_ids
                ):
                    continue
                if requested_filename_set and (
                    version.filename not in requested_filename_set
                ):
                    continue
                if formats and version.format_id not in formats:
                    continue
                item = self._build_catalog_item(
                    lineage,
                    version,
                    read_ids=read_ids,
                    delivery=delivery_by_artifact.get(version.artifact_id),
                    current_cycle_id=access.cycle_id,
                )
                all_items.append(item)
                included_lineage_ids.add(lineage.artifact_lineage_id)

        all_items.sort(
            key=lambda item: (
                item.filename.lower(),
                item.artifact_lineage_id,
                item.version,
            )
        )
        page = all_items[offset : offset + bounded_limit]
        resolutions: list[ArtifactFilenameResolution] = []
        for filename in requested_filenames:
            candidates = resolution_candidates[filename]
            if len(candidates) == 1:
                status = ArtifactFilenameResolutionStatus.OK
            elif candidates:
                status = ArtifactFilenameResolutionStatus.AMBIGUOUS
            else:
                status = ArtifactFilenameResolutionStatus.NOT_FOUND
            suggestions = (
                self._filename_suggestions(
                    filename,
                    available_filenames=available_filenames,
                )
                if status == ArtifactFilenameResolutionStatus.NOT_FOUND
                else []
            )
            resolutions.append(ArtifactFilenameResolution(
                filename=filename,
                status=status,
                candidates=candidates,
                suggestions=suggestions,
            ))

        return ArtifactCatalogResult(
            available_count=len(all_items),
            lineage_count=len(included_lineage_ids),
            offset=offset,
            limit=bounded_limit,
            items=page,
            items_truncated=offset + len(page) < len(all_items),
            filename_resolutions=resolutions,
        )

    async def read_text(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccessContext,
        offset_chars: int = 0,
        limit_chars: int | None = None,
    ) -> ArtifactTextReadResult:
        if offset_chars < 0:
            raise ArtifactValidationError(
                "invalid_artifact_text_offset",
                "Artifact text offset must not be negative.",
            )
        requested_limit = (
            self.config.max_inline_text_chars
            if limit_chars is None
            else limit_chars
        )
        if requested_limit <= 0 or requested_limit > self.config.max_read_chars:
            raise ArtifactLimitError(
                "Artifact text read limit exceeds the configured budget"
            )
        lineage, version = await self._authorized_version(artifact_id, access)
        spec = self._require_capability(
            version.format_id,
            ArtifactCapability.READ_TEXT,
        )
        encoding = version.encoding or spec.default_encoding or "utf-8"
        text, total_chars = await self._read_text_slice(
            version.content_id,
            encoding=encoding,
            offset_chars=offset_chars,
            limit_chars=requested_limit,
        )
        return ArtifactTextReadResult(
            artifact=self._build_ref(lineage, version),
            text=text,
            offset_chars=offset_chars,
            length_chars=len(text),
            total_chars=total_chars,
            eof=offset_chars + len(text) >= total_chars,
        )

    async def search_text(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccessContext,
        query: str,
        limit: int | None = None,
    ) -> ArtifactSearchResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ArtifactValidationError(
                "empty_artifact_search_query",
                "Artifact search query must not be empty.",
            )
        requested_limit = self.config.max_search_matches if limit is None else limit
        if requested_limit <= 0 or requested_limit > self.config.max_search_matches:
            raise ArtifactLimitError(
                "Artifact search match limit exceeds the configured budget"
            )
        lineage, version = await self._authorized_version(artifact_id, access)
        self._require_capability(
            version.format_id,
            ArtifactCapability.SEARCH_TEXT,
        )
        matches = await self.content_store.search_text(
            version.content_id,
            query=normalized_query,
            limit=requested_limit,
        )
        return ArtifactSearchResult(
            artifact=self._build_ref(lineage, version),
            query=normalized_query,
            matches=matches,
        )

    async def create_text(
        self,
        *,
        session_id: str,
        cycle_id: str,
        filename: str,
        text: str,
        format_id: str,
        provenance: ArtifactProvenance,
        purpose: ArtifactPurpose = ArtifactPurpose.WORKING,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        access: ArtifactAccessContext | None = None,
    ) -> ArtifactVersionRef:
        filename = normalize_artifact_filename(filename)
        creation_access = access or await self._session_access_context(
            session_id=session_id,
            cycle_id=cycle_id,
        )
        await self.ensure_new_lineage_filename_available(
            creation_access,
            filename,
        )
        spec = self._require_capability(
            format_id,
            ArtifactCapability.READ_TEXT,
        )
        encoding = spec.default_encoding or "utf-8"
        payload = enforce_text_size(
            text,
            encoding=encoding,
            max_bytes=self.config.max_artifact_size_bytes,
            operation="create",
        )
        report = validate_native_text(format_id=spec.format_id, text=text)
        content_ref = await self.content_store.save_content(
            payload,
            source_type="artifact",
            source_name=filename,
            mime_type=spec.canonical_mime_type,
            encoding=encoding,
            cycle_id=cycle_id,
            metadata={
                "artifact_format_id": spec.format_id,
                "artifact_validation": report.model_dump(mode="json"),
                **dict(metadata or {}),
            },
        )
        lineage, version = await self.artifact_store.create_lineage(
            session_id=session_id,
            cycle_id=cycle_id,
            content_id=content_ref.content_id,
            filename=filename,
            format_id=spec.format_id,
            detected_mime_type=spec.canonical_mime_type,
            provenance=provenance,
            purpose=purpose,
            declared_mime_type=spec.canonical_mime_type,
            encoding=encoding,
            title=title,
            metadata={
                "validation": report.model_dump(mode="json"),
                **dict(metadata or {}),
            },
        )
        return self._build_ref(lineage, version)

    async def find_active_by_filename(
        self,
        access: ArtifactAccessContext,
        filename: str,
    ) -> list[ArtifactVersionRef]:
        """Return exact current candidates without expanding authority."""

        normalized = normalize_artifact_filename(filename)
        allowed_lineages = await self._allowed_lineage_ids(access)
        result: list[ArtifactVersionRef] = []
        for lineage in await self.artifact_store.list_lineages(
            session_id=access.session_id,
            include_archived=False,
        ):
            if lineage.artifact_lineage_id not in allowed_lineages:
                continue
            version = await self.artifact_store.get_current_version(
                lineage.artifact_lineage_id
            )
            if version.filename == normalized:
                result.append(self._build_ref(lineage, version))
        return result

    async def ensure_new_lineage_filename_available(
        self,
        access: ArtifactAccessContext,
        filename: str,
    ) -> None:
        """Reject an agent-created lineage before any new content is saved."""

        normalized = normalize_artifact_filename(filename)
        candidates = await self.find_active_by_filename(access, normalized)
        if candidates:
            raise ArtifactFilenameConflictError(
                normalized,
                current_candidates=[
                    item.model_dump(mode="json") for item in candidates
                ],
            )

    async def replace_text(
        self,
        *,
        artifact_id: str,
        expected_current_artifact_id: str,
        access: ArtifactAccessContext,
        cycle_id: str,
        new_text: str,
        provenance: ArtifactProvenance,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactVersionRef:
        lineage, current = await self._authorized_current_version(
            artifact_id,
            expected_current_artifact_id=expected_current_artifact_id,
            access=access,
        )
        spec = self._require_capability(
            current.format_id,
            ArtifactCapability.REPLACE_TEXT,
        )
        encoding = current.encoding or spec.default_encoding or "utf-8"
        payload = enforce_text_size(
            new_text,
            encoding=encoding,
            max_bytes=self.config.max_artifact_size_bytes,
            operation="replace",
        )
        report = validate_native_text(format_id=current.format_id, text=new_text)
        content_ref = await self.content_store.save_content(
            payload,
            source_type="artifact_version",
            source_name=filename or current.filename,
            mime_type=current.detected_mime_type,
            encoding=encoding,
            cycle_id=cycle_id,
            metadata={
                "source_artifact_id": current.artifact_id,
                "artifact_format_id": current.format_id,
                "artifact_validation": report.model_dump(mode="json"),
                **dict(metadata or {}),
            },
        )
        next_lineage, version = await self.artifact_store.create_version(
            artifact_lineage_id=lineage.artifact_lineage_id,
            expected_current_artifact_id=expected_current_artifact_id,
            cycle_id=cycle_id,
            content_id=content_ref.content_id,
            filename=filename,
            format_id=current.format_id,
            detected_mime_type=current.detected_mime_type,
            provenance=provenance,
            declared_mime_type=current.declared_mime_type,
            encoding=encoding,
            metadata={
                "validation": report.model_dump(mode="json"),
                **dict(metadata or {}),
            },
        )
        return self._build_ref(next_lineage, version)

    async def patch_text(
        self,
        *,
        artifact_id: str,
        expected_current_artifact_id: str,
        access: ArtifactAccessContext,
        cycle_id: str,
        operations: list[ExactTextPatchOperation],
        provenance: ArtifactProvenance,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactVersionRef:
        lineage, current = await self._authorized_current_version(
            artifact_id,
            expected_current_artifact_id=expected_current_artifact_id,
            access=access,
        )
        spec = self._require_capability(
            current.format_id,
            ArtifactCapability.PATCH_TEXT,
        )
        encoding = current.encoding or spec.default_encoding or "utf-8"
        if current.size_bytes > self.config.max_patchable_text_bytes:
            raise ArtifactLimitError(
                "Artifact is too large for in-memory exact patching"
            )
        try:
            current_text = await self.content_store.read_text(current.content_id)
        except (LookupError, UnicodeDecodeError) as error:
            raise ArtifactValidationError(
                "artifact_text_decode_error",
                "Artifact text cannot be decoded with its declared encoding.",
                retryable=False,
            ) from error
        patched = apply_exact_text_patch(
            current_text,
            operations,
            config=self.config,
            encoding=encoding,
        )
        return await self.replace_text(
            artifact_id=artifact_id,
            expected_current_artifact_id=expected_current_artifact_id,
            access=access,
            cycle_id=cycle_id,
            new_text=patched,
            provenance=provenance,
            filename=filename,
            metadata={
                "patch_operation_count": len(operations),
                **dict(metadata or {}),
            },
        )

    async def _authorized_current_version(
        self,
        artifact_id: str,
        *,
        expected_current_artifact_id: str,
        access: ArtifactAccessContext,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        lineage, requested = await self._authorized_version(artifact_id, access)
        current = await self.artifact_store.get_current_version(
            lineage.artifact_lineage_id
        )
        if requested.artifact_lineage_id != current.artifact_lineage_id:
            raise ArtifactAccessError("Artifact lineage access mismatch")
        # Store remains the final optimistic-concurrency authority. This early
        # check avoids persisting new content for an already stale caller.
        if current.artifact_id != expected_current_artifact_id:
            from .errors import ArtifactVersionConflictError

            raise ArtifactVersionConflictError(
                lineage.artifact_lineage_id,
                expected_current_artifact_id=expected_current_artifact_id,
                current_artifact_id=current.artifact_id,
                current_version=current.version,
                current_ref=self._build_ref(lineage, current).model_dump(mode="json"),
            )
        return lineage, current

    async def _authorized_version(
        self,
        artifact_id: str,
        access: ArtifactAccessContext,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        version = await self.artifact_store.get_version(artifact_id)
        lineage = await self.artifact_store.get_lineage(
            version.artifact_lineage_id
        )
        if lineage.session_id != access.session_id:
            raise ArtifactAccessError("Artifact is outside the current session")
        allowed_lineages = await self._allowed_lineage_ids(access)
        if lineage.artifact_lineage_id not in allowed_lineages:
            raise ArtifactAccessError("Artifact is outside the current cycle authority")
        return lineage, version

    async def _allowed_lineage_ids(
        self,
        access: ArtifactAccessContext,
    ) -> set[str]:
        result: set[str] = set()
        for allowed_id in access.allowed_artifact_ids:
            try:
                version = await self.artifact_store.get_version(allowed_id)
                lineage = await self.artifact_store.get_lineage(
                    version.artifact_lineage_id
                )
            except ArtifactNotFoundError:
                # Unknown handles never expand authority. Integrity/storage
                # failures remain visible and must not be downgraded.
                continue
            if lineage.session_id == access.session_id:
                result.add(lineage.artifact_lineage_id)
        return result

    async def _session_access_context(
        self,
        *,
        session_id: str,
        cycle_id: str,
    ) -> ArtifactAccessContext:
        lineages = await self.artifact_store.list_lineages(
            session_id=session_id,
            include_archived=False,
        )
        return ArtifactAccessContext(
            session_id=session_id,
            cycle_id=cycle_id,
            allowed_artifact_ids=[
                lineage.current_artifact_id for lineage in lineages
            ],
        )

    def _build_ref(
        self,
        lineage: ArtifactLineage,
        version: ArtifactVersion,
    ) -> ArtifactVersionRef:
        spec = self.format_registry.get(version.format_id)
        return ArtifactVersionRef(
            artifact_id=version.artifact_id,
            artifact_lineage_id=version.artifact_lineage_id,
            version=version.version,
            filename=version.filename,
            format_id=version.format_id,
            mime_type=version.detected_mime_type,
            size_bytes=version.size_bytes,
            content_hash=version.content_hash,
            purpose=lineage.purpose,
            capabilities=sorted(spec.capabilities, key=lambda item: item.value),
        )

    def _build_catalog_item(
        self,
        lineage: ArtifactLineage,
        version: ArtifactVersion,
        *,
        read_ids: set[str],
        delivery: ArtifactDeliveryRef | None,
        current_cycle_id: str,
    ) -> ArtifactCatalogItem:
        spec = self.format_registry.get(version.format_id)
        capabilities = spec.capabilities
        origin_map = {
            "user_upload": "input",
            "agent_created": "agent",
            "agent_edit": "agent",
            "tool_output": "tool",
            "conversion": "tool",
            "migration": "runtime",
        }
        return ArtifactCatalogItem(
            artifact_id=version.artifact_id,
            artifact_lineage_id=lineage.artifact_lineage_id,
            version=version.version,
            versions_count=len(lineage.committed_artifact_ids),
            filename=version.filename,
            title=lineage.title,
            purpose=lineage.purpose,
            origin=origin_map[version.provenance.origin],
            format_id=version.format_id,
            mime_type=version.detected_mime_type,
            size_bytes=version.size_bytes,
            content_hash=version.content_hash,
            is_current=lineage.current_artifact_id == version.artifact_id,
            read_in_current_cycle=version.artifact_id in read_ids,
            created_in_current_cycle=(
                version.created_cycle_id == current_cycle_id
            ),
            selected_for_delivery=(
                delivery is not None
                and delivery.state != ArtifactDeliveryState.CANCELLED
            ),
            delivery_state=(delivery.state if delivery is not None else None),
            capabilities=ArtifactCatalogCapabilities(
                read_text=ArtifactCapability.READ_TEXT in capabilities,
                search_text=ArtifactCapability.SEARCH_TEXT in capabilities,
                replace_text=ArtifactCapability.REPLACE_TEXT in capabilities,
                patch_text=ArtifactCapability.PATCH_TEXT in capabilities,
                deliver=ArtifactCapability.DELIVER in capabilities,
                bind_to_tool=(
                    ArtifactCapability.PROCESS_EXTERNALLY in capabilities
                ),
            ),
        )

    @staticmethod
    def _filename_suggestions(
        filename: str,
        *,
        available_filenames: Iterable[str],
        limit: int = 5,
    ) -> list[str]:
        """Return bounded case-insensitive hints without resolving them."""

        folded = filename.casefold()
        suggestions: list[str] = []
        for candidate in sorted(set(available_filenames)):
            candidate_folded = candidate.casefold()
            if (
                candidate_folded == folded
                or folded in candidate_folded
                or candidate_folded in folded
            ):
                suggestions.append(candidate)
            if len(suggestions) >= limit:
                break
        return suggestions

    def _require_capability(
        self,
        format_id: str,
        capability: ArtifactCapability,
    ):
        spec = self.format_registry.get(format_id)
        if capability not in spec.capabilities:
            raise ArtifactCapabilityError(
                f"Artifact format {spec.format_id!r} does not support "
                f"{capability.value!r}"
            )
        return spec

    async def _read_text_slice(
        self,
        content_id: str,
        *,
        encoding: str,
        offset_chars: int,
        limit_chars: int,
    ) -> tuple[str, int]:
        try:
            decoder_type = codecs.getincrementaldecoder(encoding)
        except LookupError as error:
            raise ArtifactValidationError(
                "artifact_text_encoding_error",
                "Artifact encoding is unsupported.",
                retryable=False,
            ) from error
        decoder = decoder_type(errors="strict")
        collected: list[str] = []
        cursor = 0

        def consume(piece: str) -> None:
            nonlocal cursor
            piece_start = cursor
            piece_end = cursor + len(piece)
            wanted_start = offset_chars
            wanted_end = offset_chars + limit_chars
            if piece_end > wanted_start and piece_start < wanted_end:
                start = max(0, wanted_start - piece_start)
                end = min(len(piece), wanted_end - piece_start)
                if end > start:
                    collected.append(piece[start:end])
            cursor = piece_end

        try:
            async for chunk in self.content_store.iter_content(content_id):
                consume(decoder.decode(chunk, final=False))
            consume(decoder.decode(b"", final=True))
        except UnicodeDecodeError as error:
            raise ArtifactValidationError(
                "artifact_text_decode_error",
                "Artifact text cannot be decoded with its declared encoding.",
                retryable=False,
            ) from error
        return "".join(collected), cursor
