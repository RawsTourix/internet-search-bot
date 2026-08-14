"""IR-8 local final-output recovery and OUTPUT_READY evidence validation."""

from __future__ import annotations

from typing import Any

from ..core.models import AgentResult
from ..input_runtime.recovery import InputRuntimeRecoveryError
from ..interaction.output_models import OutputBatchKind


class FinalOutputRecovery:
    """Recover/validate final output without transport delivery."""

    def __init__(self, api: Any) -> None:
        self.api = api

    async def validate_output_ready(self, record) -> None:
        if record.output_batch_id is None:
            raise InputRuntimeRecoveryError("finalization_output_identity_missing")
        try:
            output = await self.api.output_store.get(record.output_batch_id)
        except Exception as error:
            raise InputRuntimeRecoveryError("finalization_output_missing") from error
        if (
            output.output_batch_id != record.output_batch_id
            or output.session_id != record.session_id
            or output.cycle_id != record.cycle_id
            or output.kind != OutputBatchKind.FINAL
        ):
            raise InputRuntimeRecoveryError("finalization_output_identity_mismatch")

    async def recover_final_output(
        self,
        *,
        record,
        result_payload: dict[str, Any],
    ) -> str:
        snapshot = await self.api.input_runtime_repositories.snapshots.get(
            record.cycle_id
        )
        if snapshot is None:
            raise InputRuntimeRecoveryError("finalization_snapshot_missing")
        batch, capability_snapshot = await self.api._resolve_batch_and_capability(
            snapshot.original_input_batch_id,
            session_id=record.session_id,
        )
        result = AgentResult.model_validate(result_payload)
        if result.session_id not in {None, record.session_id}:
            raise InputRuntimeRecoveryError("persisted_result_session_mismatch")
        if result.cycle_id not in {None, record.cycle_id}:
            raise InputRuntimeRecoveryError("persisted_result_cycle_mismatch")
        result.session_id = record.session_id
        result.cycle_id = record.cycle_id
        output = await self.api.output_assembler.assemble_final(
            result=result,
            input_batch=batch,
            capability_snapshot=capability_snapshot,
            locale=batch.locale or "ru",
        )
        if (
            record.output_batch_id is not None
            and output.output_batch_id != record.output_batch_id
        ):
            raise InputRuntimeRecoveryError("finalization_output_identity_mismatch")
        return output.output_batch_id
