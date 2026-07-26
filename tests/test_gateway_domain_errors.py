import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.domain_errors import register_domain_exception_handlers
from src.interaction.errors import (
    InteractionStorageError,
    InteractionValidationError,
    OutputBatchConflictError,
    OutputBatchNotFoundError,
)


class GatewayDomainErrorMappingTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        register_domain_exception_handlers(app)

        @app.get("/missing")
        async def missing():
            raise OutputBatchNotFoundError("missing batch")

        @app.get("/conflict")
        async def conflict():
            raise OutputBatchConflictError("state conflict")

        @app.get("/invalid")
        async def invalid():
            raise InteractionValidationError("invalid payload")

        @app.get("/unavailable")
        async def unavailable():
            raise InteractionStorageError("storage unavailable")

        self.client = TestClient(app, raise_server_exceptions=False)

    def test_domain_errors_have_stable_http_mapping(self):
        for path, expected_status, expected_type in (
            ("/missing", 404, "OutputBatchNotFoundError"),
            ("/conflict", 409, "OutputBatchConflictError"),
            ("/invalid", 422, "InteractionValidationError"),
            ("/unavailable", 503, "InteractionStorageError"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json()["error_type"], expected_type)
                self.assertTrue(response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
