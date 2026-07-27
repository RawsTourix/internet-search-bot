import hashlib
import unittest
from types import SimpleNamespace

import httpx

from src.servers.telegram.artifact_bridge import TelegramArtifactBridgeError
from src.servers.telegram.output_batch_gateway import TelegramClaimedOutputGateway


class TelegramClaimedOutputGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_scoped_stream_is_verified_and_filename_is_sanitized(self):
        payload = b"exact-output"
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "Content-Length": str(len(payload)),
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''../safe-result.bin"
                    ),
                    "X-Output-Batch-ID": "obat_" + "1" * 32,
                    "X-Delivery-ID": "dlv_" + "2" * 32,
                    "X-Content-Hash": digest,
                },
            )

        gateway = self._gateway(httpx.MockTransport(handler))
        spool, filename = await gateway.open_delivery_file(
            "dlv_" + "2" * 32,
            session_id="session-1",
        )
        try:
            self.assertEqual(spool.read(), payload)
            self.assertEqual(filename, "safe-result.bin")
        finally:
            spool.close()
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(
            request.url.path,
            "/internal/output-outbox/"
            + "obat_"
            + "1" * 32
            + "/deliveries/"
            + "dlv_"
            + "2" * 32
            + "/content",
        )
        self.assertEqual(request.url.params["session_id"], "session-1")
        self.assertEqual(request.url.params["client_instance_id"], "bot-1")
        self.assertEqual(request.headers["x-api-key"], "secret")

    async def test_delivery_identity_mismatch_is_rejected(self):
        payload = b"payload"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "Content-Length": str(len(payload)),
                    "X-Output-Batch-ID": "obat_" + "1" * 32,
                    "X-Delivery-ID": "dlv_" + "9" * 32,
                    "X-Content-Hash": (
                        "sha256:" + hashlib.sha256(payload).hexdigest()
                    ),
                },
            )

        with self.assertRaises(TelegramArtifactBridgeError):
            await self._gateway(
                httpx.MockTransport(handler)
            ).open_delivery_file(
                "dlv_" + "2" * 32,
                session_id="session-1",
            )

    async def test_missing_or_incorrect_hash_is_rejected(self):
        payload = b"payload"

        for headers in (
            {
                "Content-Length": str(len(payload)),
                "X-Output-Batch-ID": "obat_" + "1" * 32,
                "X-Delivery-ID": "dlv_" + "2" * 32,
            },
            {
                "Content-Length": str(len(payload)),
                "X-Output-Batch-ID": "obat_" + "1" * 32,
                "X-Delivery-ID": "dlv_" + "2" * 32,
                "X-Content-Hash": "sha256:" + "0" * 64,
            },
        ):
            with self.subTest(headers=headers):
                transport = httpx.MockTransport(
                    lambda request, exact=headers: httpx.Response(
                        200,
                        content=payload,
                        headers=exact,
                    )
                )
                with self.assertRaises(TelegramArtifactBridgeError):
                    await self._gateway(transport).open_delivery_file(
                        "dlv_" + "2" * 32,
                        session_id="session-1",
                    )

    def test_from_client_copies_only_connection_configuration(self):
        client = SimpleNamespace(
            gateway_url="http://gateway.test/",
            api_key="secret",
            transport=None,
            delivery_spool_memory_bytes=2048,
        )
        gateway = TelegramClaimedOutputGateway.from_client(
            client,
            output_batch_id="obat_" + "1" * 32,
            client_instance_id="bot-1",
        )
        self.assertEqual(gateway.gateway_url, "http://gateway.test")
        self.assertEqual(gateway.delivery_spool_memory_bytes, 2048)
        self.assertEqual(gateway.client_instance_id, "bot-1")

    @staticmethod
    def _gateway(transport):
        return TelegramClaimedOutputGateway(
            gateway_url="http://gateway.test",
            api_key="secret",
            output_batch_id="obat_" + "1" * 32,
            client_instance_id="bot-1",
            transport=transport,
            delivery_spool_memory_bytes=1024,
        )


if __name__ == "__main__":
    unittest.main()
