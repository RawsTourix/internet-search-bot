import inspect

from src.input_runtime import interfaces


EXPECTED_METHODS = {
    interfaces.SessionInputRuntimeRepository: {
        "create_if_absent", "get", "compare_and_swap",
    },
    interfaces.InputAdmissionRepository: {
        "create_if_absent", "get_by_input_batch_id", "allocate",
        "mark_applied", "cancel",
    },
    interfaces.CycleInboxRepository: {
        "create_if_absent", "claim_contiguous_range", "mark_applying",
        "mark_applied", "requeue_claim",
    },
    interfaces.SessionControlRepository: {
        "append", "get_by_idempotency_key", "acknowledge", "apply", "reject",
    },
    interfaces.ActiveCycleSnapshotRepository: {
        "create_if_absent", "get", "compare_and_swap",
    },
    interfaces.ContextRevisionRepository: {
        "append_revision", "get", "get_latest",
    },
    interfaces.AgentEmissionRepository: {
        "create_if_absent", "get_by_idempotency_key", "claim_delivery",
        "complete_delivery", "fail_delivery",
    },
    interfaces.FinalizationRepository: {
        "prepare", "get", "advance", "abort",
    },
}


def public_methods(port):
    return {
        name for name, value in inspect.getmembers(port, inspect.isfunction)
        if not name.startswith("_")
    }


def test_all_repository_ports_are_runtime_checkable_protocols():
    for port in EXPECTED_METHODS:
        assert getattr(port, "_is_protocol", False)
        assert getattr(port, "_is_runtime_protocol", False)


def test_all_repository_protocols_expose_exact_command_oriented_surface():
    forbidden = {"save", "load", "patch", "execute", "query"}
    for port, expected in EXPECTED_METHODS.items():
        methods = public_methods(port)
        assert methods == expected
        assert not methods.intersection(forbidden)


def test_compare_and_swap_ports_require_expected_revision():
    for port in (
        interfaces.SessionInputRuntimeRepository,
        interfaces.ActiveCycleSnapshotRepository,
    ):
        signature = inspect.signature(port.compare_and_swap)
        assert "expected_revision" in signature.parameters


def test_inbox_claim_transitions_require_typed_claim():
    for method_name in ("mark_applying", "mark_applied", "requeue_claim"):
        signature = inspect.signature(
            getattr(interfaces.CycleInboxRepository, method_name)
        )
        assert "claim" in signature.parameters
        assert signature.parameters["claim"].annotation is not inspect.Parameter.empty


def test_emission_delivery_transitions_require_claim_token():
    for method_name in ("complete_delivery", "fail_delivery"):
        signature = inspect.signature(
            getattr(interfaces.AgentEmissionRepository, method_name)
        )
        assert "claim_token" in signature.parameters


def test_finalization_transitions_are_state_fenced():
    for method_name in ("advance", "abort"):
        signature = inspect.signature(
            getattr(interfaces.FinalizationRepository, method_name)
        )
        assert "expected_state" in signature.parameters
        assert "next_record" in signature.parameters


def test_ports_do_not_import_infrastructure_frameworks():
    source = inspect.getsource(interfaces)
    for forbidden in ("telegram", "fastapi", "sqlalchemy", "redis", "MCPClient"):
        assert forbidden.lower() not in source.lower()
