import asyncio
import hashlib
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from telegram import InputFile
from telegram.error import BadRequest, TimedOut

from src.servers.telegram.artifact_bridge import (
    DebouncedBatchRunner,
    TelegramArtifactGatewayClient,
    build_telegram_input_envelope,
    extract_telegram_attachments,
    telegram_session_id,
)


class FakeBot:
    def __init__(self, *, send_error=None, send_errors=None):
        self.send_error = send_error
        self.send_errors = list(send_errors or [])
        self.sent = []
        self.calls = []

    async def send_document(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.send_errors:
            error = self.send_errors.pop(0)
            if error is not None:
                raise error
        if self.send_error is not None:
            raise self.send_error
        document = kwargs["document"]
        if isinstance(document, InputFile):
            payload = document.input_file_content
            if hasattr(payload, "read"):
                payload = payload.read()
        else:
            payload = document.read()
        self.sent.append({**kwargs, "payload": payload})
        return SimpleNamespace(
            message_id=77,
            document=SimpleNamespace(
                file_id="telegram-file-id",
                file_unique_id="telegram-unique-id",
            ),
        )

    async def get_file(self, file_id):
        return SimpleNamespace(
            file_path="https://telegram.example/files/exact.bin",
            file_size=5,
        )


class TelegramArtifactBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_attachment_extraction_uses_exact_document_and_largest_photo(self):
        document_message = SimpleNamespace(
            message_id=10,
            document=SimpleNamespace(
                file_id="file-1",
                file_unique_id="unique-1",
                file_name="../../report.pdf",
                mime_type="application/pdf",
                file_size=123,
            ),
            animation=None,
            audio=None,
            video=None,
            voice=None,
            video_note=None,
            photo=[],
        )
        extracted = extract_telegram_attachments(document_message)
        self.assertEqual(extracted[0]["filename"], "report.pdf")
        self.assertEqual(extracted[0]["file_id"], "file-1")

        photos = [
            SimpleNamespace(
                file_id="small",
                file_unique_id="u1",
                file_size=10,
                width=100,
                height=100,
            ),
            SimpleNamespace(
                file_id="large",
                file_unique_id="u2",
                file_size=20,
                width=200,
                height=200,
            ),
        ]
        photo_message = SimpleNamespace(
            message_id=11,
            document=None,
            animation=None,
            audio=None,
            video=None,
            voice=None,
            video_note=None,
            photo=photos,
        )
        extracted = extract_telegram_attachments(photo_message)
        self.assertEqual(extracted[0]["file_id"], "large")
        self.assertEqual(extracted[0]["mime_type"], "image/jpeg")

    def test_build_envelope_preserves_opaque_locator_and_authority(self):
        message = SimpleNamespace(
            message_id=15,
            document=SimpleNamespace(
                file_id="opaque-file-id",
                file_unique_id="unique",
                file_name="input.txt",
                mime_type="text/plain",
                file_size=5,
            ),
            animation=None,
            audio=None,
            video=None,
            voice=None,
            video_note=None,
            photo=[],
            caption="Read this",
            media_group_id="album-1",
            message_thread_id=9,
            reply_to_message=None,
            date=datetime.now(timezone.utc),
        )
        update = SimpleNamespace(
            update_id=100,
            effective_message=message,
            effective_user=SimpleNamespace(
                id=7,
                full_name="User",
                language_code="ru",
            ),
            effective_chat=SimpleNamespace(id=-1001),
        )
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id="bot-1",
            response_metadata={"status_message_id": 20},
        )
        slot = envelope.attachment_slots[0]
        self.assertEqual(slot.transport_locator.provider, "telegram")
        self.assertEqual(slot.transport_locator.locator, "opaque-file-id")
        serialized = json.dumps(envelope.model_dump(mode="json"))
        self.assertNotIn("api.telegram.org", serialized)
        self.assertEqual(envelope.source_group_id, "album-1")
        self.assertEqual(envelope.conversation.thread_id, "9")
        self.assertEqual(
            telegram_session_id(-1001, 9),
            "telegram:conversation:-1001:thread:9",
        )

    async def test_debounce_resets_waiting_task_but_not_running_callback(self):
        runner = DebouncedBatchRunner()
        calls = []
        running = asyncio.Event()
        release = asyncio.Event()

        async def first():
            calls.append("first")

        async def second():
            calls.append("second")
            running.set()
            await release.wait()

        await runner.schedule("album", delay_seconds=0.05, callback=first)
        await runner.schedule("album", delay_seconds=0.01, callback=second)
        await asyncio.wait_for(running.wait(), timeout=1)
        scheduled = await runner.schedule(
            "album",
            delay_seconds=0,
            callback=first,
        )
        self.assertFalse(scheduled)
        release.set()
        await asyncio.sleep(0.03)
        self.assertEqual(calls, ["second"])
        await runner.cancel_all()

    async def test_open_telegram_file_streams_exact_bytes(self):
        async def handler(request):
            self.assertEqual(request.url.host, "telegram.example")
            return httpx.Response(200, content=b"exact")

        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
        )
        stream = await client.open_telegram_file(
            FakeBot(),
            "file-id",
            transport=httpx.MockTransport(handler),
        )
        chunks = []
        async for chunk in stream.iterator:
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), b"exact")
        self.assertEqual(stream.size_bytes, 5)

    async def test_open_telegram_file_retries_with_fresh_file_url(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("temporary timeout", request=request)
            return httpx.Response(200, content=b"exact")

        class RefreshingBot(FakeBot):
            def __init__(self):
                super().__init__()
                self.get_file_calls = 0

            async def get_file(self, file_id):
                self.get_file_calls += 1
                return await super().get_file(file_id)

        bot = RefreshingBot()
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
        )
        stream = await client.open_telegram_file(
            bot,
            "file-id",
            transport=httpx.MockTransport(handler),
            base_backoff_seconds=0,
        )
        body = b"".join([chunk async for chunk in stream.iterator])
        self.assertEqual(body, b"exact")
        self.assertEqual(attempts, 2)
        self.assertEqual(bot.get_file_calls, 2)

    async def test_open_telegram_file_exhausts_bounded_retries_once(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("temporary timeout", request=request)

        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
        )
        with self.assertRaisesRegex(RuntimeError, "after retries"):
            await client.open_telegram_file(
                FakeBot(),
                "file-id",
                transport=httpx.MockTransport(handler),
                max_attempts=3,
                base_backoff_seconds=0,
            )
        self.assertEqual(attempts, 3)

    async def test_delivery_success_verifies_bytes_and_posts_receipt(self):
        data = b"report body"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        requests = []

        async def handler(request):
            requests.append(request)
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Content-Hash": digest,
                        "Content-Disposition": (
                            "attachment; filename*=UTF-8''report.md"
                        ),
                    },
                )
            if request.url.path.endswith("/complete"):
                return httpx.Response(200, json={"state": "delivered"})
            return httpx.Response(404)

        bot = FakeBot()
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "a" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
            reply_to_message_id=10,
        )
        self.assertEqual(outcomes[0].state, "delivered")
        self.assertEqual(bot.sent[0]["payload"], data)
        self.assertIsInstance(bot.sent[0]["document"], InputFile)
        self.assertEqual(bot.sent[0]["document"].filename, "report.md")
        self.assertTrue(bot.sent[0]["allow_sending_without_reply"])
        complete = next(
            request for request in requests
            if request.url.path.endswith("/complete")
        )
        receipt = json.loads(complete.content)
        self.assertEqual(receipt["receipt"]["message_id"], 77)

    async def test_missing_reply_target_retries_without_reply_metadata(self):
        data = b"report"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()

        async def handler(request):
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Content-Hash": digest,
                    },
                )
            if request.url.path.endswith("/complete"):
                return httpx.Response(200, json={"state": "delivered"})
            return httpx.Response(404)

        bot = FakeBot(send_errors=[
            BadRequest("Message to be replied not found"),
            None,
        ])
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "e" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
            reply_to_message_id=10,
        )
        self.assertEqual(outcomes[0].state, "delivered")
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(bot.calls[0]["reply_to_message_id"], 10)
        self.assertNotIn("reply_to_message_id", bot.calls[1])

    async def test_send_timeout_is_ambiguous_and_never_retried(self):
        data = b"file"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        failures = []

        async def handler(request):
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Content-Hash": digest,
                    },
                )
            if request.url.path.endswith("/failed"):
                failures.append(json.loads(request.content))
                return httpx.Response(200, json={"state": "unknown"})
            return httpx.Response(404)

        bot = FakeBot(send_error=TimedOut("timeout"))
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "b" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
        )
        self.assertEqual(outcomes[0].state, "unknown")
        self.assertTrue(failures[0]["ambiguous"])
        self.assertEqual(len(bot.sent), 0)

    async def test_generic_send_failure_is_ambiguous_and_keeps_details(self):
        data = b"file"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        failures = []

        async def handler(request):
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Content-Hash": digest,
                    },
                )
            if request.url.path.endswith("/failed"):
                failures.append(json.loads(request.content))
                return httpx.Response(200, json={"state": "unknown"})
            return httpx.Response(404)

        bot = FakeBot(send_error=RuntimeError("upload backend rejected file handle"))
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "f" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
        )
        self.assertEqual(outcomes[0].state, "unknown")
        self.assertTrue(failures[0]["ambiguous"])
        self.assertIn("upload backend rejected file handle", failures[0]["error"])

    async def test_successful_send_with_lost_complete_receipt_becomes_unknown(self):
        data = b"file"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        failures = []

        async def handler(request):
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Content-Hash": digest,
                    },
                )
            if request.url.path.endswith("/complete"):
                return httpx.Response(503)
            if request.url.path.endswith("/failed"):
                failures.append(json.loads(request.content))
                return httpx.Response(200, json={"state": "unknown"})
            return httpx.Response(404)

        bot = FakeBot()
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "c" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
        )
        self.assertEqual(outcomes[0].state, "unknown")
        self.assertEqual(outcomes[0].telegram_message_id, 77)
        self.assertTrue(failures[0]["ambiguous"])
        self.assertEqual(failures[0]["receipt"]["message_id"], 77)
        self.assertEqual(len(bot.sent), 1)

    async def test_hash_mismatch_fails_before_telegram_send(self):
        failures = []

        async def handler(request):
            if request.url.path.endswith("/content"):
                return httpx.Response(
                    200,
                    content=b"changed",
                    headers={
                        "Content-Length": "7",
                        "X-Content-Hash": "sha256:" + "0" * 64,
                    },
                )
            if request.url.path.endswith("/failed"):
                failures.append(json.loads(request.content))
                return httpx.Response(200, json={"state": "failed"})
            return httpx.Response(404)

        bot = FakeBot()
        client = TelegramArtifactGatewayClient(
            gateway_url="https://gateway.example",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        outcomes = await client.deliver_selected(
            bot=bot,
            artifacts=[{"delivery_id": "dlv_" + "d" * 32, "state": "selected"}],
            session_id="telegram:conversation:1",
            chat_id=1,
        )
        self.assertEqual(outcomes[0].state, "failed")
        self.assertFalse(failures[0]["ambiguous"])
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
