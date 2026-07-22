"""Task-local request identity used by artifact delivery and input binding."""

from contextvars import ContextVar, Token
from typing import Any


_REQUEST_CLIENT_TYPE: ContextVar[Any] = ContextVar(
    "artifact_request_client_type",
    default=None,
)
_REQUEST_CYCLE_IDENTITY: ContextVar[tuple[str, str] | None] = ContextVar(
    "artifact_request_cycle_identity",
    default=None,
)
_REQUEST_INPUT_BATCH: ContextVar[Any] = ContextVar(
    "artifact_request_input_batch",
    default=None,
)


def get_artifact_request_client_type() -> Any:
    return _REQUEST_CLIENT_TYPE.get()


def set_artifact_request_client_type(value: Any) -> Token:
    return _REQUEST_CLIENT_TYPE.set(value)


def reset_artifact_request_client_type(token: Token) -> None:
    _REQUEST_CLIENT_TYPE.reset(token)


def get_artifact_request_cycle_identity() -> tuple[str, str] | None:
    return _REQUEST_CYCLE_IDENTITY.get()


def set_artifact_request_cycle_identity(
    value: tuple[str, str] | None,
) -> Token:
    return _REQUEST_CYCLE_IDENTITY.set(value)


def reset_artifact_request_cycle_identity(token: Token) -> None:
    _REQUEST_CYCLE_IDENTITY.reset(token)


def get_artifact_request_input_batch() -> Any:
    return _REQUEST_INPUT_BATCH.get()


def set_artifact_request_input_batch(value: Any) -> Token:
    return _REQUEST_INPUT_BATCH.set(value)


def reset_artifact_request_input_batch(token: Token) -> None:
    _REQUEST_INPUT_BATCH.reset(token)
