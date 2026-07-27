import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.legacy_delivery_guard import (
    LegacyTelegramDeliveryGuardMiddleware,
)


class LegacyTelegramDeliveryGuardTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(
            LegacyTelegramDeliveryGuardMiddleware,
            internal_api_key="internal-secret",
        )

        @app.get("/internal/deliveries/{delivery_id}/content")
        async def legacy_content(delivery_id: str, client_type: str):
            return {"delivery_id": delivery_id, "client_type": client_type}

        @app.post("/internal/output-batches/{output_batch_id}/claim")
        async def legacy_output_claim(output_batch_id: str):
            return {"output_batch_id": output_batch_id}

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_telegram_must_use_scoped_output_batch_route(self):
        response = self.client.get(
            "/internal/deliveries/dlv_example/content",
            params={"client_type": "telegram"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("OutputBatch", response.json()["detail"])

    def test_other_transports_keep_compatibility_byte_route(self):
        response = self.client.get(
            "/internal/deliveries/dlv_example/content",
            params={"client_type": "web"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_type"], "web")

    def test_transport_key_cannot_use_legacy_aggregate_route(self):
        response = self.client.post(
            "/internal/output-batches/obat_example/claim",
            headers={"X-API-Key": "telegram-secret"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("instance-scoped", response.json()["detail"])

    def test_internal_key_keeps_legacy_aggregate_compatibility(self):
        response = self.client.post(
            "/internal/output-batches/obat_example/claim",
            headers={"X-API-Key": "internal-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_batch_id"], "obat_example")


if __name__ == "__main__":
    unittest.main()
