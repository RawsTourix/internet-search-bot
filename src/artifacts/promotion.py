"""Explicit, idempotent promotion of tool-output candidates to artifacts."""

from __future__ import annotations

import asyncio
import codecs
import threading
from collections.abc import Iterable

from .candidate_store import ArtifactCandidateStore
from .errors import (
    ArtifactAccessError,
    ArtifactCandidateError,
    ArtifactCapabilityError,
    ArtifactIntegrityError,
)
from .models import (
    ArtifactAccessContext,
    ArtifactCandidate,
    ArtifactCandidateStatus,
    ArtifactContentKind,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    ArtifactVersionRef,
)
from .service import ArtifactService
from .validators import validate_native_text


class ArtifactCandidatePromotionService:
    """Promote one runtime-owned candidate exactly once in v0.4 single process."""

    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        candidate_store: ArtifactCandidateStore,
    ) -> None:
        self.artifact_service = artifact_service
        self.candidate_store = candidate_store
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = threading.Lock()

    async def create_artifact(
        self,
        *,
        candidate_id: str,
        allowed_candidate_ids: Iterable[str],
        session_id: str,
        cycle_id: str,
        purpose: ArtifactPurpose,
        filename: str | None = None,
        title: str | None = None,
        plan_id: str | None = None,
        plan_revision: int | None = None,
        plan_node_id: str | None = None,
    ) -> ArtifactVersionRef:
        lock = self._acquire_lock(candidate_id)
        try:
            async with lock:
                candidate = await self._authorized_candidate(
                    candidate_id,
                    allowed_candidate_ids=allowed_candidate_ids,
                    session_id=session_id,
                    cycle_id=cycle_id,
                )
                recovered = await self._find_existing_promotion(
                    candidate,
                    session_id=session_id,
                )
                if recovered is not None:
                    await self._repair_terminal_state(candidate, recovered.artifact_id)
                    return recovered

                await self._validate_candidate_content(candidate)
                lineage, version = await self.artifact_service.artifact_store.create_lineage(
                    session_id=session_id,
                    cycle_id=cycle_id,
                    content_id=candidate.content_id,
                    filename=filename or candidate.suggested_filename,
                    format_id=candidate.format_id,
                    detected_mime_type=candidate.mime_type,
                    provenance=self._provenance(
                        candidate,
                        operation="promote_candidate_to_artifact",
                        plan_id=plan_id,
                        plan_revision=plan_revision,
                        plan_node_id=plan_node_id,
                    ),
                    purpose=purpose,
                    declared_mime_type=candidate.mime_type,
                    encoding=self._encoding(candidate),
                    title=title,
                    metadata={
                        "source_candidate_id": candidate.candidate_id,
                        "candidate_metadata": dict(candidate.metadata),
                    },
                )
                result = self.artifact_service._build_ref(lineage, version)
                await self._repair_terminal_state(candidate, result.artifact_id)
                return result
        finally:
            self._release_lock(candidate_id, lock)

    async def create_version(
        self,
        *,
        candidate_id: str,
        allowed_candidate_ids: Iterable[str],
        artifact_lineage_id: str,
        expected_current_artifact_id: str,
        access: ArtifactAccessContext,
        cycle_id: str,
        filename: str | None = None,
        plan_id: str | None = None,
        plan_revision: int | None = None,
        plan_node_id: str | None = None,
    ) -> ArtifactVersionRef:
        lock = self._acquire_lock(candidate_id)
        try:
            async with lock:
                candidate = await self._authorized_candidate(
                    candidate_id,
                    allowed_candidate_ids=allowed_candidate_ids,
                    session_id=access.session_id,
                    cycle_id=cycle_id,
                )
                recovered = await self._find_existing_promotion(
                    candidate,
                    session_id=access.session_id,
                    artifact_lineage_id=artifact_lineage_id,
                )
                if recovered is not None:
                    await self._repair_terminal_state(candidate, recovered.artifact_id)
                    return recovered

                current_ref = await self.artifact_service.get_artifact(
                    expected_current_artifact_id,
                    access=access,
                )
                if current_ref.artifact_lineage_id != artifact_lineage_id:
                    raise ArtifactAccessError(
                        "Expected artifact does not belong to the requested lineage"
                    )
                if candidate.format_id != current_ref.format_id:
                    raise ArtifactCapabilityError(
                        "A new version must preserve the artifact format; "
                        "format conversion creates a new lineage"
                    )

                await self._validate_candidate_content(candidate)
                lineage, version = await self.artifact_service.artifact_store.create_version(
                    artifact_lineage_id=artifact_lineage_id,
                    expected_current_artifact_id=expected_current_artifact_id,
                    cycle_id=cycle_id,
                    content_id=candidate.content_id,
                    filename=filename or candidate.suggested_filename,
                    format_id=candidate.format_id,
                    detected_mime_type=candidate.mime_type,
                    provenance=self._provenance(
                        candidate,
                        operation="promote_candidate_to_version",
                        plan_id=plan_id,
                        plan_revision=plan_revision,
                        plan_node_id=plan_node_id,
                    ),
                    declared_mime_type=candidate.mime_type,
                    encoding=self._encoding(candidate),
                    metadata={
                        "source_candidate_id": candidate.candidate_id,
                        "candidate_metadata": dict(candidate.metadata),
                    },
                )
                result = self.artifact_service._build_ref(lineage, version)
                await self._repair_terminal_state(candidate, result.artifact_id)
                return result
        finally:
            self._release_lock(candidate_id, lock)

    async def _authorized_candidate(
        self,
        candidate_id: str,
        *,
        allowed_candidate_ids: Iterable[str],
        session_id: str,
        cycle_id: str,
    ) -> ArtifactCandidate:
        if candidate_id not in set(allowed_candidate_ids):
            raise ArtifactAccessError(
                "Candidate is outside the current cycle authority"
            )
        candidate = await self.candidate_store.get(candidate_id)
        if candidate.session_id != session_id or candidate.cycle_id != cycle_id:
            raise ArtifactAccessError(
                "Candidate is outside the current session or cycle"
            )
        if candidate.status == ArtifactCandidateStatus.PROMOTED:
            if candidate.promoted_artifact_id is None:
                raise ArtifactIntegrityError(
                    "Promoted candidate is missing its exact artifact reference"
                )
            existing = await self.artifact_service.get_artifact(
                candidate.promoted_artifact_id,
                access=ArtifactAccessContext(
                    session_id=session_id,
                    cycle_id=cycle_id,
                    allowed_artifact_ids=[candidate.promoted_artifact_id],
                ),
            )
            return candidate.model_copy(
                update={"metadata": {**candidate.metadata, "existing": existing.artifact_id}}
            )
        if candidate.status != ArtifactCandidateStatus.AVAILABLE:
            raise ArtifactCandidateError(
                f"Candidate is not available: {candidate.status.value}"
            )
        return candidate

    async def _validate_candidate_content(self, candidate: ArtifactCandidate) -> None:
        metadata = await self.artifact_service.content_store.get_metadata(
            candidate.content_id
        )
        if (
            metadata.size_bytes != candidate.size_bytes
            or metadata.content_hash != candidate.content_hash
            or metadata.mime_type != candidate.mime_type
        ):
            raise ArtifactIntegrityError(
                "Candidate metadata disagrees with canonical content metadata"
            )
        spec = self.artifact_service.format_registry.get(candidate.format_id)
        if (
            spec.content_kind == ArtifactContentKind.OPAQUE_BINARY
            and not self.artifact_service.config.allow_opaque_binary
        ):
            raise ArtifactCapabilityError("Opaque binary artifacts are disabled")
        if spec.content_kind == ArtifactContentKind.NATIVE_TEXT:
            text = await self._read_text(candidate, spec.default_encoding or "utf-8")
            validate_native_text(format_id=spec.format_id, text=text)

    async def _read_text(self, candidate: ArtifactCandidate, encoding: str) -> str:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        except LookupError as error:
            raise ArtifactIntegrityError(
                "Candidate text encoding is unsupported"
            ) from error
        parts: list[str] = []
        try:
            async for chunk in self.artifact_service.content_store.iter_content(
                candidate.content_id
            ):
                parts.append(decoder.decode(chunk, final=False))
            parts.append(decoder.decode(b"", final=True))
        except UnicodeDecodeError as error:
            raise ArtifactIntegrityError(
                "Candidate content is not valid text for its detected format"
            ) from error
        return "".join(parts)

    async def _find_existing_promotion(
        self,
        candidate: ArtifactCandidate,
        *,
        session_id: str,
        artifact_lineage_id: str | None = None,
    ) -> ArtifactVersionRef | None:
        lineages = (
            [await self.artifact_service.artifact_store.get_lineage(artifact_lineage_id)]
            if artifact_lineage_id is not None
            else await self.artifact_service.artifact_store.list_lineages(
                session_id=session_id,
                include_archived=True,
            )
        )
        for lineage in lineages:
            if lineage.session_id != session_id:
                continue
            versions = await self.artifact_service.artifact_store.list_versions(
                lineage.artifact_lineage_id
            )
            for version in versions:
                if version.metadata.get("source_candidate_id") == candidate.candidate_id:
                    return self.artifact_service._build_ref(lineage, version)
        return None

    async def _repair_terminal_state(
        self,
        candidate: ArtifactCandidate,
        artifact_id: str,
    ) -> None:
        await self.candidate_store.mark_promoted(
            candidate.candidate_id,
            artifact_id=artifact_id,
        )

    def _encoding(self, candidate: ArtifactCandidate) -> str | None:
        spec = self.artifact_service.format_registry.get(candidate.format_id)
        return (
            spec.default_encoding
            if spec.content_kind == ArtifactContentKind.NATIVE_TEXT
            else None
        )

    @staticmethod
    def _provenance(
        candidate: ArtifactCandidate,
        *,
        operation: str,
        plan_id: str | None,
        plan_revision: int | None,
        plan_node_id: str | None,
    ) -> ArtifactProvenance:
        return ArtifactProvenance(
            origin="tool_output",
            creator="tool",
            source_artifact_ids=list(candidate.source_artifact_ids),
            source_content_ids=[candidate.content_id],
            tool_call_id=candidate.source_tool_call_id,
            tool_name=candidate.source_tool_name,
            plan_id=plan_id,
            plan_revision=plan_revision,
            plan_node_id=plan_node_id,
            operation=operation,
        )

    def _acquire_lock(self, candidate_id: str) -> asyncio.Lock:
        with self._locks_guard:
            entry = self._locks.get(candidate_id)
            if entry is None:
                lock = asyncio.Lock()
                self._locks[candidate_id] = (lock, 1)
                return lock
            lock, users = entry
            self._locks[candidate_id] = (lock, users + 1)
            return lock

    def _release_lock(self, candidate_id: str, lock: asyncio.Lock) -> None:
        with self._locks_guard:
            entry = self._locks.get(candidate_id)
            if entry is None or entry[0] is not lock:
                return
            users = entry[1] - 1
            if users <= 0 and not lock.locked():
                self._locks.pop(candidate_id, None)
            else:
                self._locks[candidate_id] = (lock, users)
