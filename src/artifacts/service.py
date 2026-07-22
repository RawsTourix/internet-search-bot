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
    ArtifactCapability,
    ArtifactLineage,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    ArtifactVersionRef,
    ExactTextPatchOperation,
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
    ) -> ArtifactVersionRef:
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
