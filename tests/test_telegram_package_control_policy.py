import json
import unittest

import httpx

from src.servers.telegram.artifact_bridge import TelegramArtifactGatewayClient


class TelegramPackageControlPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_gateway_uses_exact_outbox_claim_and_receipt_routes(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/claim"):
                return httpx.Response(
                    200,
                    json={
                        "output_batch": {"output_batch_id": "obat_example"},
                        "attempt_id": "odat_example",
                        "delivery_plan": {},
                    },
                )
            return httpx.Response(
                200,
                json={"output_batch_id": "obat_example", "state": "delivered"},
            )

        client = TelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="secret",
            transport=httpx.MockTransport(handler),
        )
        client.client_instance_id = "bot-1"

        claim = await client.claim_output_batch(
            "obat_example",
            session_id="session-1",
        )
        completed = await client.complete_output_batch(
            "obat_example",
            session_id="session-1",
            receipt={"state": "delivered"},
        )

        self.assertEqual(claim["attempt_id"], "odat_example")
        self.assertEqual(completed["state"], "delivered")
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/internal/output-outbox/obat_example/claim",
                "/internal/output-outbox/obat_example/receipt",
            ],
        )
        claim_body = json.loads(requests[0].content.decode("utf-8"))
        self.assertEqual(claim_body["session_id"], "session-1")
        self.assertEqual(claim_body["client_type"], "telegram")
        self.assertEqual(claim_body["client_instance_id"], "bot-1")
        self.assertTrue(claim_body["claim_request_id"].startswith("oclm_"))
        receipt_body = json.loads(requests[1].content.decode("utf-8"))
        self.assertEqual(receipt_body["client_instance_id"], "bot-1")
        self.assertEqual(receipt_body["receipt"], {"state": "delivered"})


if __name__ == "__main__":
    unittest.main()
