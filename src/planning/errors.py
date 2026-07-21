"""Domain errors for DAG planning."""

from __future__ import annotations

from typing import Any


class PlanningError(RuntimeError):
    """Base error for planning domain and persistence failures."""


class PlanNotFoundError(PlanningError):
    """Raised when a requested plan or revision does not exist."""


class PlanStorageError(PlanningError):
    """Raised when the plan store cannot complete an operation."""


class PlanAccessError(PlanningError):
    """Raised when a plan does not belong to the current session/cycle."""


class PlanConsistencyError(PlanningError):
    """Raised when runtime cannot reconcile an active plan safely."""


class PlanValidationError(PlanningError):
    """Structured domain validation failure suitable for manager-tool output."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.details = dict(details or {})


class PlanRevisionConflictError(PlanningError):
    """Optimistic concurrency conflict."""

    def __init__(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        super().__init__(
            f"Plan revision conflict for {plan_id}: "
            f"expected {expected_revision}, current {current_revision}"
        )
        self.plan_id = plan_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
