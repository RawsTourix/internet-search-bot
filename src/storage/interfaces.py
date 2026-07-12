"""Protocols consumed by agent runtime and higher-level services."""

from typing import Any, Protocol, runtime_checkable

from .models import ArtifactRef, ContentMatch, ContentMetadata, ContentRange, ContentRef


@runtime_checkable
class ContentStore(Protocol):
    async def save_content(
        self,
        content: bytes | str,
        *,
        source_type: str,
        source_name: str | None = None,
        mime_type: str | None = None,
        encoding: str | None = None,
        cycle_id: str | None = None,
        tool_call_id: str | None = None,
        size_tokens_estimate: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContentRef:
        """Persist content and return an opaque reference."""
        ...

    async def get_metadata(self, content_id: str) -> ContentMetadata:
        """Load content metadata."""
        ...

    async def read_content(self, content_id: str) -> bytes:
        """Read complete content within the configured memory limit."""
        ...

    async def read_text(self, content_id: str) -> str:
        """Read complete content using its declared encoding."""
        ...

    async def read_range(
        self, content_id: str, *, offset: int, length: int
    ) -> ContentRange:
        """Read a byte range without loading the complete object."""
        ...

    async def search_text(
        self, content_id: str, *, query: str, limit: int = 10
    ) -> list[ContentMatch]:
        """Search decoded text sequentially without a persistent index."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    async def save_artifact(
        self,
        content: bytes,
        *,
        cycle_id: str,
        filename: str,
        mime_type: str | None = None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Persist an initial artifact version."""
        ...

    async def get_artifact(self, artifact_id: str) -> ArtifactRef:
        """Load an artifact reference."""
        ...

    async def open_artifact(self, artifact_id: str) -> bytes:
        """Read complete artifact bytes within the memory limit."""
        ...

    async def create_version(
        self,
        artifact_id: str,
        content: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: str = "agent_modified",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Create an immutable successor to an artifact."""
        ...

    async def list_cycle_artifacts(self, cycle_id: str) -> list[ArtifactRef]:
        """List all artifact versions belonging to a cycle."""
        ...

    async def mark_for_delivery(
        self, artifact_id: str, *, client_type: str
    ) -> None:
        """Idempotently add a delivery target to artifact metadata."""
        ...
