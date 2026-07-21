"""Persistence contracts for artifact lineages and exact versions."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import (
    ArtifactLineage,
    ArtifactLineageStatus,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
)


@runtime_checkable
class ArtifactStore(Protocol):
    async def create_lineage(
        self,
        *,
        session_id: str,
        cycle_id: str,
        content_id: str,
        filename: str,
        format_id: str,
        detected_mime_type: str,
        provenance: ArtifactProvenance,
        purpose: ArtifactPurpose = ArtifactPurpose.WORKING,
        declared_mime_type: str | None = None,
        encoding: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        """Create a lineage containing one committed exact version."""
        ...

    async def create_version(
        self,
        *,
        artifact_lineage_id: str,
        expected_current_artifact_id: str,
        cycle_id: str,
        content_id: str,
        filename: str | None,
        format_id: str | None,
        detected_mime_type: str | None,
        provenance: ArtifactProvenance,
        declared_mime_type: str | None = None,
        encoding: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ArtifactLineage, ArtifactVersion]:
        """Append one version when the expected current head still matches."""
        ...

    async def get_lineage(self, artifact_lineage_id: str) -> ArtifactLineage:
        ...

    async def get_version(self, artifact_id: str) -> ArtifactVersion:
        ...

    async def get_current_version(
        self,
        artifact_lineage_id: str,
    ) -> ArtifactVersion:
        ...

    async def list_versions(
        self,
        artifact_lineage_id: str,
    ) -> list[ArtifactVersion]:
        ...

    async def list_cycle_artifacts(
        self,
        cycle_id: str,
    ) -> list[ArtifactVersion]:
        ...

    async def list_lineages(
        self,
        *,
        session_id: str,
        include_archived: bool = False,
    ) -> list[ArtifactLineage]:
        ...

    async def archive_lineage(
        self,
        artifact_lineage_id: str,
        *,
        expected_current_artifact_id: str,
    ) -> ArtifactLineage:
        ...

    async def list_orphan_version_ids(self) -> list[str]:
        ...
