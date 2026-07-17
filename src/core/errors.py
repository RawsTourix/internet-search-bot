class APIError(Exception):
    """Базовая ошибка API."""
    pass

class LLMError(Exception):
    """Базовая ошибка LLM."""
    pass


class LLMTransportError(LLMError):
    """Ошибка сети/транспорта при обращении к LLM."""

    def __init__(
        self,
        message: str,
        *,
        cause_type: str | None = None,
        request_method: str | None = None,
        request_url: str | None = None,
        original_repr: str | None = None
    ):
        self.cause_type = cause_type
        self.request_method = request_method
        self.request_url = request_url
        self.original_repr = original_repr

        details = []

        if cause_type:
            details.append(f"cause={cause_type}")

        if request_method or request_url:
            details.append(f"request={request_method or '?'} {request_url or '?'}")

        if original_repr:
            details.append(f"original={original_repr}")

        if details:
            message = f"{message} ({', '.join(details)})"

        super().__init__(message)


class LLMHTTPError(LLMError):
    """Некорректный HTTP-ответ от LLM API."""

    def __init__(
        self,
        status_code: int,
        response_text: str,
        retry_after: float | None = None
    ):
        self.status_code = status_code
        self.response_text = response_text
        self.retry_after = retry_after

        super().__init__(
            f"Ошибка LLM API: HTTP {status_code}"
        )


class LLMTimeoutError(LLMTransportError):
    """Таймаут при обращении к LLM."""
    pass
