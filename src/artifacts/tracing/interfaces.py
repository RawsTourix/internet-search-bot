"""Storage contract for artifact lifecycle traces."""

from __future__ import annotations

from typing import Protocol

from .models import ArtifactTraceEvent


class ArtifactTraceStore(Protocol):
    async def append(self, event: ArtifactTraceEvent) -> None:
        """Append one immutable artifact event."""

    async def list_session(self, session_id: str) -> list[ArtifactTraceEvent]:
        """Read one session trace in persisted order."""
