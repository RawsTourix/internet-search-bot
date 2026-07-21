"""Planning-aware cycle compaction adapter."""

from __future__ import annotations

from ..memory import CycleCompactionService, CycleWorkingState


class PlanningCycleCompactionService(CycleCompactionService):
    """Overlay runtime-owned plan identity after ordinary cycle compaction."""

    def build_working_memory(self, **kwargs):
        active_cycle = kwargs["active_cycle"]
        memory = super().build_working_memory(**kwargs)
        state_payload = memory.working_state.model_dump(mode="python")

        if active_cycle.active_plan_id is not None:
            state_payload.update({
                "active_plan_id": active_cycle.active_plan_id,
                "active_plan_revision": active_cycle.active_plan_revision,
                "active_plan_node_id": active_cycle.active_plan_node_id,
            })
        else:
            state_payload.update({
                "active_plan_id": None,
                "active_plan_revision": None,
                "active_plan_node_id": None,
            })

        return memory.model_copy(
            update={
                "working_state": CycleWorkingState.model_validate(state_payload),
            }
        )
