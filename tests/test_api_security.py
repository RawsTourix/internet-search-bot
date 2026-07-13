import unittest

from src.api.config import (
    safe_llm_config_summary,
    safe_mcp_server_config_summary,
)
from src.mcp.mcp_client import LLMConfigType, ServerConfigType, ServerConnectType


class ApiConfigLoggingTests(unittest.TestCase):
    def test_log_summaries_exclude_sensitive_config_fields(self):
        llm_config = LLMConfigType(
            api_url="https://llm.example.test/v1/chat/completions",
            api_key="llm-secret-value",
            model="test-model",
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
            }
        )

        self.assertNotIn("llm-secret-value", summaries)
        self.assertNotIn("llm-header-secret", summaries)
        self.assertNotIn("mcp-header-secret", summaries)
        self.assertNotIn("mcp-env-secret", summaries)
        self.assertIn("test-model", summaries)
        self.assertIn("private-server", summaries)


if __name__ == "__main__":
    unittest.main()
