import unittest

from scripts.audit_configuration_examples import (
    missing_config_example_fields,
    missing_config_example_sections,
    missing_environment_example_keys,
    unregistered_config_sections,
    unresolved_environment_reads,
    validate_config_example_values,
)


class ConfigurationExampleCoverageTests(unittest.TestCase):
    def test_environment_reads_are_statically_auditable(self):
        self.assertEqual(unresolved_environment_reads(), [])

    def test_env_example_covers_all_production_environment_reads(self):
        self.assertEqual(missing_environment_example_keys(), [])

    def test_mcp_config_example_covers_all_root_sections(self):
        self.assertEqual(missing_config_example_sections(), [])
        self.assertEqual(unregistered_config_sections(), [])

    def test_mcp_config_example_covers_every_model_field(self):
        self.assertEqual(missing_config_example_fields(), {})

    def test_mcp_config_example_values_validate(self):
        validate_config_example_values()


if __name__ == "__main__":
    unittest.main()
