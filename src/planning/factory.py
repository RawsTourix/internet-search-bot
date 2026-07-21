"""Construction helpers for planning dependencies."""

from dataclasses import dataclass

from ..storage.config import StorageConfigType
from .config import PlanningConfigType
from .file_store import FileSystemPlanStore
from .interfaces import PlanStore
from .service import PlanningService


@dataclass(slots=True)
class PlanningServices:
    config: PlanningConfigType
    plan_store: PlanStore
    planning_service: PlanningService


def create_planning_services(
    *,
    storage_config: StorageConfigType,
    planning_config: PlanningConfigType,
) -> PlanningServices:
    plan_store = FileSystemPlanStore(
        storage_config=storage_config,
        planning_config=planning_config,
    )
    return PlanningServices(
        config=planning_config,
        plan_store=plan_store,
        planning_service=PlanningService(
            store=plan_store,
            config=planning_config,
        ),
    )
