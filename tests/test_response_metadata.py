import json
import unittest
from enum import Enum
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from src.core.response_metadata import agent_result_metadata


class Status(str, Enum):
    DONE = "done"


class FakeProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    message: str


class FakeDeliveryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    artifact_id: str
    filename: str
    state: str


class ResponseMetadataTests(unittest.TestCase):
    def test_agent_metadata_is_plain_json_and_keeps_delivery_refs(self):
        result = SimpleNamespace(
            status=Status.DONE,
            session_id="telegram:conversation:1",
            iterations=3,
            tools_used=["artifact_set_delivery"],
            error=None,
            error_kind=None,
            can_resume=False,
            progress_events=[
                FakeProgressEvent(
                    type="artifact_delivery_selected",
                    message="Selected",
                )
            ],
            artifacts=[
                FakeDeliveryRef(
                    delivery_id="dlv_" + "a" * 32,
                    artifact_id="art_" + "b" * 32,
                    filename="report.md",
                    state="selected",
                )
            ],
        )

        metadata = agent_result_metadata(result)
        encoded = json.dumps(metadata, ensure_ascii=False)

        self.assertIn('"agent_status": "done"', encoded)
        self.assertEqual(metadata["progress_events"][0]["type"], "artifact_delivery_selected")
        self.assertEqual(metadata["artifacts"][0]["filename"], "report.md")
        self.assertEqual(metadata["artifacts"][0]["state"], "selected")
        self.assertIsInstance(metadata["progress_events"][0], dict)
        self.assertIsInstance(metadata["artifacts"][0], dict)

    def test_empty_collections_remain_json_safe(self):
        result = SimpleNamespace(
            status="error",
            session_id=None,
            iterations=0,
            tools_used=[],
            error="boom",
            error_kind="critical_error",
            can_resume=False,
            progress_events=[],
            artifacts=[],
        )

        metadata = agent_result_metadata(result)

        self.assertEqual(metadata["agent_status"], "error")
        self.assertEqual(metadata["progress_events"], [])
        self.assertEqual(metadata["artifacts"], [])
        json.dumps(metadata)


if __name__ == "__main__":
    unittest.main()
