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


def test_ports_are_runtime_checkable_protocols():
    assert all(getattr(port, "_is_protocol", False) for port in PORTS)


def test_ports_expose_command_oriented_methods_only():
    forbidden = {"save", "load", "patch", "execute"}
    for port in PORTS:
        methods = {
            name
            for name, value in inspect.getmembers(port, inspect.isfunction)
            if not name.startswith("_")
        }
        assert not (methods & forbidden)


def test_claim_terminal_transitions_require_typed_claim():
    for method_name in ("mark_applying", "mark_applied", "requeue_claim"):
        signature = inspect.signature(getattr(interfaces.CycleInboxRepository, method_name))
        assert "claim" in signature.parameters


def test_delivery_terminal_transitions_require_claim_token():
    for method_name in ("complete_delivery", "fail_delivery"):
        signature = inspect.signature(getattr(interfaces.AgentEmissionRepository, method_name))
        assert "claim_token" in signature.parameters
