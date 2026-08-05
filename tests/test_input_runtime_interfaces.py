import inspect

from src.input_runtime import interfaces

PORTS = [
    interfaces.SessionInputRuntimeRepository,
    interfaces.InputAdmissionRepository,
    interfaces.CycleInboxRepository,
    interfaces.SessionControlRepository,
    interfaces.ActiveCycleSnapshotRepository,
    interfaces.ContextRevisionRepository,
    interfaces.AgentEmissionRepository,
    interfaces.FinalizationRepository,
]

EXPECTED = {
    interfaces.SessionInputRuntimeRepository: {"create_if_absent", "get", "compare_and_swap", "list_states"},
    interfaces.InputAdmissionRepository: {"create_if_absent", "get_by_input_batch_id", "allocate", "mark_applied", "cancel", "list_for_session", "list_unapplied", "cancel_generation"},
    interfaces.CycleInboxRepository: {"create_if_absent", "claim_contiguous_range", "mark_applying", "mark_applied", "requeue_claim", "list_for_cycle", "recover_expired_claims", "cancel_generation"},
    interfaces.SessionControlRepository: {"append", "get_by_idempotency_key", "acknowledge", "apply", "reject", "list_pending", "cancel_generation"},
    interfaces.ActiveCycleSnapshotRepository: {"create_if_absent", "get", "compare_and_swap", "list_active", "list_resumable", "cancel_generation"},
    interfaces.ContextRevisionRepository: {"append_revision", "get", "get_latest", "list_for_cycle"},
    interfaces.AgentEmissionRepository: {"create_if_absent", "get_by_idempotency_key", "claim_delivery", "complete_delivery", "fail_delivery", "list_pending_delivery", "cancel_generation"},
    interfaces.FinalizationRepository: {"prepare", "get", "advance", "abort", "list_recoverable", "cancel_generation"},
}


def methods(port):
    return {name for name, value in inspect.getmembers(port, inspect.isfunction) if not name.startswith("_")}


def test_ports_are_runtime_checkable_command_protocols():
    assert all(getattr(port, "_is_protocol", False) for port in PORTS)
    for port, expected in EXPECTED.items():
        assert expected <= methods(port)
        assert not (methods(port) & {"save", "load", "patch", "execute"})


def test_cas_ports_require_expected_revision():
    for port in (interfaces.SessionInputRuntimeRepository, interfaces.ActiveCycleSnapshotRepository):
        assert "expected_revision" in inspect.signature(port.compare_and_swap).parameters


def test_inbox_claim_transitions_are_fenced_by_typed_claim():
    for name in ("mark_applying", "mark_applied", "requeue_claim"):
        parameter = inspect.signature(getattr(interfaces.CycleInboxRepository, name)).parameters["claim"]
        assert "ClaimedInboxRange" in str(parameter.annotation)


def test_delivery_transitions_require_claim_token():
    for name in ("complete_delivery", "fail_delivery"):
        assert "claim_token" in inspect.signature(getattr(interfaces.AgentEmissionRepository, name)).parameters


def test_finalization_transitions_are_state_fenced():
    for name in ("advance", "abort"):
        assert "expected_state" in inspect.signature(getattr(interfaces.FinalizationRepository, name)).parameters


def test_generation_cancellation_and_recovery_listing_exist_before_backend():
    for port in (interfaces.InputAdmissionRepository, interfaces.CycleInboxRepository,
                 interfaces.SessionControlRepository, interfaces.ActiveCycleSnapshotRepository,
                 interfaces.AgentEmissionRepository, interfaces.FinalizationRepository):
        assert "cancel_generation" in methods(port)
    assert "recover_expired_claims" in methods(interfaces.CycleInboxRepository)
    assert "list_resumable" in methods(interfaces.ActiveCycleSnapshotRepository)
    assert "list_recoverable" in methods(interfaces.FinalizationRepository)
