"""Shared compatibility fixtures for pre-IR-8 runtime tests."""

from __future__ import annotations

import pytest

from src.api import input_runtime_controls as _control_api
from src.api import session_reset as _reset_api
from src.input_runtime.recovery import InputRuntimeReadinessGate


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

    Do not relax the production readiness boundary.  Instead, attach the same
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
