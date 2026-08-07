import asyncio
from types import SimpleNamespace

from src.input_runtime.emissions import ManagerToolExecutionContext
from src.mcp.input_runtime_checkpoints import _checkpoint_active_cycle
from src.mcp.input_runtime_emissions import InputRuntimeEmissionMixin


class Base:
    def _build_manager_tools(self):
        return {}

    def _tool_start_message(self, tool_name, arguments, *, progress_locale="ru"):
        return "base"

    def _tool_result_payload(self, tool_name, tool_result):
        return {"type": "base"}


class Probe(InputRuntimeEmissionMixin, Base):
    def __init__(self):
        self.manager_tools = self._build_manager_tools()


def active_cycle(session, cycle, revision, call_id, *, generation=3, batch="batch"):
    return SimpleNamespace(
        session_id=session,
        cycle_id=cycle,
        active_context_revision_id=revision,
        input_runtime_generation=generation,
        original_input_batch_id=batch,
        cycle_trace=[
            {"type": "tool_call", "tool_name": "other", "tool_call_id": "other"},
            {
                "type": "tool_call",
                "tool_name": "send_user_message",
                "tool_call_id": call_id,
            },
        ],
    )


def test_manager_tool_schema_contains_only_semantic_arguments():
    spec = Probe().manager_tools["send_user_message"]
    properties = spec.parameters["properties"]
    assert set(properties) == {"message", "kind", "importance"}
    assert spec.parameters["additionalProperties"] is False
    assert properties["kind"]["enum"] == ["intermediate"]
    forbidden = {
        "session_id", "cycle_id", "generation", "context_revision_id",
        "chat_id", "conversation_id", "reply_to_message_id", "emission_id",
        "idempotency_key", "client_instance_id", "capability_snapshot_id",
    }
    assert not forbidden.intersection(properties)


def test_manager_tool_description_distinguishes_semantic_update_from_progress_question_final():
    description = Probe().manager_tools["send_user_message"].description.lower()
    assert "partial result" in description
    assert "debug" in description
    assert "ask_user" in description
    assert "финаль" in description
    assert "кажд" in description


def test_exact_runtime_context_comes_from_scoped_active_cycle():
    probe = Probe()
    cycle = active_cycle("s1", "c1", "ctxrev_" + "1" * 32, "call-7")
    token = _checkpoint_active_cycle.set(cycle)
    try:
        actual = probe._manager_tool_execution_context()
    finally:
        _checkpoint_active_cycle.reset(token)
    assert actual == ManagerToolExecutionContext(
        session_id="s1",
        cycle_id="c1",
        generation=3,
        context_revision_id="ctxrev_" + "1" * 32,
        tool_call_id="call-7",
        original_input_batch_id="batch",
    )


def test_missing_native_tool_call_identity_is_controlled_context_failure():
    probe = Probe()
    cycle = active_cycle("s1", "c1", "ctxrev_" + "1" * 32, "call-7")
    cycle.cycle_trace = []
    token = _checkpoint_active_cycle.set(cycle)
    try:
        assert probe._manager_tool_execution_context() is None
    finally:
        _checkpoint_active_cycle.reset(token)


def test_two_concurrent_sessions_do_not_bleed_context():
    probe = Probe()

    async def one(session, cycle, marker):
        token = _checkpoint_active_cycle.set(
            active_cycle(
                session,
                cycle,
                "ctxrev_" + marker * 32,
                f"call-{marker}",
                batch=f"batch-{marker}",
            )
        )
        try:
            await asyncio.Event().wait() if False else asyncio.sleep(0)
            return probe._manager_tool_execution_context()
        finally:
            _checkpoint_active_cycle.reset(token)

    async def race():
        return await asyncio.gather(one("s-a", "c-a", "a"), one("s-b", "c-b", "b"))

    left, right = asyncio.run(race())
    assert (left.session_id, left.cycle_id, left.tool_call_id) == ("s-a", "c-a", "call-a")
    assert (right.session_id, right.cycle_id, right.tool_call_id) == ("s-b", "c-b", "call-b")


def test_unscoped_direct_manager_call_cannot_manufacture_authority():
    result = asyncio.run(Probe()._manager_send_user_message_unscoped({"message": "hello"}))
    assert result["accepted"] is False
    assert result["reason_code"] == "runtime_context_unavailable"


def test_agent_emission_tool_result_is_compact_trusted_runtime_evidence():
    probe = Probe()
    payload = probe._tool_result_payload(
        "send_user_message",
        '{"type":"agent_emission_result","accepted":true,"emission_id":"emit_123"}',
    )
    assert payload["type"] == "agent_emission_result"
    assert payload["trusted"] is True
    assert payload["runtime_generated"] is True
    assert "content" not in payload
