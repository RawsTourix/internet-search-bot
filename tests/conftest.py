"""Shared compatibility fixtures for staged input-runtime tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api import input_runtime_controls as _control_api
from src.api import session_reset as _reset_api
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.runtime.finalization_bridge import clear_finalization_bridge_for_tests


def _mark_api_ready_then(check):
    def wrapped(api):
        gate = getattr(api, "input_runtime_readiness_gate", None)
        if gate is None:
            gate = InputRuntimeReadinessGate()
            gate.begin_recovery()
            gate.mark_ready()
            api.input_runtime_readiness_gate = gate
        return check(api)

    return wrapped


@pytest.fixture(autouse=True)
def _legacy_ir5_api_shells_are_post_start_ready(request, monkeypatch):
    """IR-5 API-shell tests predate IR-8 but model normal post-start calls.

    Do not relax the production readiness boundary. Instead, attach the same
    real process-local READY gate that a successfully started Api owns before an
    IR-5 test invokes `/continue` or `/reset` through application helpers.
    IR-8 readiness tests are outside this module-name scope and still prove that
    absent/non-READY gates reject ordinary runtime work.
    """

    module_name = getattr(request.module, "__name__", "")
    if not module_name.startswith("test_input_runtime_ir5_"):
        return

    monkeypatch.setattr(
        _reset_api,
        "_require_runtime_ready",
        _mark_api_ready_then(_reset_api._require_runtime_ready),
    )
    monkeypatch.setattr(
        _control_api,
        "_require_runtime_ready",
        _mark_api_ready_then(_control_api._require_runtime_ready),
    )


@pytest.fixture(autouse=True)
def _standalone_output_tests_do_not_inherit_global_ir7_composition(request):
    """Standalone interaction stores must not inherit another test's Api bridge.

    IR-7 deliberately binds one process-local final-output eligibility checker
    for a complete production composition. The legacy interaction/outbox tests
    below construct independent temporary stores and intentionally exercise the
    compatibility boundary without an InputRuntime composition root. In a full
    repository run, an earlier Api import can otherwise leak its bridge into
    those stores and make test results collection-order dependent.
    """

    module_name = getattr(request.module, "__name__", "")
    standalone_modules = {
        "test_output_claim_idempotency",
        "test_output_outbox_api_authority",
        "test_ready_output_outbox",
        "test_web_output_delivery_hardening",
    }
    if module_name not in standalone_modules:
        return

    clear_finalization_bridge_for_tests()
    try:
        yield
    finally:
        clear_finalization_bridge_for_tests()


@pytest.fixture(autouse=True)
def _ir10_deterministic_harness(request, monkeypatch):
    """Keep IR-10 deterministic while preserving real recovery semantics.

    The filesystem repositories own a few UTC cleanup timestamps independently
    from the injected service clock, so the fixed IR-10 clock must precede the
    release run. The reset/crash scenario also models its post-restart input as
    genuinely new: it must not be visible in the committed store while startup
    recovery is reconciling the interrupted reset.
    """

    module_name = getattr(request.module, "__name__", "")
    if module_name != "test_input_runtime_ir10_release":
        return

    monkeypatch.setattr(
        request.module,
        "NOW",
        datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc),
    )

    if request.node.name != (
        "test_ir10_reset_crash_restart_finishes_once_and_old_generation_stays_fenced"
    ):
        return

    original_recover = request.module.recover

    async def recover_before_new_input(root, reader):
        future_input = reader.batches.pop("new-input", None)
        try:
            return await original_recover(root, reader)
        finally:
            if future_input is not None:
                reader.add(future_input)

    monkeypatch.setattr(request.module, "recover", recover_before_new_input)
