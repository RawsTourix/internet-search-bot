import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from src.api.artifact_routes import create_artifact_router


class FakeFacade:
    def __init__(self):
        self.commit_calls = 0
        self.run_calls = 0
        self.batch = SimpleNamespace(
            input_batch_id="ibat_" + "a" * 32,
            session_id="telegram:conversation:1",
            sequence_number=1,
            artifact_refs=["art_" + "b" * 32],
            text_parts=[],
            committed_at=datetime.now(timezone.utc),
        )

    async def commit_grouped_batch(self, input_batch_id, *, session_id):
        self.commit_calls += 1
        self.assert_authority(input_batch_id, session_id)
        return self.batch, self.commit_calls > 1

    async def run_committed_batch(
        self,
        input_batch_id,
        *,
        session_id,
        progress_callback=None,
        progress_locale="ru",
    ):
        self.assert_authority(input_batch_id, session_id)
        self.run_calls += 1
        return SimpleNamespace(
            content="done",
            metadata={"run": self.run_calls},
        )

    def assert_authority(self, input_batch_id, session_id):
        if input_batch_id != self.batch.input_batch_id:
            raise AssertionError("unexpected batch")
        if session_id != self.batch.session_id:
            raise AssertionError("unexpected session")


async def allow_request():
    return True


class ArtifactRouteIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_commit_does_not_run_agent_twice(self):
        facade = FakeFacade()
        app = FastAPI()
        app.include_router(
            create_artifact_router(
                facade=facade,
                auth_dependency=allow_request,
            )
        )
        transport = httpx.ASGITransport(app=app)
        body = {
            "session_id": facade.batch.session_id,
            "progress_locale": "ru",
            "run": True,
        }
        url = f"/input-batches/{facade.batch.input_batch_id}/commit"

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = await client.post(url, json=body)
            second = await client.post(url, json=body)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["response"], "done")
        self.assertFalse(first.json()["run_skipped_duplicate"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertTrue(second.json()["run_skipped_duplicate"])
        self.assertNotIn("response", second.json())
        self.assertEqual(facade.commit_calls, 2)
        self.assertEqual(facade.run_calls, 1)


if __name__ == "__main__":
    unittest.main()
