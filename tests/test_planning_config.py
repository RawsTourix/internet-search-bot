import json
import tempfile
import unittest
from pathlib import Path

from src.planning import PlanningConfigType, load_planning_config
from src.planning.errors import PlanningConfigValidationError


class PlanningConfigTests(unittest.TestCase):
    def test_missing_section_uses_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"llm": {}, "servers": []}), encoding="utf-8")
            config = load_planning_config(str(path))
        self.assertEqual(config, PlanningConfigType())

    def test_explicit_section_is_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps({
                    "planning": {
                        "enabled": False,
                        "max_nodes": 8,
                        "max_plan_get_limit": 8,
                    }
                }),
                encoding="utf-8",
            )
            config = load_planning_config(str(path))
        self.assertFalse(config.enabled)
        self.assertEqual(config.max_nodes, 8)

    def test_invalid_section_is_wrapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps({"planning": {"max_nodes": 2, "max_plan_get_limit": 3}}),
                encoding="utf-8",
            )
            with self.assertRaises(PlanningConfigValidationError):
                load_planning_config(str(path))
