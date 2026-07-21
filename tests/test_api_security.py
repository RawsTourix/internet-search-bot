import unittest

from src.api.config import (
    safe_llm_config_summary,
    safe_memory_config_summary,
    safe_mcp_server_config_summary,
    safe_planning_config_summary,
    safe_runtime_config_summary,
)
from src.memory import MemoryConfigType
from src.mcp.mcp_client import LLMConfigType, ServerConfigType, ServerConnectType
from src.planning import PlanningConfigType
from src.runtime import RuntimeConfigType


class ApiConfigLoggingTests(unittest.TestCase):
    def test_log_summaries_exclude_sensitive_config_fields(self):
        llm_config = LLMConfigType(
            api_url="https://llm.example.test/v1/chat/completions",
            api_key="llm-secret-value",
            model="test-model",
            tokenizer_encoding="test-encoding",
            headers={"Authorization": "Bearer llm-header-secret"},
        )
        server_configs = [
            ServerConfigType(
                name="private-server",
                alias="private",
                connect_type=ServerConnectType.STREAMABLE_HTTP,
                url="http://127.0.0.1:8011/mcp/",
                headers={"Authorization": "Bearer mcp-header-secret"},
                env={"PRIVATE_TOKEN": "mcp-env-secret"},
                startup_required=False,
            )
        ]
        summaries = repr(
            {
                "llm": safe_llm_config_summary(llm_config),
                "servers": safe_mcp_server_config_summary(server_configs),
                "memory": safe_memory_config_summary(MemoryConfigType()),
                "runtime": safe_runtime_config_summary(RuntimeConfigType()),
                "planning": safe_planning_config_summary(PlanningConfigType()),
            }
        )

        self.assertNotIn("llm-secret-value", summaries)
        self.assertNotIn("llm-header-secret", summaries)
        self.assertNotIn("mcp-header-secret", summaries)
        self.assertNotIn("mcp-env-secret", summaries)
        self.assertIn("test-model", summaries)
        self.assertIn("test-encoding", summaries)
        self.assertIn("private-server", summaries)
        self.assertIn("enable_result_compaction", summaries)
        self.assertIn("result_summary_target_tokens", summaries)
        self.assertIn("result_compaction_max_output_tokens", summaries)
        self.assertIn("cycle_compaction_summary_target_tokens", summaries)
        self.assertIn("cycle_compaction_max_output_tokens", summaries)
        self.assertIn("mcp_runtime_close_timeout", summaries)
        self.assertIn("max_ready_nodes_in_context", summaries)
        self.assertIn("max_reconciliation_attempts", summaries)


if __name__ == "__main__":
    unittest.main()
