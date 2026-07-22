"""Task-local client identity used by artifact delivery manager tools."""

from contextvars import ContextVar, Token
from typing import Any


_REQUEST_CLIENT_TYPE: ContextVar[Any] = ContextVar(
    "artifact_request_client_type",
    default=None,
)


def get_artifact_request_client_type() -> Any:
    return _REQUEST_CLIENT_TYPE.get()


def set_artifact_request_client_type(value: Any) -> Token:
    return _REQUEST_CLIENT_TYPE.set(value)


def reset_artifact_request_client_type(token: Token) -> None:
    _REQUEST_CLIENT_TYPE.reset(token)
