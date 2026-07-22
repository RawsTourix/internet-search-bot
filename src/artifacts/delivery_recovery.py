"""Crash recovery for delivery claims with unknown transport outcome."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .delivery import FileSystemArtifactDeliveryStore
from .errors import ArtifactDeliveryError, ArtifactStorageError
from .models import ArtifactDeliveryRef, ArtifactDeliveryState


async def recover_stale_delivery_claims(
    store: FileSystemArtifactDeliveryStore,
    *,
    claim_timeout_seconds: int,
    now: datetime | None = None,
) -> list[ArtifactDeliveryRef]:
    """Move stale ``delivering`` records to ``unknown`` after process failure.

    Unknown is intentionally conservative: the transport may have accepted the
    bytes before the process died. Callers must not automatically redeliver it.
    """

    if (
        isinstance(claim_timeout_seconds, bool)
        or not isinstance(claim_timeout_seconds, int)
        or claim_timeout_seconds <= 0
    ):
        raise ValueError("claim_timeout_seconds must be a positive integer")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cutoff = current.astimezone(timezone.utc) - timedelta(
        seconds=claim_timeout_seconds
    )

    def list_ids() -> list[str]:
        try:
            return sorted(path.stem for path in store.root.glob("dlv_*.json"))
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list delivery records for recovery"
            ) from error

    recovered: list[ArtifactDeliveryRef] = []
    for delivery_id in await asyncio.to_thread(list_ids):
        record = await store.get(delivery_id)
        if (
            record.state != ArtifactDeliveryState.DELIVERING
            or record.delivering_at is None
            or record.delivering_at > cutoff
        ):
            continue
        try:
            updated = await store.transition(
                delivery_id,
                target=ArtifactDeliveryState.UNKNOWN,
                allowed_from={ArtifactDeliveryState.DELIVERING},
                error="delivery_claim_recovered_after_timeout",
                receipt={
                    "recovery": "startup_stale_claim",
                    "recovered_at": current.isoformat(),
                },
            )
        except ArtifactDeliveryError:
            # A live transport may have completed the record after the scan.
            continue
        recovered.append(updated.public_ref())
    return recovered
