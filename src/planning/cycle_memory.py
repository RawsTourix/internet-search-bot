"""Planning-aware cycle compaction adapter."""

from __future__ import annotations

from ..memory import CycleCompactionService, CycleWorkingState


class PlanningCycleCompactionService(CycleCompactionService):
    """Overlay runtime-owned plan identity around ordinary cycle compaction."""

    @staticmethod
    def _active_plan_projection(active_cycle):
        state = active_cycle.active_plan_state
        return state.model_dump(mode="json") if state is not None else None

    def build_request(self, **kwargs):
        kwargs.setdefault(
            "active_plan_state",
            self._active_plan_projection(kwargs["active_cycle"]),
        )
        return super().build_request(**kwargs)

    def build_request_for_content_id(self, **kwargs):
        kwargs.setdefault(
            "active_plan_state",
            self._active_plan_projection(kwargs["active_cycle"]),
        )
        return super().build_request_for_content_id(**kwargs)

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
