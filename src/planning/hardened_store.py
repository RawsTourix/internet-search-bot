"""Additional integrity checks for the filesystem plan backend."""

from __future__ import annotations

from .errors import PlanStorageError
from .file_store import FileSystemPlanStore


class VerifiedFileSystemPlanStore(FileSystemPlanStore):
    """Reject revisions whose stable ownership disagrees with metadata."""

    def _get_plan_sync(self, plan_id: str, revision: int | None):
        metadata = self._load_metadata(plan_id)
        plan = super()._get_plan_sync(plan_id, revision)
        if (
            plan.session_id != metadata.session_id
            or plan.cycle_id != metadata.cycle_id
        ):
            raise PlanStorageError(
                f"Plan revision ownership mismatch for {plan_id}"
            )
        if plan.revision == metadata.current_revision:
            if plan.status != metadata.status:
                raise PlanStorageError(
                    f"Current plan status mismatch for {plan_id}"
                )
            if plan.updated_at != metadata.updated_at:
                raise PlanStorageError(
                    f"Current plan timestamp mismatch for {plan_id}"
                )
        return plan
