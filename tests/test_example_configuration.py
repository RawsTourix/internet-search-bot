import json
import unittest
from pathlib import Path

from src.artifacts import load_artifact_config
from src.ingress import load_ingress_config
from src.interaction.config import load_interaction_config
from src.mcp.mcp_client import load_config
from src.planning import load_planning_config


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
MCP_CONFIG_EXAMPLE = ROOT / "src" / "api" / "mcp.config.example"


class ExampleConfigurationTests(unittest.TestCase):
    def test_env_example_documents_runtime_environment_contract(self):
        names = {
            line.split("=", maxsplit=1)[0].strip()
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        }
        required = {
            "BOT_TOKEN",
            "WEBHOOK_DOMAIN",
            "WEBHOOK_SECRET",
            "TELEGRAM_API_KEY",
            "TELEGRAM_PROGRESS_CALLBACK_URL",
            "TELEGRAM_PROGRESS_CALLBACK_TOKEN",
            "TELEGRAM_FILE_PROVIDER_TOKEN",
            "TELEGRAM_FILE_PROVIDER_URL",
            "TELEGRAM_BOT_INSTANCE_ID",
            "TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS",
            "TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS",
            "TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES",
            "TELEGRAM_READY_OUTBOX_POLL_SECONDS",
            "TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS",
            "TELEGRAM_READY_OUTBOX_BATCH_LIMIT",
            "PROGRESS_EDIT_MIN_INTERVAL",
            "PROGRESS_MAX_TEXT_LENGTH",
            "TELEGRAM_FINAL_EDIT_MAX_LENGTH",
            "TELEGRAM_FINAL_DELIVERY_MODE",
            "TELEGRAM_SERVER_HOST",
            "TELEGRAM_SERVER_PORT",
            "WEB_API_KEY",
            "CLI_API_KEY",
            "GATEWAY_URL",
            "INTERNAL_API_KEY",
            "PROGRESS_CALLBACK_ALLOWED_PREFIXES",
            "CORS_ORIGINS",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "AGENT_CONFIG_PATH",
        }
        self.assertEqual(required - names, set())

    def test_mcp_config_example_is_complete_and_loadable(self):
        payload = json.loads(MCP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue({
            "servers",
            "llm",
            "runtime",
            "storage",
            "memory",
            "artifacts",
            "ingress",
            "client_capabilities",
            "localization",
            "input_presentation",
            "output_runtime",
            "telegram_output",
            "planning",
        }.issubset(payload))

        _, llm, storage, memory, runtime = load_config(
            str(MCP_CONFIG_EXAMPLE)
        )
        artifacts = load_artifact_config(str(MCP_CONFIG_EXAMPLE))
        ingress = load_ingress_config(str(MCP_CONFIG_EXAMPLE))
        interaction = load_interaction_config(str(MCP_CONFIG_EXAMPLE))
        planning = load_planning_config(str(MCP_CONFIG_EXAMPLE))

        self.assertEqual(llm.context_window_tokens, 262144)
        self.assertEqual(storage.root_dir, "storage")
        self.assertTrue(memory.enable_result_compaction)
        self.assertEqual(runtime.mcp_startup_timeout, 30)
        self.assertEqual(artifacts.delivery_claim_timeout_seconds, 900)
        self.assertEqual(ingress.media_group_maximum_wait_seconds, 300)
        self.assertEqual(
            interaction.input_presentation.reservation_timeout_seconds,
            30,
        )
        self.assertEqual(
            interaction.output_runtime.delivery_claim_timeout_seconds,
            900,
        )
        self.assertTrue(interaction.telegram_output.prefer_document_groups)
        self.assertEqual(planning.max_nodes, 32)


if __name__ == "__main__":
    unittest.main()
