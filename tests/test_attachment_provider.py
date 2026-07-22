import unittest

import httpx

from src.api.artifact_transport import AttachmentProviderError
from src.api.attachment_provider import StrictHttpAttachmentStreamProvider


class StrictAttachmentProviderTests(unittest.IsolatedAsyncioTestCase):
    async def _read(self, provider, locator="file-id"):
        stream = await provider.open_stream(locator, max_size_bytes=1024)
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        return b"".join(chunks)

    async def test_exact_content_length_is_accepted(self):
        async def handler(request):
            self.assertEqual(
                request.headers["X-File-Provider-Token"],
                "secret",
            )
            return httpx.Response(
                200,
                content=b"exact",
                headers={"Content-Length": "5"},
            )

        provider = StrictHttpAttachmentStreamProvider(
            base_url="https://provider.example",
            token="secret",
            provider_name="telegram",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(await self._read(provider), b"exact")

    async def test_truncated_and_overlong_streams_are_rejected(self):
        for payload, declared in ((b"short", "10"), (b"too-long", "3")):
            with self.subTest(payload=payload, declared=declared):
                async def handler(request, payload=payload, declared=declared):
                    class Stream(httpx.AsyncByteStream):
                        async def __aiter__(self):
                            yield payload

                    return httpx.Response(
                        200,
                        stream=Stream(),
                        headers={"Content-Length": declared},
                    )

                provider = StrictHttpAttachmentStreamProvider(
                    base_url="https://provider.example",
                    token="secret",
                    provider_name="telegram",
                    transport=httpx.MockTransport(handler),
                )
                with self.assertRaises(AttachmentProviderError):
                    await self._read(provider)

    async def test_locator_is_percent_encoded_and_control_chars_rejected(self):
        requested_path = None

        async def handler(request):
            nonlocal requested_path
            requested_path = request.url.raw_path.decode("ascii")
            return httpx.Response(200, content=b"x")

        provider = StrictHttpAttachmentStreamProvider(
            base_url="https://provider.example",
            token="secret",
            provider_name="telegram",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(await self._read(provider, "a/b+c"), b"x")
        self.assertEqual(requested_path, "/internal/files/a%2Fb%2Bc")

        with self.assertRaises(AttachmentProviderError):
            await provider.open_stream("bad\nlocator", max_size_bytes=1024)


if __name__ == "__main__":
    unittest.main()
