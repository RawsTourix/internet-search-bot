class LLMError(Exception):
    """Базовая ошибка LLM."""
    pass


class LLMTransportError(LLMError):
    """Ошибка сети/транспорта при обращении к LLM."""
    pass


class LLMHTTPError(LLMError):
    """Некорректный HTTP-ответ от LLM API."""
    pass


class LLMTimeoutError(LLMTransportError):
    """Таймаут при обращении к LLM."""
    pass