class LLMError(Exception):
    """Базовая ошибка LLM."""
    pass


class LLMTransportError(LLMError):
    """Ошибка сети/транспорта при обращении к LLM."""
    pass


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
            f"Ошибка LLM API: {status_code} - {response_text}"
        )


class LLMTimeoutError(LLMTransportError):
    """Таймаут при обращении к LLM."""
    pass