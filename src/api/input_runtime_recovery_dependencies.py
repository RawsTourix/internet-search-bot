"""Local durable dependency validation/enrichment for IR-8 API startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..input_runtime.recovery import InputRuntimeRecoveryError
from ..planning.validation import build_active_plan_state
from ..storage.models import is_result_id


@dataclass(frozen=True, slots=True)
class RecoveredRuntimeDependencies:
    active_plan_states: dict[str, Any]


async def validate_recovered_runtime_dependencies(
    api: Any,
    recovery_plan: Any,
) -> RecoveredRuntimeDependencies:
    """Validate durable refs without LLM/MCP/transport side effects."""

    active_plan_states: dict[str, Any] = {}
    for session_plan in recovery_plan.sessions:
        snapshot = session_plan.snapshot
        if snapshot is None:
            continue

        # Artifact references are opaque durable IDs.  Existence is sufficient:
        # input artifacts may predate cycle creation and therefore need not have
        # the active cycle as their original storage owner.
        for artifact_id in dict.fromkeys(
            [*snapshot.artifact_refs, *snapshot.read_artifact_refs]
        ):
            try:
                await api.storage_services.artifact_store.get_artifact(artifact_id)
            except Exception as error:
                raise InputRuntimeRecoveryError(
                    "snapshot_artifact_reference_missing"
                ) from error

        # Result refs currently are opaque res_* identities embedded in the
        # durable tool protocol/content metadata; there is no independent v0.4
        # result repository port.  Validate their canonical identity and retain
        # them exactly.  Missing result payload needed by the LLM is separately
        # caught by message/tool protocol validation and content references.
        if any(not is_result_id(result_id) for result_id in snapshot.result_refs):
            raise InputRuntimeRecoveryError("snapshot_result_reference_invalid")

        if snapshot.active_plan_id is None:
            continue
        if snapshot.active_plan_revision is None:
            raise InputRuntimeRecoveryError("snapshot_plan_revision_missing")
        try:
            plan = await api.planning_services.plan_store.get_plan(
                snapshot.active_plan_id,
                revision=snapshot.active_plan_revision,
            )
        except Exception as error:
            raise InputRuntimeRecoveryError("snapshot_plan_reference_missing") from error
        if (
            plan.session_id != snapshot.session_id
            or plan.cycle_id != snapshot.cycle_id
            or plan.plan_id != snapshot.active_plan_id
            or plan.revision != snapshot.active_plan_revision
        ):
            raise InputRuntimeRecoveryError("snapshot_plan_ownership_mismatch")
        if snapshot.active_plan_node_id is not None and not any(
            node.node_id == snapshot.active_plan_node_id for node in plan.nodes
        ):
            raise InputRuntimeRecoveryError("snapshot_plan_node_missing")
        active_plan_states[snapshot.cycle_id] = build_active_plan_state(
            plan,
            max_ready_nodes=api.planning_services.config.max_ready_nodes_in_context,
        )

    return RecoveredRuntimeDependencies(
        active_plan_states=active_plan_states,
    )
